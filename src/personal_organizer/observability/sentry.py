"""Sentry wiring.

Sentry does not go through structlog, so the log redaction chain gives it no protection at
all: it captures exceptions directly, and exception messages from asyncpg and Pydantic embed
the offending values. Both hooks below run the event through the *same* :func:`scrub_text`
the log redactor uses -- a separate implementation here is how the two silently drift apart.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import sentry_sdk
from sentry_sdk.integrations.logging import LoggingIntegration
from sentry_sdk.types import Event, Hint

from personal_organizer.observability.redaction import (
    REDACTED_SECRET,
    SECRET_KEYS,
    scrub_text,
)
from personal_organizer.settings import Settings

_MAX_DEPTH = 8


def _scrub(value: Any, depth: int = 0) -> Any:
    if depth >= _MAX_DEPTH:
        return "<redacted:depth>"
    if isinstance(value, str):
        return scrub_text(value)
    if isinstance(value, Mapping):
        return {
            key: (
                REDACTED_SECRET if str(key).casefold() in SECRET_KEYS else _scrub(item, depth + 1)
            )
            for key, item in value.items()
        }
    if isinstance(value, Sequence) and not isinstance(value, str | bytes):
        return [_scrub(item, depth + 1) for item in value]
    return value


def scrub_event(event: Event, _hint: Hint) -> Event:
    """``before_send`` / ``before_send_transaction`` hook."""
    scrubbed: Event = _scrub(event)
    return scrubbed


def scrub_breadcrumb(crumb: dict[str, Any], _hint: dict[str, Any] | None = None) -> dict[str, Any]:
    return dict(_scrub(crumb))


def init_sentry(settings: Settings) -> None:
    """Initialise Sentry. A no-op when no DSN is configured (local, CI)."""
    if settings.sentry.dsn is None:
        return

    sentry_sdk.init(
        dsn=settings.sentry.dsn.get_secret_value(),
        environment=settings.app.env,
        release=settings.app.release,
        # Never let Sentry attach request bodies, headers or user identifiers on its own.
        send_default_pii=False,
        max_request_body_size="never",
        traces_sample_rate=settings.sentry.traces_sample_rate,
        profiles_sample_rate=settings.sentry.profiles_sample_rate,
        # Breadcrumbs from stdlib logging only; events come from explicit captures, so a
        # log line never becomes a second, unredacted copy of an error.
        integrations=[LoggingIntegration(level=None, event_level=None)],
        before_send=scrub_event,
        before_send_transaction=scrub_event,
        before_breadcrumb=scrub_breadcrumb,
    )


__all__ = ["init_sentry", "scrub_breadcrumb", "scrub_event"]
