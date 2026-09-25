"""The runtime role's constraints.

This is the foundation Iteration 03's RLS sits on. If `app_user` is a superuser, owns
tables, or has BYPASSRLS, then every policy written in Iteration 03 is decorative --
and nothing else in the test suite would notice.
"""

from __future__ import annotations

from typing import Any

import pytest

pytestmark = pytest.mark.db


async def test_runtime_role_is_not_a_superuser(app_conn: Any) -> None:
    assert (
        await app_conn.fetchval("SELECT rolsuper FROM pg_roles WHERE rolname = current_user")
        is False
    )


async def test_runtime_role_cannot_bypass_rls(app_conn: Any) -> None:
    assert (
        await app_conn.fetchval("SELECT rolbypassrls FROM pg_roles WHERE rolname = current_user")
        is False
    )


async def test_runtime_role_owns_no_tables(app_conn: Any) -> None:
    owned = await app_conn.fetchval(
        "SELECT count(*) FROM pg_class c JOIN pg_roles r ON c.relowner = r.oid "
        "WHERE r.rolname = current_user AND c.relkind IN ('r', 'p')"
    )
    assert owned == 0


async def test_runtime_role_cannot_create_in_public(app_conn: Any) -> None:
    assert (
        await app_conn.fetchval("SELECT has_schema_privilege(current_user, 'public', 'CREATE')")
        is False
    )


async def test_runtime_role_cannot_become_the_owner(app_conn: Any) -> None:
    """No SET ROLE escape hatch out of RLS."""
    assert await app_conn.fetchval("SELECT pg_has_role(current_user, 'app_owner', 'USAGE')") is (
        False
    )


async def test_owner_role_is_not_a_superuser(owner_conn: Any) -> None:
    """FORCE ROW LEVEL SECURITY is meaningless against a superuser table owner, so the
    owner being non-super is what makes the spec's 'second guard' real."""
    assert (
        await owner_conn.fetchval("SELECT rolsuper FROM pg_roles WHERE rolname = current_user")
        is False
    )


async def test_runtime_role_can_use_the_queue(app_conn: Any) -> None:
    """Procrastinate tables are owned by app_owner; app_user reaches them via the default
    privileges bootstrap installs. Without this the worker fails confusingly at runtime."""
    assert (
        await app_conn.fetchval(
            "SELECT has_table_privilege(current_user, 'procrastinate_jobs', 'SELECT')"
        )
        is True
    )
