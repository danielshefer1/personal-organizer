"""``/webhooks/whatsapp`` with a fake store: every rejection path, and what each answers.

The real persist-and-defer path, against Postgres, is ``tests/db/test_ingress_dedupe.py``.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient

from personal_organizer.api.app import create_app
from personal_organizer.providers.channel.hmac_sha256 import sign
from personal_organizer.settings import Settings
from tests.api.conftest import FakeDatabase, FakeIngressStore, FakeProcrastinate
from tests.fixtures.payloads import (
    APP_SECRET,
    PHONE_NUMBER_ID,
    encode,
    load,
    signed,
    text_message,
)

VERIFY_TOKEN = "f" * 64
URL = "/webhooks/whatsapp"

WHATSAPP_ENV = {
    "WHATSAPP__ENABLED": "true",
    "WHATSAPP__APP_SECRET": APP_SECRET.decode(),
    "WHATSAPP__VERIFY_TOKEN": VERIFY_TOKEN,
    "WHATSAPP__ACCESS_TOKEN": "graph-token",
    "WHATSAPP__PHONE_NUMBER_ID": PHONE_NUMBER_ID,
}


def _app(settings: Settings, store: FakeIngressStore | None = None) -> FastAPI:
    application = create_app(settings)
    application.state.db = FakeDatabase()
    application.state.procrastinate = FakeProcrastinate()
    application.state.ingress_store = store or FakeIngressStore()
    return application


@pytest.fixture
def whatsapp_settings(settings_factory: Callable[..., Settings]) -> Settings:
    return settings_factory(**WHATSAPP_ENV)


@pytest.fixture
def store() -> FakeIngressStore:
    return FakeIngressStore()


@pytest.fixture
def webhook_app(whatsapp_settings: Settings, store: FakeIngressStore) -> FastAPI:
    return _app(whatsapp_settings, store)


@pytest.fixture
async def http(webhook_app: FastAPI, make_client: Callable[[FastAPI], AsyncClient]) -> Any:
    async with make_client(webhook_app) as client:
        yield client


class TestHandshake:
    async def test_the_right_token_echoes_the_challenge(self, http: AsyncClient) -> None:
        response = await http.get(
            URL,
            params={
                "hub.mode": "subscribe",
                "hub.verify_token": VERIFY_TOKEN,
                "hub.challenge": "1158201444",
            },
        )
        assert response.status_code == 200
        assert response.text == "1158201444"
        assert response.headers["content-type"].startswith("text/plain")

    @pytest.mark.parametrize(
        "params",
        [
            pytest.param(
                {"hub.mode": "subscribe", "hub.verify_token": "wrong", "hub.challenge": "1"},
                id="wrong-token",
            ),
            pytest.param(
                {"hub.mode": "unsubscribe", "hub.verify_token": VERIFY_TOKEN, "hub.challenge": "1"},
                id="wrong-mode",
            ),
            pytest.param({"hub.mode": "subscribe", "hub.challenge": "1"}, id="no-token"),
            pytest.param(
                {"hub.mode": "subscribe", "hub.verify_token": VERIFY_TOKEN}, id="no-challenge"
            ),
            pytest.param({}, id="nothing"),
            pytest.param(
                {
                    "hub.mode": "subscribe",
                    "hub.verify_token": VERIFY_TOKEN,
                    "hub.challenge": "x" * 129,
                },
                id="oversized-challenge",
            ),
            pytest.param(
                {"hub.mode": "subscribe", "hub.verify_token": "caf\xe9", "hub.challenge": "1"},
                id="non-ascii-token",
            ),
        ],
    )
    async def test_everything_else_is_403_never_422(
        self, http: AsyncClient, params: dict[str, str]
    ) -> None:
        response = await http.get(URL, params=params)
        assert response.status_code == 403
        assert response.text == ""


class TestSignature:
    """Done-When: an unsigned or wrongly signed POST is rejected -- and stores nothing."""

    @pytest.mark.parametrize(
        "header",
        [
            pytest.param(None, id="unsigned"),
            pytest.param("", id="empty"),
            pytest.param("sha256=" + "0" * 64, id="wrong-hex"),
            pytest.param(
                lambda body: sign(APP_SECRET, body).removeprefix("sha256="), id="no-prefix"
            ),
            pytest.param(lambda body: sign(b"another-secret", body), id="other-secret"),
            pytest.param(lambda body: sign(APP_SECRET, body + b" "), id="other-body"),
            pytest.param("sha256=caf\xe9" + "0" * 60, id="non-ascii"),
        ],
    )
    async def test_a_bad_signature_is_401_and_reaches_nothing(
        self, http: AsyncClient, store: FakeIngressStore, header: Any
    ) -> None:
        body = encode(text_message())
        headers = {"content-type": "application/json"}
        if header is not None:
            value = header(body) if callable(header) else header
            headers["x-hub-signature-256"] = (
                value.encode("latin-1") if not value.isascii() else value
            )
        response = await http.post(URL, content=body, headers=headers)
        assert response.status_code == 401
        assert store.calls == []

    async def test_a_body_changed_after_signing_is_rejected(
        self, http: AsyncClient, store: FakeIngressStore
    ) -> None:
        body, headers = signed(text_message())
        tampered = body.replace(b"hello", b"hellO")
        response = await http.post(URL, content=tampered, headers=headers)
        assert response.status_code == 401
        assert store.calls == []

    async def test_the_signature_is_over_the_bytes_as_sent(
        self, http: AsyncClient, store: FakeIngressStore
    ) -> None:
        """Meta's serialisation is not canonical; a re-serialised parse would not verify."""
        body = b'{"object" : "whatsapp_business_account",\n "entry":[]}'
        response = await http.post(
            URL, content=body, headers={"x-hub-signature-256": sign(APP_SECRET, body)}
        )
        assert response.status_code == 200


