from __future__ import annotations

import io
import re
from time import perf_counter
from uuid import uuid4

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from structlog.testing import capture_logs

from personal_organizer import __version__
from personal_organizer.api.middleware import UNMATCHED_ROUTE
from personal_organizer.observability.logging import configure_logging
from personal_organizer.settings import Settings


class TestHealth:
    async def test_liveness_needs_no_dependencies(self, client: AsyncClient, app: FastAPI) -> None:
        response = await client.get("/health")
        assert response.status_code == 200
        assert response.json()["version"] == __version__
        # Liveness must not touch the database, or it stops being liveness.
        assert app.state.db.checks == 0

    async def test_request_id_is_echoed(self, client: AsyncClient) -> None:
        response = await client.get("/health", headers={"x-request-id": "abc123"})
        assert response.headers["x-request-id"] == "abc123"

    async def test_request_id_is_generated_when_absent(self, client: AsyncClient) -> None:
        response = await client.get("/health")
        assert len(response.headers["x-request-id"]) == 32


class TestReady:
    async def test_ready_checks_the_database(self, client: AsyncClient, app: FastAPI) -> None:
        response = await client.get("/ready")
        assert response.status_code == 200
        assert response.json()["checks"]["database"] == "ok"
        assert app.state.db.checks == 1

    async def test_ready_reports_503_when_the_database_is_down(
        self, client: AsyncClient, app: FastAPI
    ) -> None:
        app.state.db.healthy = False
        response = await client.get("/ready")
        assert response.status_code == 503
        assert response.json()["status"] == "degraded"


class TestRequestContext:
    """X-Request-ID and the request path are the caller's to choose, and both reach a log line
    (scrub_text runs on each, on the event loop): only bounded, id-shaped values get there."""

    @pytest.mark.parametrize(
        "value",
        [
            "a" * 129,
            "x[1](" * 13_000,
            "has space",
            "+31 6 1234 5678",
            "<script>",
            "line\tbreak",
            "naïve",
        ],
        ids=["too_long", "call_string_64k", "space", "phone", "markup", "tab", "non_ascii"],
    )
    async def test_an_odd_request_id_is_replaced(self, client: AsyncClient, value: str) -> None:
        with capture_logs() as logs:
            # Bytes, as a raw client would send them: httpx refuses a non-ASCII str header.
            header = value.encode("latin-1")
            response = await client.get("/health/x", headers={b"x-request-id": header})
        echoed = response.headers["x-request-id"]
        assert echoed != value
        assert re.fullmatch(r"[0-9a-f]{32}", echoed)
        assert all(value not in str(entry) for entry in logs)

    @pytest.mark.parametrize("value", ["a" * 128, "req-1.2:3_4", str(uuid4())])
    async def test_an_id_shaped_request_id_is_kept(self, client: AsyncClient, value: str) -> None:
        response = await client.get("/health", headers={"x-request-id": value})
        assert response.headers["x-request-id"] == value

    async def test_an_unmatched_path_is_not_logged(self, client: AsyncClient) -> None:
        path = "/" + "x[1](" * 12_000  # 60 KB: httpx refuses much longer URLs
        with capture_logs() as logs:
            response = await client.get(path)
        assert response.status_code == 404
        [entry] = [entry for entry in logs if entry["event"] == "http.request"]
        assert entry["route"] == UNMATCHED_ROUTE
        assert all("x[1](x[1](" not in str(entry) for entry in logs)

    async def test_a_matched_route_logs_its_template(self, client: AsyncClient) -> None:
        with capture_logs() as logs:
            await client.post("/internal/ping")
        [entry] = [entry for entry in logs if entry["event"] == "http.request"]
        assert entry["route"] == "/internal/ping"

    async def test_hostile_header_and_path_are_logged_quickly(
        self, client: AsyncClient, settings: Settings
    ) -> None:
        """Through the real processor chain: 64 KB of each took 23 s before the fix. (The
        path is a little short of 64 KB: httpx refuses longer URLs.)"""
        configure_logging(settings, stream=io.StringIO())
        started = perf_counter()
        await client.get("/" + "a" * 60_000, headers={"x-request-id": "a" * 65_536})
        assert perf_counter() - started < 1.0
