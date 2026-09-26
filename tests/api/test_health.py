from __future__ import annotations

from fastapi import FastAPI
from httpx import AsyncClient

from personal_organizer import __version__


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
