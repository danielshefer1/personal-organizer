"""Tenant GUC scoping.

The `after_begin` listener exists for one specific reason: `AsyncSession` re-begins a
transaction automatically after a commit, so a helper that sets `app.tenant_id` once at the
top of a block loses it the moment anything commits. Every subsequent query would then see
zero rows under RLS -- silently, and only in production where RLS is doing real work.

Without the second test below, that listener will eventually be "simplified" into a one-shot
statement and nothing will notice.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from personal_organizer.core.types import TenantId
from personal_organizer.db.engine import Database
from personal_organizer.db.session import TENANT_INFO_KEY
from personal_organizer.settings import Settings

pytestmark = pytest.mark.db

CURRENT_TENANT = text("SELECT current_setting('app.tenant_id', true)")


@pytest.fixture
async def database(db_settings: Settings, app_conn: object) -> Database:
    del app_conn  # reuses the fixture's skip-if-unavailable behaviour
    return Database(db_settings)


async def test_tenant_session_sets_the_guc(database: Database) -> None:
    tenant_id = TenantId(uuid4())
    async with database.tenant_session(tenant_id) as session:
        assert await session.scalar(CURRENT_TENANT) == str(tenant_id)
    await database.dispose()


async def test_guc_is_reapplied_to_every_transaction(database: Database) -> None:
    """The regression this design exists to prevent."""
    tenant_id = TenantId(uuid4())
    maker = async_sessionmaker(database.engine(), expire_on_commit=False)
    async with maker(info={TENANT_INFO_KEY: tenant_id}) as session:
        async with session.begin():
            assert await session.scalar(CURRENT_TENANT) == str(tenant_id)
        # Second transaction on the same session: the GUC was reset by the commit, and the
        # listener must put it back.
        async with session.begin():
            assert await session.scalar(CURRENT_TENANT) == str(tenant_id)
    await database.dispose()


async def test_guc_does_not_leak_between_sessions(database: Database) -> None:
    """SET LOCAL is transaction-scoped, which is what makes this pool-safe."""
    first, second = TenantId(uuid4()), TenantId(uuid4())
    async with database.tenant_session(first) as session:
        assert await session.scalar(CURRENT_TENANT) == str(first)
    async with database.tenant_session(second) as session:
        assert await session.scalar(CURRENT_TENANT) == str(second)
    await database.dispose()


async def test_system_session_has_no_tenant(database: Database) -> None:
    """Non-tenant tables only. Under RLS an unset GUC yields zero rows, which is the
    fail-closed behaviour the policies are written for."""
    async with database.system_session() as session:
        assert await session.scalar(CURRENT_TENANT) in (None, "")
    await database.dispose()
