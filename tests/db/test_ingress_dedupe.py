"""Done-When: replaying the same webhook ten times creates one job.

End to end over the real router, the real ingress store, the real Procrastinate app and
Postgres -- because what makes it true is a unique constraint and a transaction, and a fake
can only restate the claim.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import procrastinate
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr

from personal_organizer.api.app import create_app
from personal_organizer.db.dsn import normalise
from personal_organizer.db.roles import DatabaseRole
from personal_organizer.interfaces.channel import SenderRef
from personal_organizer.messaging.ingress import PgIngressStore, sender_lock
from personal_organizer.settings import Settings, WhatsAppSettings
from personal_organizer.worker.app import build_procrastinate_app
from personal_organizer.worker.queues import Queue
from personal_organizer.worker.tasks.channel import HANDLE_INBOUND_TASK
from tests.fixtures.payloads import (
    APP_SECRET,
    PHONE_NUMBER_ID,
    SENDER_PHONE,
    load,
    signed,
    text_message,
)

pytestmark = [pytest.mark.db, pytest.mark.usefixtures("clean_channel_tables")]

URL = "/webhooks/whatsapp"


@pytest.fixture
def whatsapp_db_settings(db_settings: Settings) -> Settings:
    return db_settings.model_copy(
        update={
            "whatsapp": WhatsAppSettings(
                enabled=True,
                app_secret=SecretStr(APP_SECRET.decode()),
                verify_token=SecretStr("v" * 64),
                access_token=SecretStr("graph-token"),
                phone_number_id=PHONE_NUMBER_ID,
            )
        }
    )


def _pepper(settings: Settings) -> str:
    return settings.logging.pii_pepper.get_secret_value()


@pytest.fixture
async def procrastinate_app(whatsapp_db_settings: Settings) -> AsyncIterator[procrastinate.App]:
    app = build_procrastinate_app(whatsapp_db_settings)
    async with app.open_async():
        yield app


def _webhook_app(settings: Settings, queue: procrastinate.App) -> FastAPI:
    application = create_app(settings)
    application.state.ingress_store = PgIngressStore(queue, pepper=_pepper(settings))
    return application


@pytest.fixture
async def http(
    whatsapp_db_settings: Settings, procrastinate_app: procrastinate.App
) -> AsyncIterator[AsyncClient]:
    application = _webhook_app(whatsapp_db_settings, procrastinate_app)
    transport = ASGITransport(app=application, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


async def _counts(conn: Any) -> tuple[int, int]:
    inbox = await conn.fetchval("SELECT count(*) FROM channel_inbox")
    jobs = await conn.fetchval("SELECT count(*) FROM procrastinate_jobs")
    return inbox, jobs


class TestReplayCreatesOneJob:
    async def test_ten_sequential_replays(self, http: AsyncClient, owner_conn: Any) -> None:
        body, headers = signed(text_message(wamid="wamid.REPLAY1"))
        for _ in range(10):
            assert (await http.post(URL, content=body, headers=headers)).status_code == 200
        assert await _counts(owner_conn) == (1, 1)

    async def test_ten_concurrent_replays(self, http: AsyncClient, owner_conn: Any) -> None:
        """Meta can deliver the same event on two connections at once. The second INSERT
        waits on the first's unique-index entry and then does nothing."""
        body, headers = signed(text_message(wamid="wamid.REPLAY2"))
        responses = await asyncio.gather(
            *(http.post(URL, content=body, headers=headers) for _ in range(10))
        )
        assert [r.status_code for r in responses] == [200] * 10
        assert await _counts(owner_conn) == (1, 1)

    async def test_the_job_is_the_right_one(
        self, http: AsyncClient, owner_conn: Any, whatsapp_db_settings: Settings
    ) -> None:
        body, headers = signed(text_message(wamid="wamid.JOB1", body="Oncology appointment"))
        await http.post(URL, content=body, headers=headers)

        inbox_id = await owner_conn.fetchval("SELECT id FROM channel_inbox")
        job = await owner_conn.fetchrow(
            "SELECT task_name, queue_name, lock, args FROM procrastinate_jobs"
        )
        assert job["task_name"] == HANDLE_INBOUND_TASK
        assert job["queue_name"] == Queue.WEBHOOKS.value
        # ADR 0001: the id, never the content.
        assert json.loads(job["args"]) == {"inbox_id": str(inbox_id)}
        expected_lock = sender_lock(
            "whatsapp", SenderRef(user_id=None, phone=SENDER_PHONE), _pepper(whatsapp_db_settings)
        )
        assert job["lock"] == expected_lock
        assert SENDER_PHONE.removeprefix("+") not in job["lock"]

    async def test_the_message_is_stored_normalised(
        self, http: AsyncClient, owner_conn: Any
    ) -> None:
        body, headers = signed(text_message(wamid="wamid.STORED1", body="hello"))
        await http.post(URL, content=body, headers=headers)
        row = await owner_conn.fetchrow("SELECT * FROM channel_inbox")
        assert row["channel"] == "whatsapp"
        assert row["sender_phone"] == SENDER_PHONE
        assert row["sender_key"] == f"tel:{SENDER_PHONE}"
        assert row["body"] == "hello"
        assert row["processed_at"] is None

    async def test_a_batch_defers_one_job_per_message(
        self, http: AsyncClient, owner_conn: Any
    ) -> None:
        body, headers = signed(load("multi"))
        await http.post(URL, content=body, headers=headers)
        await http.post(URL, content=body, headers=headers)
        assert await _counts(owner_conn) == (2, 2)

    async def test_a_nul_in_the_body_does_not_fail_the_insert(
        self, http: AsyncClient, owner_conn: Any
    ) -> None:
        """Postgres text and jsonb reject NUL. A 500 here would be redelivered, and fail the
        same way, for seven days."""
        body, headers = signed(text_message(wamid="wamid.NUL1", body="a\x00b"))
        assert (await http.post(URL, content=body, headers=headers)).status_code == 200
        assert await owner_conn.fetchval("SELECT body FROM channel_inbox") == "ab"


