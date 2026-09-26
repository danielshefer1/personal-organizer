"""Task provider interface (Iteration 20, behind a feature flag per Gate 15)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable

from personal_organizer.core.types import TenantId


@dataclass(frozen=True)
class Task:
    external_id: str
    title: str
    due: datetime | None = None
    completed: bool = False


@runtime_checkable
class TasksProvider(Protocol):
    async def list_tasks(self, tenant_id: TenantId) -> Sequence[Task]: ...

    async def create_task(self, tenant_id: TenantId, task: Task) -> Task: ...


__all__ = ["Task", "TasksProvider"]
