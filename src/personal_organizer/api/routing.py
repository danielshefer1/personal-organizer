"""Raw-body preservation for the webhook router.

Meta computes ``X-Hub-Signature-256`` over the **exact bytes** it sent, so Iteration 02 must
verify the HMAC against those bytes and not against a re-serialised parse.

This is a route class rather than middleware on purpose: global middleware would buffer every
request body, including future streaming ones. Scoped to the webhook router, it buffers only
what needs buffering, and downstream Pydantic parsing reads the same bytes the signature
covers -- there is no second read.

The size limit is enforced **while reading**, not from ``Content-Length``. A chunked request
carries no ``Content-Length`` at all, so a declared-size check alone is a limit an attacker
opts into: omit the header and ``await request.body()`` buffers however much they care to send.
The declared value is still checked first, because rejecting before reading is cheaper.
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from typing import Any

from fastapi import Request, Response
from fastapi.routing import APIRoute
from starlette.status import HTTP_413_CONTENT_TOO_LARGE

#: Meta's webhook payloads are small; anything larger is not one.
MAX_WEBHOOK_BODY_BYTES = 1024 * 1024


def _declared_length(request: Request) -> int | None:
    """``Content-Length`` as an int, or ``None`` if absent or unparseable.

    An unparseable value is treated as absent rather than rejected: a malformed
    ``Content-Length`` is the HTTP layer's business, and answering 4xx here would mean Meta
    retrying a request the streaming cap below already handles correctly.
    """
    declared = request.headers.get("content-length")
    if declared is None:
        return None
    try:
        return int(declared)
    except ValueError:
        return None


async def _read_capped(request: Request) -> bytes | None:
    """Buffer the body, or return ``None`` once it exceeds the cap.

    Assigns ``request._body`` because that is the cache Starlette's own ``Request.body()``
    consults; without it, a downstream ``await request.json()`` would try to re-read a stream
    this function has already drained. Reaching into the private attribute is the price of
    capping the read, and it is the same cache the module docstring relies on.
    """
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_WEBHOOK_BODY_BYTES:
            return None
        chunks.append(chunk)
    body = b"".join(chunks)
    request._body = body
    return body


class RawBodyRoute(APIRoute):
    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        original = super().get_route_handler()

        async def handler(request: Request) -> Response:
            declared = _declared_length(request)
            if declared is not None and declared > MAX_WEBHOOK_BODY_BYTES:
                return Response(status_code=HTTP_413_CONTENT_TOO_LARGE)
            body = await _read_capped(request)
            if body is None:
                return Response(status_code=HTTP_413_CONTENT_TOO_LARGE)
            request.state.raw_body = body
            return await original(request)

        return handler


__all__ = ["MAX_WEBHOOK_BODY_BYTES", "RawBodyRoute"]
