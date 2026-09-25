"""Declarative base.

The explicit naming convention matters more here than in a typical project: Iteration 03
writes RLS policies and indexes that refer to constraints by name, and autogenerate must
produce the same names every time or those migrations drift.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import MetaData
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


class TenantMixin:
    """Marks a table as tenant-scoped.

    Iteration 03 enables ``ROW LEVEL SECURITY`` and ``FORCE ROW LEVEL SECURITY`` on exactly
    the tables carrying this mixin, and the isolation suite asserts that set matches the
    tables with RLS enabled -- catching both "forgot to enable" and "enabled on the wrong
    table". ``procrastinate_*`` tables deliberately never carry it: RLS on the queue would
    deadlock the worker against its own jobs.
    """

    tenant_id: Mapped[UUID] = mapped_column(index=True)


__all__ = ["NAMING_CONVENTION", "Base", "TenantMixin"]
