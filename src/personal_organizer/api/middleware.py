"""Request context and access logging.

Pure ASGI rather than ``BaseHTTPMiddleware``: the latter wraps the receive channel, which is
exactly the machinery :mod:`personal_organizer.api.routing` depends on for byte-identical
webhook bodies.

The access log deliberately records the **route template** (``/webhooks/{provider}``) rather
than the request path. Path parameters routinely carry identifiers, and a log line naming a
raw path is a PII leak that no redaction regex would reliably catch.
"""

from __future__ import annotations

from time import perf_counter
from typing import Any
from uuid import uuid4

import structlog
from starlette.datastructures import Headers
from starlette.types import ASGIApp, Message, Receive, Scope, Send

log = structlog.get_logger("personal_organizer.access")

#: Routes that are polled constantly and would drown everything else.
QUIET_PATHS = frozenset({"/health", "/ready"})


class RequestContextMiddleware:
    """Binds ``request_id`` for the life of the request and emits one access log line."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        structlog.contextvars.clear_contextvars()
        request_id = Headers(scope=scope).get("x-request-id") or uuid4().hex
        structlog.contextvars.bind_contextvars(request_id=request_id)

        started = perf_counter()
        status = 500

        async def send_wrapper(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                headers: list[tuple[bytes, bytes]] = message.setdefault("headers", [])
                headers.append((b"x-request-id", request_id.encode()))
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            path = scope.get("path", "")
            if path not in QUIET_PATHS:
                route: Any = scope.get("route")
                log.info(
                    "http.request",
                    method=scope.get("method"),
                    route=getattr(route, "path", path),
                    status_code=status,
                    duration_ms=round((perf_counter() - started) * 1000, 2),
                )
            structlog.contextvars.clear_contextvars()


__all__ = ["QUIET_PATHS", "RequestContextMiddleware"]