class TestIngress:
    async def test_a_signed_message_is_recorded_and_acked(
        self, http: AsyncClient, store: FakeIngressStore
    ) -> None:
        body, headers = signed(text_message(wamid="wamid.A"))
        response = await http.post(URL, content=body, headers=headers)
        assert response.status_code == 200
        assert response.content == b""
        ((channel, batch),) = store.calls
        assert channel == "whatsapp"
        assert [m.provider_message_id for m in batch.messages] == ["wamid.A"]

    async def test_replaying_ten_times_is_forwarded_ten_times_and_stored_once(
        self, http: AsyncClient, store: FakeIngressStore
    ) -> None:
        """The router does not deduplicate; the store does. The database version of this --
        one row, one job -- is the Done-When test in tests/db/test_ingress_dedupe.py."""
        body, headers = signed(text_message(wamid="wamid.REPLAY"))
        for _ in range(10):
            response = await http.post(URL, content=body, headers=headers)
            assert response.status_code == 200
        assert len(store.calls) == 10
        assert store.seen == {("whatsapp", "wamid.REPLAY")}

    async def test_statuses_are_recorded(self, http: AsyncClient, store: FakeIngressStore) -> None:
        body, headers = signed(load("statuses"))
        assert (await http.post(URL, content=body, headers=headers)).status_code == 200
        ((_, batch),) = store.calls
        assert len(batch.updates) == 4

    async def test_signed_but_unparseable_json_is_200_and_stores_nothing(
        self, http: AsyncClient, store: FakeIngressStore
    ) -> None:
        """A redelivery cannot fix it, and a non-200 earns seven days of them."""
        body = b"{not json"
        response = await http.post(
            URL, content=body, headers={"x-hub-signature-256": sign(APP_SECRET, body)}
        )
        assert response.status_code == 200
        assert store.calls == []

    async def test_a_payload_for_another_number_is_200_and_skipped(
        self, http: AsyncClient, store: FakeIngressStore
    ) -> None:
        payload = text_message()
        payload["entry"][0]["changes"][0]["value"]["metadata"]["phone_number_id"] = "999"
        body, headers = signed(payload)
        assert (await http.post(URL, content=body, headers=headers)).status_code == 200
        ((_, batch),) = store.calls
        assert batch.messages == ()
        assert batch.skipped == 1

    async def test_a_database_failure_is_500_so_meta_redelivers(
        self, whatsapp_settings: Settings, make_client: Callable[[FastAPI], AsyncClient]
    ) -> None:
        """Nothing committed, so a redelivery is exactly right."""
        application = _app(whatsapp_settings, FakeIngressStore(fail=True))
        body, headers = signed(text_message())
        async with make_client(application) as client:
            response = await client.post(URL, content=body, headers=headers)
        assert response.status_code == 500

    async def test_an_oversized_body_is_413_before_any_verification(
        self, http: AsyncClient, store: FakeIngressStore
    ) -> None:
        response = await http.post(URL, content=b"x" * (1024 * 1024 + 1))
        assert response.status_code == 413
        assert store.calls == []


class TestMounting:
    async def test_disabled_means_not_mounted(
        self, settings: Settings, make_client: Callable[[FastAPI], AsyncClient]
    ) -> None:
        """Staging deploys before the Meta app exists. Until then there is no route to
        attack at all -- a 404, with a correctly signed body, proves absence not rejection."""
        store = FakeIngressStore()
        application = _app(settings, store)
        body, headers = signed(text_message())
        async with make_client(application) as client:
            post = await client.post(URL, content=body, headers=headers)
            get = await client.get(
                URL,
                params={
                    "hub.mode": "subscribe",
                    "hub.verify_token": VERIFY_TOKEN,
                    "hub.challenge": "1",
                },
            )
        assert post.status_code == 404
        assert get.status_code == 404
        assert store.calls == []

    async def test_mounted_in_production_when_enabled(
        self,
        settings_factory: Callable[..., Settings],
        make_client: Callable[[FastAPI], AsyncClient],
    ) -> None:
        cfg = settings_factory(
            APP__ENV="production",
            SENTRY__DSN="https://k@o.ingest.sentry.io/1",
            LOGGING__PII_PEPPER="a-real-secret",
            **WHATSAPP_ENV,
        )
        application = create_app(cfg)
        application.state.ingress_store = FakeIngressStore()
        body, headers = signed(text_message())
        async with make_client(application) as client:
            assert (await client.post(URL, content=body, headers=headers)).status_code == 200