class TestAtomicity:
    async def test_a_failed_defer_leaves_no_inbox_row(
        self, whatsapp_db_settings: Settings, owner_conn: Any
    ) -> None:
        """Row exists if and only if its job exists. A queue app that does not know the task
        -- a drifted name -- fails the defer *after* the insert, inside the transaction."""
        conninfo, _ = normalise(whatsapp_db_settings.dsn_for(DatabaseRole.APP), "libpq")
        bare = procrastinate.App(connector=procrastinate.PsycopgConnector(conninfo=conninfo))
        async with bare.open_async():
            application = _webhook_app(whatsapp_db_settings, bare)
            transport = ASGITransport(app=application, raise_app_exceptions=False)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                body, headers = signed(text_message(wamid="wamid.ATOMIC1"))
                response = await client.post(URL, content=body, headers=headers)
        assert response.status_code == 500
        assert await _counts(owner_conn) == (0, 0)

    async def test_meta_redelivering_after_the_failure_then_succeeds(
        self, whatsapp_db_settings: Settings, http: AsyncClient, owner_conn: Any
    ) -> None:
        """Nothing half-written blocks the retry: the same id inserts cleanly next time."""
        conninfo, _ = normalise(whatsapp_db_settings.dsn_for(DatabaseRole.APP), "libpq")
        bare = procrastinate.App(connector=procrastinate.PsycopgConnector(conninfo=conninfo))
        body, headers = signed(text_message(wamid="wamid.ATOMIC2"))
        async with bare.open_async():
            application = _webhook_app(whatsapp_db_settings, bare)
            transport = ASGITransport(app=application, raise_app_exceptions=False)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                assert (await client.post(URL, content=body, headers=headers)).status_code == 500
        assert (await http.post(URL, content=body, headers=headers)).status_code == 200
        assert await _counts(owner_conn) == (1, 1)


class TestStatuses:
    async def _outbox(self, conn: Any, wamid: str, status: str) -> None:
        await conn.execute(
            "INSERT INTO channel_outbox (channel, kind, recipient_key, status, "
            "provider_message_id) VALUES ('whatsapp', 'ack', 'tel:+31612345678', $1, $2)",
            status,
            wamid,
        )

    async def _status(self, conn: Any, wamid: str) -> Any:
        return await conn.fetchrow(
            "SELECT status, error_code FROM channel_outbox WHERE provider_message_id = $1", wamid
        )

    async def test_statuses_move_the_outbox_forward(
        self, http: AsyncClient, owner_conn: Any
    ) -> None:
        await self._outbox(owner_conn, "wamid.OUT1", "accepted")
        await self._outbox(owner_conn, "wamid.OUT2", "accepted")
        body, headers = signed(load("statuses"))
        await http.post(URL, content=body, headers=headers)
        assert tuple(await self._status(owner_conn, "wamid.OUT1")) == ("read", None)
        assert tuple(await self._status(owner_conn, "wamid.OUT2")) == ("failed", 131047)

    async def test_a_late_status_never_moves_it_back(
        self, http: AsyncClient, owner_conn: Any
    ) -> None:
        await self._outbox(owner_conn, "wamid.OUT1", "read")
        payload = load("statuses")
        payload["entry"][0]["changes"][0]["value"]["statuses"] = [
            {"id": "wamid.OUT1", "status": "delivered", "timestamp": "1790000009"}
        ]
        body, headers = signed(payload)
        await http.post(URL, content=body, headers=headers)
        assert (await self._status(owner_conn, "wamid.OUT1"))["status"] == "read"

    async def test_statuses_create_no_jobs_and_unknown_ids_are_ignored(
        self, http: AsyncClient, owner_conn: Any
    ) -> None:
        body, headers = signed(load("statuses"))
        for _ in range(2):
            assert (await http.post(URL, content=body, headers=headers)).status_code == 200
        assert await _counts(owner_conn) == (0, 0)
