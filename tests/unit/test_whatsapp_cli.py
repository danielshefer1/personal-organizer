from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from personal_organizer.observability.redaction import redact_event
from personal_organizer.providers.channel.whatsapp.cli import check, sender_hash, simulate
from personal_organizer.providers.channel.whatsapp.parser import parse_webhook
from personal_organizer.providers.channel.whatsapp.signature import verify_signature
from personal_organizer.settings import Settings
from tests.api.test_whatsapp_webhook import WHATSAPP_ENV
from tests.fixtures.payloads import APP_SECRET, PHONE_NUMBER_ID


@pytest.fixture
def whatsapp_settings(settings_factory: Callable[..., Settings]) -> Settings:
    return settings_factory(**WHATSAPP_ENV)


def _client(respond: Callable[[httpx.Request], httpx.Response]) -> tuple[httpx.Client, list[Any]]:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return respond(request)

    return httpx.Client(transport=httpx.MockTransport(handler)), seen


class TestSimulate:
    def test_it_posts_what_meta_would_signed_as_meta_would(
        self, whatsapp_settings: Settings
    ) -> None:
        client, seen = _client(lambda _r: httpx.Response(200))
        code = simulate(
            whatsapp_settings,
            url="http://api/webhooks/whatsapp",
            from_="+31 6 1234 5678",
            text="hi",
            replay=3,
            client=client,
        )
        assert code == 0
        assert len(seen) == 3
        assert len({request.content for request in seen}) == 1, "replays must be identical"

        request = seen[0]
        assert verify_signature(APP_SECRET, request.content, request.headers["x-hub-signature-256"])
        batch = parse_webhook(json.loads(request.content), phone_number_id=PHONE_NUMBER_ID)
        (message,) = batch.messages
        assert message.sender.phone == "+31612345678"
        assert message.text == "hi"

    def test_a_non_200_is_a_failing_exit_code(self, whatsapp_settings: Settings) -> None:
        client, _ = _client(lambda _r: httpx.Response(401))
        code = simulate(
            whatsapp_settings,
            url="http://x",
            from_="+31612345678",
            text="hi",
            replay=1,
            client=client,
        )
        assert code == 1

    def test_it_refuses_without_a_secret(self, settings: Settings) -> None:
        client, seen = _client(lambda _r: httpx.Response(200))
        code = simulate(
            settings, url="http://x", from_="+31612345678", text="hi", replay=1, client=client
        )
        assert code == 2
        assert seen == []


class TestHash:
    def test_it_matches_the_sender_hash_in_the_logs(self, settings: Settings) -> None:
        """What ``inbound.handled`` logs is ``sender=<sender_key>``, hashed by the redactor."""
        pepper = settings.logging.pii_pepper.get_secret_value()
        logged = redact_event({"sender": "tel:+31612345678"}, pepper=pepper)["sender_hash"]
        assert sender_hash(settings, "0031 6 1234 5678") == logged

    def test_a_national_number_is_refused(self, settings: Settings) -> None:
        assert sender_hash(settings, "0612345678") is None


class TestCheck:
    def test_ok(self, whatsapp_settings: Settings, capsys: pytest.CaptureFixture[str]) -> None:
        client, seen = _client(
            lambda _r: httpx.Response(
                200,
                json={
                    "verified_name": "Organizer",
                    "display_phone_number": "15550783881",
                    "quality_rating": "GREEN",
                },
            )
        )
        assert check(whatsapp_settings, client=client) == 0
        assert seen[0].headers["authorization"] == "Bearer graph-token"
        assert str(seen[0].url).startswith(f"https://graph.facebook.com/v24.0/{PHONE_NUMBER_ID}?")
        assert "Organizer" in capsys.readouterr().out

    def test_an_expired_token(
        self, whatsapp_settings: Settings, capsys: pytest.CaptureFixture[str]
    ) -> None:
        client, _ = _client(lambda _r: httpx.Response(401, json={"error": {"code": 190}}))
        assert check(whatsapp_settings, client=client) == 1
        out = capsys.readouterr().out
        assert "190" in out
        assert "graph-token" not in out
