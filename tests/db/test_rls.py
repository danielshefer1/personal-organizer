"""RLS invariants.

Iteration 01 has no tenant tables, so this asserts the *invariant* rather than any specific
policy: every table carrying `TenantMixin` has RLS enabled and forced, and no other table
does. Iteration 03 adds tables and this suite grows with them automatically -- catching both
"forgot to enable RLS" and "enabled it on the queue by accident".
"""

from __future__ import annotations

from typing import Any

import pytest

from personal_organizer.db.base import Base, TenantMixin

pytestmark = [pytest.mark.db, pytest.mark.rls]


def _tenant_tables() -> set[str]:
    return {
        str(table.name)
        for table in Base.metadata.tables.values()
        if any(
            issubclass(mapper.class_, TenantMixin) and mapper.local_table is table
            for mapper in Base.registry.mappers
        )
    }


async def test_every_tenant_table_has_rls_enabled_and_forced(app_conn: Any) -> None:
    expected = _tenant_tables()
    rows = await app_conn.fetch(
        "SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class c "
        "JOIN pg_namespace n ON c.relnamespace = n.oid "
        "WHERE n.nspname = 'public' AND c.relkind = 'r' AND c.relrowsecurity"
    )
    enabled = {row["relname"] for row in rows}
    assert enabled == expected, f"RLS enabled on {enabled}, tenant tables are {expected}"
    for row in rows:
        assert row["relforcerowsecurity"], (
            f"{row['relname']} has RLS but not FORCE; the owner would bypass its own policies"
        )


async def test_the_queue_never_has_rls(app_conn: Any) -> None:
    """RLS on procrastinate_jobs would deadlock the worker against its own queue."""
    forced = await app_conn.fetchval(
        "SELECT relrowsecurity FROM pg_class WHERE relname = 'procrastinate_jobs'"
    )
    assert forced is False
