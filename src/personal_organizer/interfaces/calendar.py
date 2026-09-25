"""Calendar provider interface.

Behind this sits Composio managed auth today and our own Google OAuth app from Iteration 08,
so the cutover is an adapter swap.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable

from personal_organizer.core.types import TenantId


@dataclass(frozen=True)
class CalendarEvent:
    external_id: str
    title: str
    start: datetime
    end: datetime
    all_day: bool = False


@runtime_checkable
class CalendarProvider(Protocol):
    async def list_events(
        self, tenant_id: TenantId, *, start: datetime, end: datetime
    ) -> Sequence[CalendarEvent]: ...

    async def create_event(self, tenant_id: TenantId, event: CalendarEvent) -> CalendarEvent: ...


__all__ = ["CalendarEvent", "CalendarProvider"]
