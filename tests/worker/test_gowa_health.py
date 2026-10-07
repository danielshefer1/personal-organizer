"""``system:gowa_health``: a logged-out linked device must never be silent."""

from __future__ import annotations

import io
import json
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

import httpx
import procrastinate
import pytest
import sentry_sdk
from procrastinate.testing import InMemoryConnector
from sentry_sdk.integrations.logging import LoggingIntegration
from sentry_sdk.types import Event, Hint

from personal_organizer.observability.logging import configure_logging
from personal_organizer.settings import Settings
from personal_organizer.worker.app import build_procrastinate_app
from personal_organizer.worker.tasks.gowa_health import (
    GOWA_HEALTH_TASK,
    GOWA_HEALTH_TIMEOUT,
    SENTRY_FINGERPRINT,
    register,
    run_health_check,
)
from tests.api.test_gowa_webhook import GOWA_ENV

Respond = Callable[[httpx.Request], httpx.Response]


@pytest.fixture
def gowa_settings(settings_factory: Callable[..., Settings]) -> Settings:
    return settings_factory(**GOWA_ENV, LOGGING__LEVEL="DEBUG", LOGGING__RENDERER="json")


@pytest.fixture
def log_stream(gowa_settings: Settings) -> io.StringIO:
    """The real processor chain, redaction included, writing to a buffer."""
    stream = io.StringIO()
    configure_logging(gowa_settings, stream=stream)
    return stream


def _lines(stream: io.StringIO) -> list[dict[str, Any]]:
    return [json.loads(line) for line in stream.getvalue().splitlines() if line.strip()]


@contextmanager
def sentry_events() -> Iterator[list[Event]]:
    """A real Sentry client whose ``before_send`` keeps each event and drops it.

    Its logging integration is off, as ``init_sentry`` turns it off in production, so the
    only events counted are ones the code captures explicitly.
    """
    events: list[Event] = []

    def keep(event: Event, _hint: Hint) -> Event | None:
        events.append(event)
        return None

    client = sentry_sdk.Client(
        dsn="https://k@o.ingest.sentry.io/1",
        before_send=keep,
        integrations=[LoggingIntegration(level=None, event_level=None)],
    )
    with sentry_sdk.new_scope() as scope:
        scope.set_client(client)
        yield events
    client.close()


def status(*, connected: bool, logged_in: bool) -> Respond:
    return lambda _r: httpx.Response(
        200, json={"results": {"is_connected": connected, "is_logged_in": logged_in}}
    )


