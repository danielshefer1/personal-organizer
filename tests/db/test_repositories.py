"""Repository behaviour, against Postgres, as ``app_user`` under RLS.

What each function promises its callers in PRs 3 and 4: idempotent tenant creation, a
single-use link, a reconnect that leaves one active connection, and where a send that
answers nothing should go. Isolation between tenants is ``test_isolation.py``'s job.
"""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy.exc import IntegrityError

from personal_organizer.core.types import TenantId
from personal_organizer.db.engine import Database
from personal_organizer.db.models.tenant import NETWORK_WHATSAPP, TenantIdentity
from personal_organizer.db.repositories import tenants

pytestmark = [pytest.mark.db, pytest.mark.usefixtures("clean_tenant_tables")]

PHONE = "+972501234567"
KEY = f"tel:{PHONE}"


async def _create(database: Database, *, key: str = KEY, phone: str | None = PHONE) -> UUID:
    async with database.system_session() as session:
        return await tenants.create_tenant(
            session, network=NETWORK_WHATSAPP, external_id=key, phone=phone, language="he"
        )


# --- tenants ------------------------------------------------------------------------------


async def test_create_tenant_starts_onboarding_at_the_zone_step(database: Database) -> None:
    tenant_id = await _create(database)
    async with database.tenant_session(TenantId(tenant_id)) as session:
        tenant = await tenants.get_tenant(session, tenant_id)
    assert tenant is not None
    assert (tenant.status, tenant.onboarding_step, tenant.language, tenant.timezone) == (
        "onboarding",
        "zone",
        "he",
        None,
    )


async def test_create_tenant_is_idempotent_on_the_identity(database: Database) -> None:
    first = await _create(database)
    second = await _create(database, phone="+15550000000")
    assert first == second
    async with database.tenant_session(TenantId(first)) as session:
        # The second call changed nothing, not even the phone it was given.
        assert await tenants.primary_phone(session, first) == PHONE


async def test_concurrent_create_tenant_leaves_one_tenant(
    database: Database, owner_conn: Any
) -> None:
    """Two messages from a new sender, handled by two workers at once."""
    ids = await asyncio.gather(*(_create(database) for _ in range(5)))
    assert len(set(ids)) == 1
    # A loser's tenant row would be an orphan no GUC can see, so count past RLS: app_owner
    # may become app_definer (bootstrap grants it for ALTER FUNCTION), which is BYPASSRLS.
    async with owner_conn.transaction():
        await owner_conn.execute("SET LOCAL ROLE app_definer")
        assert await owner_conn.fetchval("SELECT count(*) FROM tenants") == 1
        assert await owner_conn.fetchval("SELECT count(*) FROM tenant_identities") == 1


async def test_create_tenant_rejects_an_unknown_language(
    database: Database, owner_conn: Any
) -> None:
    """D11 detects ``he`` or ``en``; anything else is a caller bug, refused whole."""
    with pytest.raises(IntegrityError, match="ck_tenants_language"):
        async with database.system_session() as session:
            await tenants.create_tenant(
                session, network=NETWORK_WHATSAPP, external_id=KEY, phone=PHONE, language="fr"
            )
    async with database.system_session() as session:
        assert (
            await tenants.resolve_tenant(session, network=NETWORK_WHATSAPP, external_id=KEY) is None
        )


async def test_resolve_tenant(database: Database) -> None:
    async with database.system_session() as session:
        assert (
            await tenants.resolve_tenant(session, network=NETWORK_WHATSAPP, external_id=KEY) is None
        )
    tenant_id = await _create(database)
    async with database.system_session() as session:
        assert (
            await tenants.resolve_tenant(session, network=NETWORK_WHATSAPP, external_id=KEY)
            == tenant_id
        )
        # Keyed by network (D12): the same key on another network is someone else.
        assert await tenants.resolve_tenant(session, network="telegram", external_id=KEY) is None


async def test_onboarding_state_updates(database: Database) -> None:
    tenant_id = await _create(database)
    async with database.tenant_session(TenantId(tenant_id)) as session:
        await tenants.set_timezone(session, tenant_id, "Asia/Jerusalem")
        await tenants.set_onboarding_step(session, tenant_id, "connect")
        tenant = await tenants.get_tenant(session, tenant_id)
        assert tenant is not None
        await session.refresh(tenant)
        assert (tenant.timezone, tenant.onboarding_step, tenant.status) == (
            "Asia/Jerusalem",
            "connect",
            "onboarding",
        )
        assert tenant.updated_at >= tenant.created_at

        await tenants.activate(session, tenant_id)
        await session.refresh(tenant)
        assert (tenant.status, tenant.onboarding_step) == ("active", None)


async def test_primary_phone_is_the_earliest_identity_with_one(database: Database) -> None:
    tenant_id = await _create(database, key="uid:BSUID.1", phone=None)
    async with database.tenant_session(TenantId(tenant_id)) as session:
        assert await tenants.primary_phone(session, tenant_id) is None
        session.add(
            TenantIdentity(
                tenant_id=tenant_id, network=NETWORK_WHATSAPP, external_id=KEY, phone=PHONE
            )
        )
    # A separate transaction: created_at is now(), the transaction's start time.
    async with database.tenant_session(TenantId(tenant_id)) as session:
        session.add(
            TenantIdentity(
                tenant_id=tenant_id,
                network="telegram",
                external_id="tel:+15550000000",
                phone="+15550000000",
            )
        )
    async with database.tenant_session(TenantId(tenant_id)) as session:
        assert await tenants.primary_phone(session, tenant_id) == PHONE
