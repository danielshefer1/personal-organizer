"""Byte-identity of webhook bodies.

Iteration 02 verifies Meta's ``X-Hub-Signature-256`` over the raw request body. If anything
in the stack re-serialises the payload before the HMAC is computed, every signature check
fails -- and it fails in a way that looks like a credentials problem. This test exists so
that regression is caught here rather than there.
"""

from __future__ import annotations

from collections.abc import Callable

from fastapi import APIRouter, FastAPI, Request
from httpx import AsyncClient

from personal_organizer.api.routing import MAX_WEBHOOK_BODY_BYTES, RawBodyRoute

# Deliberately not canonical JSON: odd whitespace, escaped unicode, key order.
NON_CANONICAL = b'{"b" :  1,\n  "a":\t"caf\\u00e9",  "n": 1.50 }'


def _app_with_raw_route() -> FastAPI:
    router = APIRouter(route_class=RawBodyRoute)

    @router.post("/echo")
    async def echo(request: Request) -> dict[str, object]:
        raw: bytes = request.state.raw_body
        parsed = await request.json()
        return {"raw": raw.decode(), "length": len(raw), "parsed_keys": sorted(parsed)}

    app = FastAPI()
    app.include_router(router)
    return app


class TestRawBody:
    async def test_body_is_byte_identical(
        self, make_client: Callable[[FastAPI], AsyncClient]
    ) -> None:
        async with make_client(_app_with_raw_route()) as client:
            response = await client.post(
                "/echo", content=NON_CANONICAL, headers={"content-type": "application/json"}
            )
        assert response.json()["raw"].encode() == NON_CANONICAL
        assert response.json()["length"] == len(NON_CANONICAL)

    async def test_downstream_parsing_still_works(
        self, make_client: Callable[[FastAPI], AsyncClient]
    ) -> None:
        """Starlette caches the body, so reading it twice must not consume the stream."""
        async with make_client(_app_with_raw_route()) as client:
            response = await client.post(
                "/echo", content=NON_CANONICAL, headers={"content-type": "application/json"}
            )
        assert response.json()["parsed_keys"] == ["a", "b", "n"]

    async def test_oversized_body_is_rejected_before_buffering(
        self, make_client: Callable[[FastAPI], AsyncClient]
    ) -> None:
        async with make_client(_app_with_raw_route()) as client:
            response = await client.post(
                "/echo",
                content=b"{}",
                headers={
                    "content-type": "application/json",
                    "content-length": str(MAX_WEBHOOK_BODY_BYTES + 1),
                },
            )
        assert response.status_code == 413
