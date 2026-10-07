"""Which tables the models declare tenant-scoped, and the column each is scoped by.

Shared by the RLS invariant (``test_rls.py``) and the isolation suite (``test_isolation.py``),
so both derive their expectations from the same place: the models, never a hand-kept list.
"""

from __future__ import annotations

from personal_organizer.db import models  # noqa: F401  (registers every table)
from personal_organizer.db.base import Base, TenantMixin, TenantRoot


def tenant_tables() -> dict[str, str]:
    """``{table name: tenant column}`` for every ``TenantMixin`` or ``TenantRoot`` model."""
    return {
        table.name: mapper.class_.tenant_column
        for mapper in Base.registry.mappers
        if issubclass(mapper.class_, TenantMixin | TenantRoot)
        for table in mapper.tables
    }


__all__ = ["tenant_tables"]
