"""Raw-body preservation for the webhook router.

Meta computes ``X-Hub-Signature-256`` over the **exact bytes** it sent, so Iteration 02 must
verify the HMAC against those bytes and not against a re-serialised parse.

This is a route class rather than middleware on purpose: global middleware would buffer every
request body, including future streaming ones. Scoped to the webhook router, it buffers only
what needs buffering. Starlette caches the result on ``request._body``, so downstream Pydantic
parsing reads the same bytes the signature covers -- there is no second read.
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from typing import Any

from fastapi import Request, Response
from fastapi.routing import APIRoute
from starlette.status import HTTP_413_CONTENT_TOO_LARGE

#: Meta's webhook payloads are small; anything larger is not one.
MAX_WEBHOOK_BODY_BYTES = 1024 * 1024


class RawBodyRoute(APIRoute):
    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        original = super().get_route_handler()

        async def handler(request: Request) -> Response:
            declared = request.headers.get("content-length")
            if declared is not None and int(declared) > MAX_WEBHOOK_BODY_BYTES:
                return Response(status_code=HTTP_413_CONTENT_TOO_LARGE)
            request.state.raw_body = await request.body()
            return await original(request)

        return handler


__all__ = ["MAX_WEBHOOK_BODY_BYTES", "RawBodyRoute"]
