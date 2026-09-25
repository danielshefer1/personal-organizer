"""API fixtures.

``httpx.ASGITransport`` does not run lifespan events, which is convenient here: the app can
be exercised with stubbed ``app.state`` and no database at all. Lifespan itself is covered by
the integration tests that do have Postgres.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from personal_organizer.api.app import create_app
from personal_organizer.settings import Settings


class FakeDatabase:
    def __init__(self, *, healthy: bool = True) -> None:
        self.healthy = healthy
        self.checks = 0

    async def check(self, **_: Any) -> None:
        self.checks += 1
        if not self.healthy:
            msg = "connection refused to app_user@db:5432"
            raise ConnectionError(msg)


class FakeProcrastinate:
    def __init__(self) -> None:
        self.deferred: list[str] = []

    def configure_task(self, name: str, **_: Any) -> Any:
        parent = self

        class Deferrer:
            async def defer_async(self, **__: Any) -> int:
                parent.deferred.append(name)
                return 1

        return Deferrer()


@pytest.fixture
def app(settings: Settings) -> FastAPI:
    application = create_app(settings)
    application.state.db = FakeDatabase()
    application.state.procrastinate = FakeProcrastinate()
    return application


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        yield http


@pytest.fixture
def make_client() -> Callable[[FastAPI], AsyncClient]:
    def factory(application: FastAPI) -> AsyncClient:
        transport = ASGITransport(app=application, raise_app_exceptions=False)
        return AsyncClient(transport=transport, base_url="http://test")

    return factory
