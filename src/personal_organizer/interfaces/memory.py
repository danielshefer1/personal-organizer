"""Long-term memory interface (Iteration 11).

The store is hybrid: pgvector similarity plus full-text. Both live behind this Protocol so
the recall strategy can change without touching the agent loop.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from personal_organizer.core.types import TenantId


@dataclass(frozen=True)
class Memory:
    id: str
    kind: str  # fact | preference | person | note
    content: str


@runtime_checkable
class MemoryStore(Protocol):
    async def recall(
        self, tenant_id: TenantId, query: str, *, limit: int = 8
    ) -> Sequence[Memory]: ...

    async def remember(self, tenant_id: TenantId, memory: Memory) -> Memory: ...

    async def forget(self, tenant_id: TenantId, memory_id: str) -> None: ...


__all__ = ["Memory", "MemoryStore"]
