"""Who a message is from, as a tenant: the network-keyed identities of D12.

``tenant_identities`` is keyed by **network**, not by gateway. Meta's ``whatsapp`` channel and
the ``gowa`` gateway are both the network ``whatsapp``, so one person writing to either
number is one tenant (ADR 0004's "one person, one key"). A key is ``SenderRef.key``: ``uid:``
plus Meta's BSUID, or ``tel:`` plus the E.164 number.

A sender is looked up by every key their message carries, strongest first. The ``uid:`` key
survives a user hiding their number behind a username, and ``tel:`` is what the gateway and
the invite list know. Two consequences:

- Found by ``tel:`` while the message also carried a ``uid:`` (a Meta BSUID arriving for a
  number first seen on the gateway): the ``uid:`` key is added to that tenant, so their next
  message resolves even with no number in it.
- A new tenant is created on the strongest key, and the others are added at once. Enrolling
  through Meta and writing later through the gateway is therefore still one person.

Resolution and creation run as ``app_user`` through the two ``SECURITY DEFINER`` functions
(D1). The worker cannot read ``tenant_identities`` before it knows the tenant, by design.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Final
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from personal_organizer.core.types import TenantId
from personal_organizer.db.engine import Database
from personal_organizer.db.models.tenant import NETWORK_WHATSAPP
from personal_organizer.db.repositories.messages import latest_inbound_channel
from personal_organizer.db.repositories.tenants import (
    add_identity,
    create_tenant,
    get_tenant,
    primary_phone,
    resolve_tenant,
)
from personal_organizer.messaging.inbox import InboxRow

#: Channel name -> identity network. A future Telegram channel maps to ``"telegram"``.
_NETWORKS: Final = MappingProxyType({"whatsapp": NETWORK_WHATSAPP, "gowa": NETWORK_WHATSAPP})


@dataclass(frozen=True, slots=True)
class TenantState:
    """What the gate and the onboarding steps decide from, read once per job."""

    id: UUID
    status: str
    step: str | None
    language: str
    #: No inbound message of theirs is recorded yet, so this one is their first. Recording
    #: happens in the transaction that finishes a row (D7), so a job that died before
    #: finishing still counts as the first message when it re-runs.
    first_message: bool


def network_for(channel: str) -> str:
    try:
        return _NETWORKS[channel]
    except KeyError:
        msg = f"no identity network for channel {channel!r}"
        raise ValueError(msg) from None


def identity_keys(row: InboxRow) -> tuple[str, ...]:
    """The row's keys, strongest first. The first is always ``row.sender_key``."""
    keys: list[str] = []
    if row.sender_user_id:
        keys.append(f"uid:{row.sender_user_id}")
    if row.sender_phone:
        keys.append(f"tel:{row.sender_phone}")
    return tuple(keys)


async def _first_match(
    session: AsyncSession, network: str, keys: tuple[str, ...]
) -> tuple[UUID, int] | None:
    for index, key in enumerate(keys):
        tenant_id = await resolve_tenant(session, network=network, external_id=key)
        if tenant_id is not None:
            return tenant_id, index
    return None


async def _link(
    db: Database, tenant_id: UUID, network: str, keys: tuple[str, ...], phone: str | None
) -> None:
    async with db.tenant_session(TenantId(tenant_id)) as session:
        for key in keys:
            await add_identity(session, tenant_id, network=network, external_id=key, phone=phone)


async def resolve_sender(db: Database, row: InboxRow) -> UUID | None:
    """The tenant this message is from, or ``None``. Records any stronger key it was missing."""
    network = network_for(row.channel)
    keys = identity_keys(row)
    async with db.system_session() as session:
        found = await _first_match(session, network, keys)
    if found is None:
        return None
    tenant_id, index = found
    if index > 0:
        await _link(db, tenant_id, network, keys[:index], row.sender_phone)
    return tenant_id


async def enrol(db: Database, row: InboxRow, *, language: str) -> UUID:
    """Create the tenant for an invited sender. Idempotent on the identity key."""
    network = network_for(row.channel)
    keys = identity_keys(row)
    async with db.system_session() as session:
        tenant_id = await create_tenant(
            session,
            network=network,
            external_id=keys[0],
            phone=row.sender_phone,
            language=language,
        )
    if len(keys) > 1:
        await _link(db, tenant_id, network, keys[1:], row.sender_phone)
    return tenant_id


async def load_state(db: Database, tenant_id: UUID) -> TenantState | None:
    async with db.tenant_session(TenantId(tenant_id)) as session:
        tenant = await get_tenant(session, tenant_id)
        if tenant is None:
            return None
        first = await latest_inbound_channel(session, tenant_id) is None
    return TenantState(
        id=tenant.id,
        status=tenant.status,
        step=tenant.onboarding_step,
        language=tenant.language,
        first_message=first,
    )


async def phone_of(db: Database, tenant_id: UUID) -> str | None:
    async with db.tenant_session(TenantId(tenant_id)) as session:
        return await primary_phone(session, tenant_id)


__all__ = [
    "TenantState",
    "enrol",
    "identity_keys",
    "load_state",
    "network_for",
    "phone_of",
    "resolve_sender",
]
