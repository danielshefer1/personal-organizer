"""``/webhooks/gowa`` with a fake store, and living beside ``/webhooks/whatsapp``.

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
from tests.api.test_whatsapp_webhook import WHATSAPP_ENV
from tests.fixtures import gowa_payloads as gowa
from tests.fixtures import payloads as meta

URL = "/webhooks/gowa"
META_URL = "/webhooks/whatsapp"

GOWA_ENV = {
    "GOWA__ENABLED": "true",
    "GOWA__BASIC_AUTH_USER": "po",
    "GOWA__BASIC_AUTH_PASSWORD": "gateway-password",
    "GOWA__WEBHOOK_SECRET": gowa.WEBHOOK_SECRET.decode(),
}


def _app(settings: Settings, store: FakeIngressStore | None = None) -> FastAPI:
    application = create_app(settings)
    application.state.db = FakeDatabase()
    application.state.procrastinate = FakeProcrastinate()
    application.state.ingress_store = store or FakeIngressStore()
    return application


@pytest.fixture
def gowa_settings(settings_factory: Callable[..., Settings]) -> Settings:
    return settings_factory(**GOWA_ENV)


@pytest.fixture
def store() -> FakeIngressStore:
    return FakeIngressStore()


@pytest.fixture
async def http(
    gowa_settings: Settings,
    store: FakeIngressStore,
    make_client: Callable[[FastAPI], AsyncClient],
) -> Any:
    async with make_client(_app(gowa_settings, store)) as client:
        yield client


class TestSignature:
    async def test_an_unsigned_body_is_401_and_reaches_nothing(
        self, http: AsyncClient, store: FakeIngressStore
    ) -> None:
        response = await http.post(URL, content=meta.encode(gowa.text_message()))
        assert response.status_code == 401
        assert store.calls == []

    async def test_the_gateways_default_secret_does_not_verify(
        self, http: AsyncClient, store: FakeIngressStore
    ) -> None:
        body, headers = gowa.signed(gowa.text_message(), secret=b"secret")
        assert (await http.post(URL, content=body, headers=headers)).status_code == 401
        assert store.calls == []


class TestIngress:
    async def test_a_signed_message_is_recorded_and_acked(
        self, http: AsyncClient, store: FakeIngressStore
    ) -> None:
        body, headers = gowa.signed(gowa.text_message(message_id="3EB0A"))
        response = await http.post(URL, content=body, headers=headers)
        assert response.status_code == 200
        ((channel, batch),) = store.calls
        assert channel == "gowa"
        assert [m.provider_message_id for m in batch.messages] == ["3EB0A"]

    async def test_replaying_ten_times_is_stored_once(
        self, http: AsyncClient, store: FakeIngressStore
    ) -> None:
        body, headers = gowa.signed(gowa.text_message(message_id="3EB0REPLAY"))
        for _ in range(10):
            assert (await http.post(URL, content=body, headers=headers)).status_code == 200
        assert store.seen == {("gowa", "3EB0REPLAY")}

    async def test_receipts_are_recorded(self, http: AsyncClient, store: FakeIngressStore) -> None:
        body, headers = gowa.signed(gowa.load("ack_delivered"))
        assert (await http.post(URL, content=body, headers=headers)).status_code == 200
        ((_, batch),) = store.calls
        assert len(batch.updates) == 2

    async def test_signed_but_unparseable_json_is_200_and_stores_nothing(
        self, http: AsyncClient, store: FakeIngressStore
    ) -> None:
        body = b"{not json"
        response = await http.post(
            URL, content=body, headers={"x-hub-signature-256": sign(gowa.WEBHOOK_SECRET, body)}
        )
        assert response.status_code == 200
        assert store.calls == []

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
        store = FakeIngressStore()
        body, headers = gowa.signed(gowa.text_message())
        async with make_client(_app(settings, store)) as client:
            assert (await client.post(URL, content=body, headers=headers)).status_code == 404
        assert store.calls == []

    async def test_gowa_alone_does_not_mount_meta(
        self, http: AsyncClient, store: FakeIngressStore
    ) -> None:
        body, headers = meta.signed(meta.text_message())
        assert (await http.post(META_URL, content=body, headers=headers)).status_code == 404


class TestBothProviders:
    """Meta and the gateway at once: each route has its own channel and its own secret."""

    @pytest.fixture
    async def both(
        self,
        settings_factory: Callable[..., Settings],
        store: FakeIngressStore,
        make_client: Callable[[FastAPI], AsyncClient],
    ) -> Any:
        cfg = settings_factory(**WHATSAPP_ENV, **GOWA_ENV)
        async with make_client(_app(cfg, store)) as client:
            yield client

    async def test_each_route_records_under_its_own_channel(
        self, both: AsyncClient, store: FakeIngressStore
    ) -> None:
        body, headers = meta.signed(meta.text_message(wamid="wamid.M"))
        assert (await both.post(META_URL, content=body, headers=headers)).status_code == 200
        body, headers = gowa.signed(gowa.text_message(message_id="3EB0G"))
        assert (await both.post(URL, content=body, headers=headers)).status_code == 200

        assert store.seen == {("whatsapp", "wamid.M"), ("gowa", "3EB0G")}

    async def test_a_body_signed_for_one_is_refused_by_the_other(
        self, both: AsyncClient, store: FakeIngressStore
    ) -> None:
        body, headers = gowa.signed(gowa.text_message())
        assert (await both.post(META_URL, content=body, headers=headers)).status_code == 401
        body, headers = meta.signed(meta.text_message())
        assert (await both.post(URL, content=body, headers=headers)).status_code == 401
        assert store.calls == []
