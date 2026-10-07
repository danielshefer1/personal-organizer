"""The connect flow behind ``/connect``: open a link, then hand the browser to Composio.

D4: a link is spent on POST, never on GET, because WhatsApp fetches links to build previews.
:func:`open_link` reads (the tenant's language and number, for the page) and writes nothing.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Final
from urllib.parse import urlencode

from personal_organizer.core.errors import ConfigError
from personal_organizer.core.types import TenantId
from personal_organizer.db.engine import Database
from personal_organizer.db.repositories.links import consume_link
from personal_organizer.db.repositories.tenants import get_tenant, primary_phone
from personal_organizer.interfaces.calendar import ConnectLinker
from personal_organizer.onboarding.tokens import (
    LINK_SALT,
    STATE_SALT,
    TokenPayload,
    sign,
    verify,
)
from personal_organizer.settings import Settings

#: Only a tenant still onboarding may use a link. An active one has connected already, through
#: another link; reconnecting arrives with the calendar-read iteration.
_LINKABLE: Final = "onboarding"
#: How many trailing digits of the number the page shows: enough for someone handed another
#: person's link to see that it is not theirs before connecting their Google account to it.
PHONE_SUFFIX_DIGITS: Final = 4


@dataclass(frozen=True, slots=True)
class ConnectConfig:
    """The four settings the flow needs, narrowed from optional. Composio being enabled makes
    them required (``Settings._composio_is_complete``), and the router mounts only then."""

    secret: str
    base_url: str
    auth_config_id: str
    link_ttl_s: int

    @classmethod
    def of(cls, settings: Settings) -> ConnectConfig:
        secret = settings.onboarding.link_secret
        base_url = settings.app.public_base_url
        auth_config_id = settings.composio.calendar_auth_config_id
        if secret is None or base_url is None or auth_config_id is None:
            msg = "The connect pages need the settings COMPOSIO__ENABLED requires"
            raise ConfigError(msg)
        return cls(
            secret=secret.get_secret_value(),
            base_url=base_url,
            auth_config_id=auth_config_id,
            link_ttl_s=settings.onboarding.link_ttl_s,
        )

    @property
    def callback_base(self) -> str:
        return f"{self.base_url}/connect/callback"


@dataclass(frozen=True, slots=True)
class LinkPage:
    language: str
    phone_suffix: str | None


def _link_payload(token: str, config: ConnectConfig) -> TokenPayload | None:
    return verify(token, secret=config.secret, salt=LINK_SALT, max_age_s=config.link_ttl_s)


async def open_link(token: str, *, db: Database, config: ConnectConfig) -> LinkPage | None:
    """What the button page shows, or ``None`` for a link that cannot be used. Writes nothing.

    It checks the signature and the tenant, not whether the link row is spent or expired:
    there is no read for that, and the POST is the authority either way.
    """
    payload = _link_payload(token, config)
    if payload is None:
        return None
    async with db.tenant_session(TenantId(payload.tenant_id)) as session:
        tenant = await get_tenant(session, payload.tenant_id)
        if tenant is None or tenant.status != _LINKABLE:
            return None
        phone = await primary_phone(session, payload.tenant_id)
    return LinkPage(
        language=tenant.language,
        phone_suffix=phone[-PHONE_SUFFIX_DIGITS:] if phone else None,
    )


async def start_connect(
    token: str, *, db: Database, linker: ConnectLinker, config: ConnectConfig, now: datetime
) -> str | None:
    """Spend the link and return Composio's URL, or ``None`` if the link cannot be used.

    The link is spent *before* Composio is called, in its own transaction, so two taps racing
    each other get one redirect between them: ``consume_link`` is one atomic UPDATE. The cost:
    if Composio then fails, this link is gone, and the user asks the bot for a new one, which
    the connect step issues because none is left unused.

    Raises :class:`~personal_organizer.core.errors.CalendarProviderError` from ``linker``.
    """
    payload = _link_payload(token, config)
    if payload is None:
        return None
    tenant_id = payload.tenant_id
    async with db.tenant_session(TenantId(tenant_id)) as session:
        tenant = await get_tenant(session, tenant_id)
        if tenant is None or tenant.status != _LINKABLE:
            return None
        if not await consume_link(session, tenant_id, nonce=payload.nonce, now=now):
            return None
    state = sign(
        TokenPayload(tenant_id=tenant_id, nonce=payload.nonce),
        secret=config.secret,
        salt=STATE_SALT,
    )
    return await linker.link(
        user_id=str(tenant_id),
        auth_config_id=config.auth_config_id,
        callback_url=f"{config.callback_base}?{urlencode({'state': state})}",
    )


__all__ = [
    "PHONE_SUFFIX_DIGITS",
    "ConnectConfig",
    "LinkPage",
    "open_link",
    "start_connect",
]
