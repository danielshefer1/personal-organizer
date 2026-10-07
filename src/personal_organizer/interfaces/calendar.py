"""Calendar interfaces.

Two seams. ``ConnectLinker`` takes a user through connecting their Google Calendar: Composio
today, under our own Google OAuth app (a Composio custom auth config). ``CalendarProvider``
reads and writes events once connected, from the calendar-read iteration. Both stay
dependency-free, so swapping an adapter never reaches the callers.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Final, Protocol, runtime_checkable

from personal_organizer.core.types import TenantId

#: The one connected-account status that means "tokens held, calls will work".
ACCOUNT_ACTIVE: Final = "ACTIVE"
#: Statuses on the way to ``ACTIVE``: the user is still at Google, or Composio is still
#: exchanging the code. Every other status (``FAILED``, ``EXPIRED``, ``INACTIVE``,
#: ``REVOKED``, and any Composio adds) will not turn ``ACTIVE`` by waiting.
ACCOUNT_CONNECTING: Final = frozenset({"INITIALIZING", "INITIATED"})


@dataclass(frozen=True)
class CalendarEvent:
    external_id: str
    title: str
    start: datetime
    end: datetime
    all_day: bool = False


@dataclass(frozen=True, slots=True)
class ConnectedAccount:
    """What the connect callback needs to know about an account, and nothing more.

    The provider's own record also holds the OAuth tokens. An adapter copies these four fields
    out and drops the rest, so the tokens never reach a log line, an exception or Sentry.
    """

    id: str
    user_id: str
    auth_config_id: str
    status: str


@runtime_checkable
class ConnectLinker(Protocol):
    async def link(self, *, user_id: str, auth_config_id: str, callback_url: str) -> str:
        """Start a connection for ``user_id``; return the URL to send the browser to."""
        ...

    async def get_account(self, connected_account_id: str) -> ConnectedAccount: ...


@runtime_checkable
class CalendarProvider(Protocol):
    async def list_events(
        self, tenant_id: TenantId, *, start: datetime, end: datetime
    ) -> Sequence[CalendarEvent]: ...

    async def create_event(self, tenant_id: TenantId, event: CalendarEvent) -> CalendarEvent: ...


__all__ = [
    "ACCOUNT_ACTIVE",
    "ACCOUNT_CONNECTING",
    "CalendarEvent",
    "CalendarProvider",
    "ConnectLinker",
    "ConnectedAccount",
]
