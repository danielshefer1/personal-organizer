"""ASGI entrypoint.

Module level rather than inside a factory call, and in this order: Sentry must be initialised
before FastAPI is constructed for its instrumentation to attach cleanly, and logging must be
configured before either so that startup itself is redacted.

Run with ``--no-access-log``: uvicorn's own access log prints full URLs, and query strings
carry tokens. :mod:`personal_organizer.api.middleware` emits an allowlisted equivalent.
"""

from __future__ import annotations

from personal_organizer.api.app import create_app
from personal_organizer.observability.logging import configure_logging
from personal_organizer.observability.sentry import init_sentry
from personal_organizer.settings import get_settings

settings = get_settings()
configure_logging(settings)
init_sentry(settings)

app = create_app(settings)

__all__ = ["app"]
