"""API fixtures.

``httpx.ASGITransport`` does not run lifespan events, which is convenient here: the app can
be exercised with stubbed ``app.state`` and no database at all. Lifespan itself is covered by
the integration tests that do have Postgres.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from personal_organizer.api.app import create_app
from personal_organizer.interfaces.channel import WebhookBatch
from personal_organizer.messaging.ingress import IngressResult
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


@dataclass(frozen=True)
class DeferredCall:
    name: str
    options: dict[str, Any]
    kwargs: dict[str, Any]


class FakeProcrastinate:
    def __init__(self) -> None:
        #: Task names only, in order -- what most assertions need.
        self.deferred: list[str] = []
        #: Everything: the options (lock, queueing_lock, ...) and the task kwargs.
        self.calls: list[DeferredCall] = []

    def configure_task(self, name: str, **options: Any) -> Any:
        parent = self

        class Deferrer:
            async def defer_async(self, **kwargs: Any) -> int:
                parent.deferred.append(name)
                parent.calls.append(DeferredCall(name, options, kwargs))
                return len(parent.calls)

        return Deferrer()


class FakeIngressStore:
    """Deduplicates by provider message id, like the unique constraint does."""

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[tuple[str, WebhookBatch]] = []
        self.seen: set[tuple[str, str]] = set()

    async def record(self, channel: str, batch: WebhookBatch) -> IngressResult:
        self.calls.append((channel, batch))
        if self.fail:
            msg = "connection to db:5432 lost"
            raise ConnectionError(msg)
        inserted = duplicates = 0
        for message in batch.messages:
            key = (channel, message.provider_message_id)
            if key in self.seen:
                duplicates += 1
            else:
                self.seen.add(key)
                inserted += 1
        return IngressResult(inserted=inserted, duplicates=duplicates)


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
