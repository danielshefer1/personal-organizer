from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx
import pytest

from personal_organizer.providers.channel.gowa.status import (
    GatewayHealth,
    Unhealthy,
    check_status,
    check_status_sync,
)
from personal_organizer.settings import Settings
from tests.api.test_gowa_webhook import GOWA_ENV

Respond = Callable[[httpx.Request], httpx.Response]


@pytest.fixture
def gowa_settings(settings_factory: Callable[..., Settings]) -> Settings:
    return settings_factory(**GOWA_ENV)


def status_body(*, connected: Any, logged_in: Any) -> Respond:
    return lambda _r: httpx.Response(
        200,
        json={"code": "SUCCESS", "results": {"is_connected": connected, "is_logged_in": logged_in}},
    )


def refuse(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("refused", request=request)


async def _check(settings: Settings, respond: Respond) -> tuple[GatewayHealth, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return respond(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        return await check_status(client, settings.gowa), seen


class TestCheckStatus:
    async def test_connected_and_logged_in_is_healthy(self, gowa_settings: Settings) -> None:
        health, _ = await _check(gowa_settings, status_body(connected=True, logged_in=True))
        assert health.ok
        assert health == GatewayHealth(None, connected=True, logged_in=True)

    async def test_it_asks_the_status_endpoint_with_basic_auth(
        self, gowa_settings: Settings
    ) -> None:
        _, (request,) = await _check(gowa_settings, status_body(connected=True, logged_in=True))
        assert request.method == "GET"
        assert str(request.url) == "http://localhost:3000/app/status"
        assert request.headers["authorization"].startswith("Basic ")
        assert "x-device-id" not in request.headers

    async def test_a_device_id_is_sent_when_configured(
        self, settings_factory: Callable[..., Settings]
    ) -> None:
        settings = settings_factory(**GOWA_ENV, GOWA__DEVICE_ID="po-bot")
        _, (request,) = await _check(settings, status_body(connected=True, logged_in=True))
        assert request.headers["x-device-id"] == "po-bot"

    async def test_logged_out(self, gowa_settings: Settings) -> None:
        health, _ = await _check(gowa_settings, status_body(connected=True, logged_in=False))
        assert health.reason is Unhealthy.NOT_LOGGED_IN
        assert not health.ok

    async def test_reconnecting(self, gowa_settings: Settings) -> None:
        health, _ = await _check(gowa_settings, status_body(connected=False, logged_in=True))
        assert health.reason is Unhealthy.NOT_CONNECTED

    async def test_logged_out_wins_over_disconnected(self, gowa_settings: Settings) -> None:
        """Only one of the two needs a person with the bot's phone in hand."""
        health, _ = await _check(gowa_settings, status_body(connected=False, logged_in=False))
        assert health.reason is Unhealthy.NOT_LOGGED_IN

    async def test_unreachable_names_the_error_class_only(self, gowa_settings: Settings) -> None:
        health, _ = await _check(gowa_settings, refuse)
        assert health.reason is Unhealthy.UNREACHABLE
        assert health.error_type == "ConnectError"

    async def test_a_timeout_is_unreachable(self, gowa_settings: Settings) -> None:
        def slow(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("slow", request=request)

        health, _ = await _check(gowa_settings, slow)
        assert health.reason is Unhealthy.UNREACHABLE
        assert health.error_type == "ReadTimeout"

    async def test_a_wrong_password_is_a_bad_response(self, gowa_settings: Settings) -> None:
        health, _ = await _check(gowa_settings, lambda _r: httpx.Response(401))
        assert health.reason is Unhealthy.BAD_RESPONSE
        assert health.status_code == 401

    @pytest.mark.parametrize(
        "respond",
        [
            lambda _r: httpx.Response(200, text="<html>login</html>"),
            lambda _r: httpx.Response(200, json={"results": None}),
            lambda _r: httpx.Response(200, json={"results": {"is_connected": True}}),
            lambda _r: httpx.Response(200, json=["not", "an", "object"]),
        ],
    )
    async def test_garbage_is_a_bad_response(
        self, gowa_settings: Settings, respond: Respond
    ) -> None:
        health, _ = await _check(gowa_settings, respond)
        assert health.reason is Unhealthy.BAD_RESPONSE
        assert health.status_code is None

    async def test_strings_are_not_booleans(self, gowa_settings: Settings) -> None:
        """``"false"`` is truthy: reading it loosely would report a logged-out gateway as
        healthy, which is the one failure this check exists to catch."""
        health, _ = await _check(gowa_settings, status_body(connected="true", logged_in="false"))
        assert health.reason is Unhealthy.BAD_RESPONSE

    async def test_it_refuses_without_credentials(self, settings: Settings) -> None:
        with pytest.raises(ValueError, match="GOWA__BASIC_AUTH_USER"):
            await _check(settings, status_body(connected=True, logged_in=True))


class TestCheckStatusSync:
    def test_it_sends_the_same_request_and_reads_the_same_answer(
        self, gowa_settings: Settings
    ) -> None:
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return status_body(connected=True, logged_in=False)(request)

        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            health = check_status_sync(client, gowa_settings.gowa)
        assert health.reason is Unhealthy.NOT_LOGGED_IN
        (request,) = seen
        assert str(request.url) == "http://localhost:3000/app/status"
        assert request.headers["authorization"].startswith("Basic ")

    def test_unreachable(self, gowa_settings: Settings) -> None:
        with httpx.Client(transport=httpx.MockTransport(refuse)) as client:
            health = check_status_sync(client, gowa_settings.gowa)
        assert health.reason is Unhealthy.UNREACHABLE
