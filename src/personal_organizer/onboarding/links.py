"""The connect link a tenant is sent in the ``connect`` step (D4, D10).

A link is a signed token (:mod:`.tokens`) over the tenant id and a random nonce, plus an
``onboarding_links`` row that makes it single-use and bounds it to ``ONBOARDING__LINK_TTL_S``.
Every message in the ``connect`` step re-sends a link, so links are **reused**: the newest
unused one is sent again, as long as at least half its life remains. A link with seconds
left would expire while the user switches to the browser and signs in to Google. Only then
is a fresh one issued. A user who writes five times holds one live link, not five.

The token is re-signed on every call, so its signature timestamp is the time it was sent,
not the time the row was created. The row's ``expires_at`` stays the authority, and
``consume_link`` checks it.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta
from typing import Final
from uuid import UUID

from personal_organizer.core.types import TenantId
from personal_organizer.db.engine import Database
from personal_organizer.db.repositories.links import create_link, latest_usable_link
from personal_organizer.onboarding.tokens import LINK_SALT, TokenPayload, sign
from personal_organizer.settings import Settings

#: 128 bits: unguessable, and short enough to keep the URL tidy in a chat bubble.
NONCE_BYTES: Final = 16


async def issue_or_reuse_link(
    db: Database, tenant_id: UUID, *, settings: Settings, now: datetime
) -> str:
    """The full URL of a usable connect link for ``tenant_id``, issuing one if needed."""
    base_url = settings.app.public_base_url
    secret = settings.onboarding.link_secret
    if base_url is None or secret is None:
        msg = "connect links need APP__PUBLIC_BASE_URL and ONBOARDING__LINK_SECRET"
        raise RuntimeError(msg)
    ttl = timedelta(seconds=settings.onboarding.link_ttl_s)
    async with db.tenant_session(TenantId(tenant_id)) as session:
        # "Usable" here means "still usable at half-life from now".
        link = await latest_usable_link(session, tenant_id, now=now + ttl / 2)
        if link is not None:
            nonce = link.nonce
        else:
            nonce = secrets.token_urlsafe(NONCE_BYTES)
            await create_link(session, tenant_id, nonce=nonce, expires_at=now + ttl)
    token = sign(
        TokenPayload(tenant_id=tenant_id, nonce=nonce),
        secret=secret.get_secret_value(),
        salt=LINK_SALT,
    )
    return f"{base_url}/connect/{token}"


__all__ = ["NONCE_BYTES", "issue_or_reuse_link"]
