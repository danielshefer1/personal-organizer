"""Tenant scoping for database sessions.

Iteration 03 keys every RLS policy to ``current_setting('app.tenant_id')`` set via
``SET LOCAL``. Three facts decide how that GUC has to be applied:

1. ``SET`` accepts no bind parameters, so it must go through ``set_config(..., true)``.
   Anything that string-formats a tenant id into a ``SET`` statement is an injection vector.
2. ``SET LOCAL`` is reset at COMMIT/ROLLBACK, which makes it pool-safe -- but *only* if every
   statement runs inside an explicit transaction.
3. ``AsyncSession`` begins lazily and silently **re-begins after a commit**. A helper that
   sets the GUC once at the top of a block therefore loses it the moment anything commits
   mid-block, and every subsequent query in that block would silently see zero rows.

Hence an ``after_begin`` listener rather than a one-shot statement: the GUC is re-applied to
every transaction the session starts, including post-commit re-begins.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import event, text
from sqlalchemy.orm import Session

TENANT_INFO_KEY = "po_tenant_id"

_SET_TENANT = text("SELECT set_config('app.tenant_id', :tenant_id, true)")


@event.listens_for(Session, "after_begin")
def _apply_tenant_guc(session: Session, _transaction: Any, connection: Any) -> None:
    tenant_id = session.info.get(TENANT_INFO_KEY)
    if tenant_id is None:
        return
    connection.execute(_SET_TENANT, {"tenant_id": str(tenant_id)})


__all__ = ["TENANT_INFO_KEY"]