def refuse(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("refused", request=request)


async def _run(settings: Settings, respond: Respond) -> None:
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        await run_health_check(settings.gowa, client)


class TestRegistration:
    def test_registered_on_a_worker_with_the_gateway(
        self, settings_factory: Callable[..., Settings]
    ) -> None:
        settings = settings_factory(**GOWA_ENV)
        app = build_procrastinate_app(settings)
        assert GOWA_HEALTH_TASK in app.tasks
        periodic = app.periodic_registry.periodic_tasks[(GOWA_HEALTH_TASK, "")]
        assert periodic.cron == "*/5 * * * *"

    def test_not_registered_without_the_gateway(self, settings: Settings) -> None:
        """A worker with no gateway would alarm every five minutes about nothing."""
        app = build_procrastinate_app(settings)
        assert GOWA_HEALTH_TASK not in app.tasks
        assert all(name != GOWA_HEALTH_TASK for name, _ in app.periodic_registry.periodic_tasks)

    def test_it_runs_on_a_queue_the_worker_subscribes_to(
        self, settings_factory: Callable[..., Settings]
    ) -> None:
        """Otherwise the job is deferred every five minutes and nothing ever runs it."""
        settings = settings_factory(**GOWA_ENV)
        app = build_procrastinate_app(settings)
        assert app.tasks[GOWA_HEALTH_TASK].queue in settings.worker.queues

    def test_runs_never_pile_up(self, settings_factory: Callable[..., Settings]) -> None:
        app = build_procrastinate_app(settings_factory(**GOWA_ENV))
        assert app.tasks[GOWA_HEALTH_TASK].queueing_lock == GOWA_HEALTH_TASK


class TestTheTask:
    async def test_the_registered_task_asks_the_gateway(self, gowa_settings: Settings) -> None:
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return status(connected=True, logged_in=True)(request)

        app = procrastinate.App(connector=InMemoryConnector())
        register(app, gowa_settings.gowa, transport=httpx.MockTransport(handler))
        await app.tasks[GOWA_HEALTH_TASK](timestamp=0)

        (request,) = seen
        assert str(request.url) == "http://localhost:3000/app/status"

    def test_a_hung_gateway_cannot_outlast_the_period(self) -> None:
        """Every phase of the request is bounded, far inside the five minutes between runs,
        so a gateway that accepts and never answers ends one run as `unreachable`."""
        phases = (
            GOWA_HEALTH_TIMEOUT.connect,
            GOWA_HEALTH_TIMEOUT.read,
            GOWA_HEALTH_TIMEOUT.write,
            GOWA_HEALTH_TIMEOUT.pool,
        )
        assert all(phase is not None and phase <= 30 for phase in phases)

    async def test_without_sentry_it_still_logs_and_does_not_raise(
        self, gowa_settings: Settings, log_stream: io.StringIO
    ) -> None:
        """Local and CI have no DSN: capture_message must be a quiet no-op there."""
        with sentry_sdk.new_scope() as scope:
            scope.set_client(None)
            await _run(gowa_settings, refuse)
        assert "gowa.unhealthy" in log_stream.getvalue()


class TestUnhealthy:
    @pytest.mark.parametrize(
        ("respond", "reason"),
        [
            (refuse, "unreachable"),
            (status(connected=True, logged_in=False), "not_logged_in"),
            (status(connected=False, logged_in=True), "not_connected"),
            (lambda _r: httpx.Response(401), "bad_response"),
            (lambda _r: httpx.Response(200, text="<html>"), "bad_response"),
        ],
    )
    async def test_logs_at_error_with_the_reason(
        self, gowa_settings: Settings, log_stream: io.StringIO, respond: Respond, reason: str
    ) -> None:
        with sentry_events():
            await _run(gowa_settings, respond)
        (line,) = [entry for entry in _lines(log_stream) if entry["event"] == "gowa.unhealthy"]
        assert line["level"] == "error"
        assert line["reason"] == reason, "reason must survive redaction (SAFE_KEYS)"

    async def test_the_status_code_and_error_class_survive_redaction(
        self, gowa_settings: Settings, log_stream: io.StringIO
    ) -> None:
        with sentry_events():
            await _run(gowa_settings, lambda _r: httpx.Response(401))
            await _run(gowa_settings, refuse)
        lines = [entry for entry in _lines(log_stream) if entry["event"] == "gowa.unhealthy"]
        assert [line["status_code"] for line in lines] == [401, None]
        assert [line["error_type"] for line in lines] == [None, "ConnectError"]

    async def test_reaches_sentry_as_one_issue(self, gowa_settings: Settings) -> None:
        """The log line alone would not: init_sentry turns logging events off. One fixed
        fingerprint for every reason is what makes Sentry's issue the alert."""
        with sentry_events() as events:
            await _run(gowa_settings, refuse)
            await _run(gowa_settings, status(connected=True, logged_in=False))
        assert [event["message"] for event in events] == ["gowa.unhealthy"] * 2
        assert all(event["level"] == "error" for event in events)
        assert all(event["fingerprint"] == list(SENTRY_FINGERPRINT) for event in events)
        assert [event["tags"]["reason"] for event in events] == ["unreachable", "not_logged_in"]

    async def test_the_password_never_leaves(
        self, gowa_settings: Settings, log_stream: io.StringIO
    ) -> None:
        with sentry_events() as events:
            await _run(gowa_settings, lambda _r: httpx.Response(401))
        assert "gateway-password" not in log_stream.getvalue()
        assert "gateway-password" not in json.dumps(events, default=str)


class TestConfigurationErrors:
    """A malformed base URL or a non-Latin-1 header value make httpx raise before any request
    is sent (this httpx encodes basic-auth as UTF-8, so the non-Latin-1 case that bites is the
    device-id header). The alarm must still ring, naming the error's class and never its message
    (an ``InvalidURL`` message quotes the URL)."""

    @pytest.mark.parametrize(
        ("overrides", "error_type"),
        [
            ({"GOWA__BASE_URL": "http://bad host:3000/\x7f"}, "InvalidURL"),
            ({"GOWA__DEVICE_ID": "dév-€-secret"}, "UnicodeEncodeError"),
        ],
    )
    async def test_still_alarms_without_leaking_the_message(
        self,
        settings_factory: Callable[..., Settings],
        log_stream: io.StringIO,
        overrides: dict[str, str],
        error_type: str,
    ) -> None:
        settings = settings_factory(
            **{**GOWA_ENV, **overrides}, LOGGING__LEVEL="DEBUG", LOGGING__RENDERER="json"
        )
        with sentry_events() as events:
            await _run(settings, status(connected=True, logged_in=True))
        (line,) = [entry for entry in _lines(log_stream) if entry["event"] == "gowa.unhealthy"]
        assert line["level"] == "error"
        assert line["error_type"] == error_type
        (event,) = events
        assert event["message"] == "gowa.unhealthy"
        assert event["fingerprint"] == list(SENTRY_FINGERPRINT)
        for leaked in ("bad host", "secret", "dév"):
            assert leaked not in log_stream.getvalue()
            assert leaked not in json.dumps(events, default=str)


class TestHealthy:
    async def test_logs_at_debug_and_sends_nothing_to_sentry(
        self, gowa_settings: Settings, log_stream: io.StringIO
    ) -> None:
        with sentry_events() as events:
            await _run(gowa_settings, status(connected=True, logged_in=True))
        assert events == []
        ours = [line for line in _lines(log_stream) if line["event"].startswith("gowa.")]
        assert [(line["event"], line["level"]) for line in ours] == [("gowa.healthy", "debug")]
