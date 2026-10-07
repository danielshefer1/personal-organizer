"""The app-level tenant filter.

RLS is the guarantee; this is the belt to its braces, and the seam the isolation suite cuts.
``scoped(stmt, Model, tenant_id)`` adds ``WHERE <tenant column> = :tenant_id`` to a
``select``, ``update`` or ``delete`` of a tenant model, reading the column from the model's
marker: ``tenant_id`` for ``TenantMixin``, ``id`` for ``TenantRoot``.

Repositories import it by name (``from ...scope import scoped``), and the isolation suite
replaces that name in every repository module, so a query that filters by tenant *without*
going through here is one the suite cannot switch off -- and one RLS is not being shown to
cover. Use it for every tenant filter.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import Delete, Select, Update

from personal_organizer.db.base import Base, TenantMixin, TenantRoot


def scoped[S: (Select[Any], Update, Delete)](stmt: S, model: type[Base], tenant_id: UUID) -> S:
    """Return ``stmt`` restricted to ``tenant_id``'s rows of ``model``."""
    if not issubclass(model, TenantMixin | TenantRoot):
        msg = f"{model.__name__} is not a tenant model"
        raise TypeError(msg)
    return stmt.where(model.__table__.c[model.tenant_column] == tenant_id)


__all__ = ["scoped"]
