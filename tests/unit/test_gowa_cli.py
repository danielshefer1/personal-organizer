from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from personal_organizer.providers.channel.gowa.cli import main, simulate, status
from personal_organizer.providers.channel.gowa.parser import parse_webhook
from personal_organizer.providers.channel.hmac_sha256 import verify_signature
from personal_organizer.providers.channel.whatsapp.cli import sender_hash
from personal_organizer.settings import Settings
from tests.api.test_gowa_webhook import GOWA_ENV
from tests.fixtures.gowa_payloads import WEBHOOK_SECRET


@pytest.fixture
def gowa_settings(settings_factory: Callable[..., Settings]) -> Settings:
    return settings_factory(**GOWA_ENV, GOWA__DEVICE_ID="po-bot")


def _client(respond: Callable[[httpx.Request], httpx.Response]) -> tuple[httpx.Client, list[Any]]:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return respond(request)

    return httpx.Client(transport=httpx.MockTransport(handler)), seen


class TestSimulate:
    def test_it_posts_what_the_gateway_would_signed_as_it_would(
        self, gowa_settings: Settings
    ) -> None:
        client, seen = _client(lambda _r: httpx.Response(200))
        code = simulate(
            gowa_settings,
            url="http://api/webhooks/gowa",
            from_="+31 6 1234 5678",
            text="hi",
            replay=3,
            client=client,
        )
        assert code == 0
        assert len(seen) == 3
        assert len({request.content for request in seen}) == 1, "replays must be identical"

        request = seen[0]
        assert verify_signature(
            WEBHOOK_SECRET, request.content, request.headers["x-hub-signature-256"]
        )
        (message,) = parse_webhook(json.loads(request.content), device_id="po-bot").messages
        assert message.sender.phone == "+31612345678"
        assert message.text == "hi"

    def test_a_non_200_is_a_failing_exit_code(self, gowa_settings: Settings) -> None:
        client, _ = _client(lambda _r: httpx.Response(401))
        code = simulate(
            gowa_settings,
            url="http://api",
            from_="+31612345678",
            text="hi",
            replay=1,
            client=client,
        )
        assert code == 1

    def test_it_refuses_without_a_secret(self, settings: Settings) -> None:
        client, seen = _client(lambda _r: httpx.Response(200))
        code = simulate(
            settings, url="http://api", from_="+31612345678", text="hi", replay=1, client=client
        )
        assert code == 2
        assert seen == []


class TestStatus:
    @staticmethod
    def _status(connected: bool, logged_in: bool) -> Callable[[httpx.Request], httpx.Response]:
        return lambda _r: httpx.Response(
            200,
            json={
                "code": "SUCCESS",
                "results": {"is_connected": connected, "is_logged_in": logged_in},
            },
        )

    def test_logged_in(self, gowa_settings: Settings, capsys: Any) -> None:
        client, seen = _client(self._status(True, True))
        assert status(gowa_settings, client=client) == 0
        (request,) = seen
        assert str(request.url) == "http://localhost:3000/app/status"
        assert request.headers["authorization"].startswith("Basic ")
        assert request.headers["x-device-id"] == "po-bot"
        assert "connected=True logged_in=True" in capsys.readouterr().out

    def test_logged_out_says_what_to_do(self, gowa_settings: Settings, capsys: Any) -> None:
        client, _ = _client(self._status(True, False))
        assert status(gowa_settings, client=client) == 1
        assert "scan the QR code" in capsys.readouterr().out

    def test_unreachable(self, gowa_settings: Settings) -> None:
        def refuse(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused", request=request)

        client, _ = _client(refuse)
        assert status(gowa_settings, client=client) == 1

    def test_garbage(self, gowa_settings: Settings) -> None:
        client, _ = _client(lambda _r: httpx.Response(200, text="<html>"))
        assert status(gowa_settings, client=client) == 1


def test_hash_matches_the_meta_cli(gowa_settings: Settings, capsys: Any, monkeypatch: Any) -> None:
    """A person's ``sender_hash`` is the same on every channel."""
    monkeypatch.setattr(
        "personal_organizer.providers.channel.gowa.cli.get_settings", lambda: gowa_settings
    )
    assert main(["hash", "+31612345678"]) == 0
    assert (
        capsys.readouterr().out.strip()
        == f"sender_hash={sender_hash(gowa_settings, '+31612345678')}"
    )


class TestStatusSharesTheHealthCheck:
    """``status`` and the worker's ``system:gowa_health`` read the gateway through one
    function, so the operator's check and the alert cannot disagree."""

    def test_a_wrong_password_names_the_status(self, gowa_settings: Settings, capsys: Any) -> None:
        client, _ = _client(lambda _r: httpx.Response(401))
        assert status(gowa_settings, client=client) == 1
        assert "failed: HTTP 401" in capsys.readouterr().out

    def test_reconnecting_is_a_failing_exit_code(
        self, gowa_settings: Settings, capsys: Any
    ) -> None:
        client, _ = _client(TestStatus._status(False, True))
        assert status(gowa_settings, client=client) == 1
        out = capsys.readouterr().out
        assert "connected=False logged_in=True" in out
        assert "scan the QR code" not in out

    def test_strings_are_not_booleans(self, gowa_settings: Settings, capsys: Any) -> None:
        client, _ = _client(
            lambda _r: httpx.Response(
                200, json={"results": {"is_connected": "true", "is_logged_in": "false"}}
            )
        )
        assert status(gowa_settings, client=client) == 1
        assert "unexpected response" in capsys.readouterr().out
