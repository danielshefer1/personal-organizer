"""Telling a tenant they are connected: the logic of the ``onboarding:connected`` task.

This send answers no inbound message, so it has no inbox row to key on and no channel to
follow. It is keyed instead (D6, ``connected:<connection_id>``), which makes a refreshed
callback, and so two jobs, one message. It goes to the tenant's phone on the channel they last
wrote on: the first instance of ADR 0004's "proactive sends need a channel choice" seam.
"""

from __future__ import annotations

from typing import Final
from uuid import UUID

import structlog

from personal_organizer.core.errors import ChannelNotConfiguredError
from personal_organizer.core.types import TenantId
from personal_organizer.db.engine import Database
from personal_organizer.db.repositories.messages import latest_inbound_channel
from personal_organizer.db.repositories.tenants import get_tenant, primary_phone
from personal_organizer.messaging.inbound import ChannelResolver
from personal_organizer.messaging.onboarding_text import t
from personal_organizer.messaging.outbox import send_once

log = structlog.get_logger(__name__)

ALL_SET_KIND: Final = "onboarding:all_set"


def all_set_key(connection_id: UUID) -> str:
    """D6's key: one "all set" per connection, however many callbacks bound it."""
    return f"connected:{connection_id}"


async def announce_connected(
    tenant_id: UUID, connection_id: UUID, *, db: Database, channels: ChannelResolver
) -> str | None:
    """Send "You're all set" at most once.

    Returns the outbox status, or ``None`` when there is nowhere to send it: no inbound message,
    no phone, a channel this worker does not run, or a tenant no longer active. Each of those is
    logged and finishes the job, because a retry would find the same thing. Re-raises
    ``TransientChannelError`` from :func:`send_once`, which the task's retry strategy retries.
    """
    async with db.tenant_session(TenantId(tenant_id)) as session:
        tenant = await get_tenant(session, tenant_id)
        channel_name = await latest_inbound_channel(session, tenant_id)
        phone = await primary_phone(session, tenant_id)
    if tenant is None or tenant.status != "active":
        log.warning("onboarding.all_set_skipped", tenant_id=str(tenant_id), reason="not_active")
        return None
    if channel_name is None or phone is None:
        reason = "no_channel" if channel_name is None else "no_phone"
        log.warning("onboarding.all_set_skipped", tenant_id=str(tenant_id), reason=reason)
        return None
    try:
        channel = channels(channel_name)
    except ChannelNotConfiguredError:
        log.error(
            "onboarding.all_set_skipped",
            tenant_id=str(tenant_id),
            channel=channel_name,
            reason="channel_not_configured",
        )
        return None
    return await send_once(
        db,
        channel,
        inbox_id=None,
        kind=ALL_SET_KIND,
        recipient_key=f"tel:{phone}",
        to=phone,
        text=t("all_set", tenant.language),
        idempotency_key=all_set_key(connection_id),
    )


__all__ = ["ALL_SET_KIND", "all_set_key", "announce_connected"]
