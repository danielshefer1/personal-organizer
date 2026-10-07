"""``scoped`` -- the app-level tenant filter the isolation suite switches off.

No database: what is pinned is the SQL it adds, and that it refuses a model RLS does not
cover, where a tenant filter would be a bug hiding behind a missing policy.
"""

from __future__ import annotations

from uuid import UUID

import pytest
from sqlalchemy import delete, select, update
from sqlalchemy.sql import ClauseElement

from personal_organizer.db.models import ChannelInbox, Message, Tenant, TenantIdentity
from personal_organizer.db.repositories.scope import scoped

TENANT = UUID("00000000-0000-4000-8000-000000000001")


def _sql(stmt: ClauseElement) -> str:
    return str(stmt)


def test_a_mixin_table_is_filtered_on_tenant_id() -> None:
    sql = _sql(scoped(select(TenantIdentity), TenantIdentity, TENANT))
    assert "WHERE tenant_identities.tenant_id = :tenant_id_1" in sql


def test_the_root_table_is_filtered_on_id() -> None:
    sql = _sql(scoped(select(Tenant), Tenant, TENANT))
    assert "WHERE tenants.id = :id_1" in sql


def test_updates_and_deletes_are_filtered_too() -> None:
    expected = "WHERE messages.tenant_id = :tenant_id_1"
    assert expected in _sql(scoped(update(Message), Message, TENANT))
    assert expected in _sql(scoped(delete(Message), Message, TENANT))


def test_a_table_outside_rls_is_refused() -> None:
    with pytest.raises(TypeError, match="ChannelInbox is not a tenant model"):
        scoped(select(ChannelInbox), ChannelInbox, TENANT)
