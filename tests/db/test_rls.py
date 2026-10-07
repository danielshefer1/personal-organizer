"""RLS invariants.

These assert the *invariant* rather than any one policy: every table carrying ``TenantMixin``
or ``TenantRoot`` has RLS enabled and forced and the one ``tenant_isolation`` policy, and no
other table has RLS. The set grows with the models automatically -- catching both "forgot to
enable RLS" and "enabled it on the queue by accident". What the policies *do* is
``test_isolation.py``'s job.
"""

from __future__ import annotations

from typing import Any

import pytest

from tests.db.tenant_tables import tenant_tables

pytestmark = [pytest.mark.db, pytest.mark.rls]

_RLS_TABLES = (
    "SELECT relname, relforcerowsecurity FROM pg_class c "
    "JOIN pg_namespace n ON c.relnamespace = n.oid "
    "WHERE n.nspname = 'public' AND c.relkind = 'r' AND c.relrowsecurity"
)

#: How PostgreSQL prints ``alembic/rls.py``'s predicate back out of ``pg_policies``.
_CURRENT_TENANT = "(NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid"


async def rls_problems(conn: Any, expected: set[str]) -> list[str]:
    """Every way the database's RLS tables differ from ``expected``, as messages."""
    rows = await conn.fetch(_RLS_TABLES)
    enabled = {row["relname"] for row in rows}
    problems = [f"{name}: tenant table without RLS" for name in sorted(expected - enabled)]
    problems += [f"{name}: RLS on a table no model scopes" for name in sorted(enabled - expected)]
    problems += [
        f"{row['relname']}: RLS but not FORCE; the owner would bypass its own policies"
        for row in rows
        if not row["relforcerowsecurity"]
    ]
    return problems


async def test_every_tenant_table_has_rls_enabled_and_forced(app_conn: Any) -> None:
    assert await rls_problems(app_conn, set(tenant_tables())) == []


async def test_a_table_with_rls_but_no_force_is_reported(owner_conn: Any) -> None:
    """The check above must be able to fail. Rolled back, so nothing is left behind."""
    tables = set(tenant_tables())
    transaction = owner_conn.transaction()
    await transaction.start()
    try:
        await owner_conn.execute("CREATE TABLE rls_probe (id int)")
        await owner_conn.execute("ALTER TABLE rls_probe ENABLE ROW LEVEL SECURITY")
        problems = await rls_problems(owner_conn, tables | {"rls_probe"})
    finally:
        await transaction.rollback()
    assert problems == ["rls_probe: RLS but not FORCE; the owner would bypass its own policies"]


async def test_the_tenants_table_is_a_tenant_table() -> None:
    """``tenants`` has no ``tenant_id``; ``TenantRoot`` is what puts it under the invariant."""
    assert tenant_tables()["tenants"] == "id"


async def test_every_tenant_table_has_exactly_the_tenant_isolation_policy(
    app_conn: Any,
) -> None:
    expected = {
        table: (f"({column} = {_CURRENT_TENANT})",) * 2 for table, column in tenant_tables().items()
    }
    rows = await app_conn.fetch(
        "SELECT tablename, policyname, cmd, permissive, qual, with_check FROM pg_policies "
        "WHERE schemaname = 'public'"
    )
    found = {row["tablename"]: (row["qual"], row["with_check"]) for row in rows}
    assert found == expected
    assert len(rows) == len(expected), "a second policy on a tenant table widens what it shows"
    for row in rows:
        assert (row["policyname"], row["cmd"], row["permissive"]) == (
            "tenant_isolation",
            "ALL",
            "PERMISSIVE",
        )


async def test_the_queue_never_has_rls(app_conn: Any) -> None:
    """RLS on procrastinate_jobs would deadlock the worker against its own queue."""
    forced = await app_conn.fetchval(
        "SELECT relrowsecurity FROM pg_class WHERE relname = 'procrastinate_jobs'"
    )
    assert forced is False
