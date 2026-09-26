"""The walking skeleton's own endpoint.

``POST /internal/ping`` is the whole point of Iteration 01 -- it is what proves
api -> queue -> worker -> database -- and until now nothing exercised it, so the only evidence
it worked was a curl in the README.

The queue is faked here (``FakeProcrastinate``); the real round trip needs Postgres and a
running worker and is verified by hand per the README. What these tests pin down is the part a
fake can prove: that the endpoint defers *the name the worker actually registers*, and that it
is not reachable without the shared secret.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from fastapi import FastAPI

from personal_organizer.api.app import create_app
from personal_organizer.api.routers.internal import INTERNAL_TOKEN_HEADER
from personal_organizer.settings import Settings
from personal_organizer.worker.app import build_procrastinate_app
from personal_organizer.worker.tasks.system import PING_TASK
from tests.api.conftest import FakeDatabase, FakeProcrastinate

TOKEN = "s3cret-internal-token"


def _app(settings: Settings) -> FastAPI:
    application = create_app(settings)
    application.state.db = FakeDatabase()
    application.state.procrastinate = FakeProcrastinate()
    return application


@pytest.fixture
def token_app(settings_factory: Callable[..., Settings]) -> FastAPI:
    return _app(settings_factory(APP__INTERNAL_TOKEN=TOKEN))


class TestPingDefersTheTask:
    async def test_a_valid_token_defers_the_ping_task(
        self, token_app: FastAPI, make_client: Callable[[FastAPI], Any]
    ) -> None:
        async with make_client(token_app) as client:
            response = await client.post("/internal/ping", headers={INTERNAL_TOKEN_HEADER: TOKEN})

        assert response.status_code == 200
        assert response.json()["deferred"] is True
        assert token_app.state.procrastinate.deferred == [PING_TASK]

    async def test_the_deferred_name_is_one_the_worker_registers(
        self, settings: Settings, token_app: FastAPI, make_client: Callable[[FastAPI], Any]
    ) -> None:
        """The silent failure this closes: ``configure_task`` defaults to
        ``allow_unknown=True``, so if ``PING_TASK`` ever drifts from the registered name the
        api returns 200 {"deferred": true} for a job no worker can run, and nothing fails.
        """
        async with make_client(token_app) as client:
            await client.post("/internal/ping", headers={INTERNAL_TOKEN_HEADER: TOKEN})

        (deferred,) = token_app.state.procrastinate.deferred
        assert deferred in build_procrastinate_app(settings).tasks


class TestTokenGate:
    @pytest.mark.parametrize(
        "headers",
        [
            pytest.param({}, id="missing"),
            pytest.param({INTERNAL_TOKEN_HEADER: ""}, id="empty"),
            pytest.param({INTERNAL_TOKEN_HEADER: "wrong"}, id="wrong"),
            pytest.param({INTERNAL_TOKEN_HEADER: TOKEN + "x"}, id="prefix-of-correct"),
        ],
    )
    async def test_a_bad_token_is_404_and_defers_nothing(
        self,
        token_app: FastAPI,
        make_client: Callable[[FastAPI], Any],
        headers: dict[str, str],
    ) -> None:
        """404 rather than 403 -- a 403 would confirm the route exists."""
        async with make_client(token_app) as client:
            response = await client.post("/internal/ping", headers=headers)

        assert response.status_code == 404
        assert token_app.state.procrastinate.deferred == []

    async def test_the_endpoint_is_open_locally_with_no_token_configured(
        self, settings_factory: Callable[..., Settings], make_client: Callable[[FastAPI], Any]
    ) -> None:
        """Local convenience. The settings validator is what stops this reaching staging."""
        application = _app(settings_factory(APP__ENV="local"))
        async with make_client(application) as client:
            response = await client.post("/internal/ping")

        assert response.status_code == 200
        assert application.state.procrastinate.deferred == [PING_TASK]


def _deployed(settings_factory: Callable[..., Settings], env: str) -> Settings:
    return settings_factory(
        APP__ENV=env,
        SENTRY__DSN="https://k@o.ingest.sentry.io/1",
        LOGGING__PII_PEPPER="a-real-secret",
        APP__INTERNAL_TOKEN=TOKEN,
    )


class TestTheEnvironmentGate:
    """Asserted through a request with a *valid* token, so a 404 can only mean the route is
    absent rather than rejected. Introspecting ``app.routes`` does not work: FastAPI wraps an
    included router in an opaque object that carries no ``path``, which makes the obvious
    version of this assertion pass whether the route is there or not.
    """

    async def test_production_does_not_mount_the_router(
        self, settings_factory: Callable[..., Settings], make_client: Callable[[FastAPI], Any]
    ) -> None:
        application = _app(_deployed(settings_factory, "production"))
        async with make_client(application) as client:
            response = await client.post("/internal/ping", headers={INTERNAL_TOKEN_HEADER: TOKEN})

        assert response.status_code == 404
        assert application.state.procrastinate.deferred == []

    async def test_staging_mounts_it_behind_the_token(
        self, settings_factory: Callable[..., Settings], make_client: Callable[[FastAPI], Any]
    ) -> None:
        """Deliberate: the iteration's staging acceptance step curls it. Hence the token."""
        application = _app(_deployed(settings_factory, "staging"))
        async with make_client(application) as client:
            accepted = await client.post("/internal/ping", headers={INTERNAL_TOKEN_HEADER: TOKEN})
            rejected = await client.post("/internal/ping")

        assert accepted.status_code == 200
        assert rejected.status_code == 404
        assert application.state.procrastinate.deferred == [PING_TASK]
