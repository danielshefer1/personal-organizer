"""Row-level security for tenant tables, as migrations apply it.

One policy shape for every tenant table, written once (D3 in docs/plan-iteration-03.md):

* ``ENABLE`` turns policies on for every role but the owner and BYPASSRLS roles.
* ``FORCE`` turns them on for the owner too. ``app_owner`` owns every table and runs the
  migrations, so without it a data migration would silently see every tenant at once.
* One ``FOR ALL`` policy, ``tenant_isolation``, whose ``USING`` and ``WITH CHECK`` are the
  same predicate: a row is visible, and may be written, only under its own tenant's GUC.

The predicate reads ``NULLIF(current_setting('app.tenant_id', true), '')::uuid``. The
``true`` makes a missing setting NULL instead of an error. The ``NULLIF`` is for pooled
connections: once a ``SET LOCAL`` has been rolled back or committed, the setting does not
disappear, it reverts to ``''`` -- and ``''::uuid`` raises. NULL compares unequal to every
id, so a session with no tenant sees zero rows and can write none: fail closed.

Imported by revisions as ``from rls import ...``: ``alembic.ini`` puts this directory on
``sys.path`` (``prepend_sys_path``), which Alembic applies before it loads any revision.
"""

from __future__ import annotations

from typing import Final

from alembic import op

POLICY_NAME: Final = "tenant_isolation"

#: The current tenant, NULL when none is set. See the module docstring for the NULLIF.
CURRENT_TENANT: Final = "NULLIF(current_setting('app.tenant_id', true), '')::uuid"


def tenant_rls_statements(table: str, column: str = "tenant_id") -> list[str]:
    """The DDL :func:`enable_tenant_rls` runs, as strings.

    ``table`` and ``column`` come from revision source, never from input, so they are
    interpolated; each is still quoted as an identifier.
    """
    predicate = f'"{column}" = {CURRENT_TENANT}'
    return [
        f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY',
        f'ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY',
        f'CREATE POLICY {POLICY_NAME} ON "{table}" FOR ALL '
        f"USING ({predicate}) WITH CHECK ({predicate})",
    ]


def enable_tenant_rls(table: str, column: str = "tenant_id") -> None:
    """Put ``table`` under the tenant policy. ``tenants`` passes ``column="id"``."""
    for statement in tenant_rls_statements(table, column):
        op.execute(statement)


def disable_tenant_rls(table: str) -> None:
    """Undo :func:`enable_tenant_rls`, for a downgrade that keeps the table."""
    op.execute(f'DROP POLICY IF EXISTS {POLICY_NAME} ON "{table}"')
    op.execute(f'ALTER TABLE "{table}" NO FORCE ROW LEVEL SECURITY')
    op.execute(f'ALTER TABLE "{table}" DISABLE ROW LEVEL SECURITY')


__all__ = [
    "CURRENT_TENANT",
    "POLICY_NAME",
    "disable_tenant_rls",
    "enable_tenant_rls",
    "tenant_rls_statements",
]
