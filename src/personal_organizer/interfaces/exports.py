"""Export service interface (Iterations 18 and 20).

Exports are built **in memory in the worker** -- nothing is written to disk, which is why no
Railway service in this project declares a writable volume.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from personal_organizer.core.types import TenantId


@runtime_checkable
class ExportService(Protocol):
    async def build(self, tenant_id: TenantId, *, kind: str) -> tuple[str, bytes]:
        """Return ``(filename, content)``. Content stays in memory."""
        ...


__all__ = ["ExportService"]
