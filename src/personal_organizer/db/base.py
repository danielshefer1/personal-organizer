"""Declarative base.

The explicit naming convention matters more here than in a typical project: Iteration 03
writes RLS policies and indexes that refer to constraints by name, and autogenerate must
produce the same names every time or those migrations drift.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, ClassVar
from uuid import UUID

from sqlalchemy import DateTime, ForeignKey, MetaData
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    # Every timestamp is timestamptz. A naive column plus a server in one zone and a worker
    # in another is how "remind me at 9" fires at 7; there is no column where that is wanted.
    type_annotation_map: ClassVar[dict[Any, Any]] = {datetime: DateTime(timezone=True)}


def check_in(column: str, values: tuple[str, ...]) -> str:
    """The SQL of a ``CHECK`` that ``column`` is one of ``values``.

    Built from the same tuple the code uses, so a model's constraint cannot drift from its
    constants. The migration spells the result out by hand, and
    ``tests/db/test_models_match_migrations.py`` compares the two.
    """
    return f"{column} IN ({', '.join(repr(value) for value in values)})"


class TenantMixin:
    """Marks a table as tenant-scoped.

    Migrations enable ``ROW LEVEL SECURITY`` and ``FORCE ROW LEVEL SECURITY`` on exactly the
    tables carrying this mixin or :class:`TenantRoot`, through ``alembic/rls.py``, and
    ``tests/db/test_rls.py`` asserts that set matches the tables with RLS enabled -- catching
    both "forgot to enable" and "enabled on the wrong table". ``procrastinate_*`` tables
    deliberately never carry it: RLS on the queue would deadlock the worker against its own
    jobs.

    ``tenant_id`` references ``tenants`` with ``ON DELETE CASCADE``, so deleting a tenant
    (not built yet; see the plan's "Not in this iteration") is one statement.
    """

    #: The column the RLS policy compares with ``app.tenant_id``. Read by
    #: :func:`personal_organizer.db.repositories.scope.scoped` and by the RLS tests.
    tenant_column: ClassVar[str] = "tenant_id"

    tenant_id: Mapped[UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )


class TenantRoot:
    """Marks the table whose own ``id`` *is* the tenant id -- ``tenants``, and only it.

    It carries no ``tenant_id``, so :class:`TenantMixin` cannot describe it, yet it holds a
    tenant's settings and must sit under RLS like everything else: its policy compares
    ``id`` with ``app.tenant_id``. The RLS invariant treats ``TenantMixin`` and
    ``TenantRoot`` tables alike.
    """

    tenant_column: ClassVar[str] = "id"


__all__ = ["NAMING_CONVENTION", "Base", "TenantMixin", "TenantRoot", "check_in"]
