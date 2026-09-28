"""Iteration 01 acceptance test: "logs contain no raw PII".

This is the Done-When criterion, mechanised. It renders through the *real* processor chain
to the *real* stdout handler, so it covers the wiring as well as the redactor: a correct
redaction function installed in the wrong place would still fail here.
"""

from __future__ import annotations

import io
import json
import logging
from collections.abc import Callable
from uuid import uuid4

import pytest
import structlog
from fastapi import APIRouter, FastAPI, Request
from httpx import AsyncClient

from personal_organizer.api.app import create_app
from personal_organizer.api.routing import RawBodyRoute
from personal_organizer.observability.logging import configure_logging
from personal_organizer.settings import Settings
from tests.api.conftest import FakeIngressStore
from tests.api.test_whatsapp_webhook import WHATSAPP_ENV
from tests.conftest import CONTENT_PII, PATTERN_PII
from tests.fixtures.payloads import WA_ID, signed, text_message

ALL_PII = PATTERN_PII + CONTENT_PII

WEBHOOK_BODY = {
    "entry": [
        {
            "changes": [
                {
                    "value": {
                        "contacts": [{"wa_id": "31612345678", "profile": {"name": "Daniel"}}],
                        "messages": [
                            {
                                "from": "31612345678",
                                "text": {"body": "Oncology appointment with Dr Meyer"},
                            }
                        ],
                    }
                }
            ]
        }
    ]
}


def _assert_absent(captured: str) -> None:
    """Check every corpus item, raw and JSON-escaped."""
    for item in ALL_PII:
        assert item not in captured, f"raw PII leaked: {item!r}"
        escaped = json.dumps(item)[1:-1]
        if escaped != item:
            assert escaped not in captured, f"escaped PII leaked: {escaped!r}"


def _pii_app() -> FastAPI:
    router = APIRouter(route_class=RawBodyRoute)
    log = structlog.get_logger("test.pii")

    @router.post("/hook")
    async def hook(request: Request) -> dict[str, str]:
        payload = await request.json()
        # The careless call this whole design exists to survive.
        log.info("webhook.received", payload=payload, body=request.state.raw_body.decode())
        return {"status": "ok"}

    @router.post("/boom")
    async def boom() -> None:
        # Mirrors asyncpg/Pydantic embedding the offending value in the message.
        msg = "duplicate key value violates unique constraint: Key (phone)=(+31612345678)"
        raise ValueError(msg)

    app = FastAPI()
    app.include_router(router)
    return app


@pytest.fixture
def log_stream(settings: Settings) -> io.StringIO:
    """Bind the real processor chain to a buffer we can assert on."""
    stream = io.StringIO()
    configure_logging(settings, stream=stream)
    return stream


class TestNoPiiInLogs:
    async def test_webhook_payload_is_redacted(
        self, make_client: Callable[[FastAPI], AsyncClient], log_stream: io.StringIO
    ) -> None:
        async with make_client(_pii_app()) as client:
            response = await client.post("/hook", json=WEBHOOK_BODY)
        assert response.status_code == 200
        _assert_absent(log_stream.getvalue())

    async def test_exception_messages_are_redacted(
        self, make_client: Callable[[FastAPI], AsyncClient], log_stream: io.StringIO
    ) -> None:
        log = structlog.get_logger("test.pii")
        try:
            msg = "Key (phone)=(+31612345678) already exists, email daniel@example.com"
            raise ValueError(msg)
        except ValueError as exc:
            log.error("request.failed", error_type=type(exc).__name__, exc_info=exc)
        _assert_absent(log_stream.getvalue())

    async def test_identifiers_in_third_party_messages_are_redacted(
        self, log_stream: io.StringIO
    ) -> None:
        """Third-party loggers bypass structlog entirely, so they go through the same
        formatter and the same scrubber."""
        logging.getLogger("some.library").warning("Delivery failed for +31612345678")
        assert "+31612345678" not in log_stream.getvalue()

    async def test_procrastinate_job_arguments_are_redacted(self, log_stream: io.StringIO) -> None:
        """The live vector, not a hypothetical one.

        Procrastinate interpolates Job.call_string -- the full repr of every task kwarg --
        into its own log messages at INFO and ERROR. Were a message body ever passed as a
        kwarg -- docs/adr/0001 forbids it -- without the call-string rule every processed job
        would log the user's message verbatim.
        """
        logging.getLogger("procrastinate.worker").info(
            "Job webhooks:handle[9](body=%r, phone=%r) ended with status: succeeded",
            "Oncology appointment with Dr Meyer",
            "+31612345678",
        )
        _assert_absent(log_stream.getvalue())

    async def test_contextvars_are_redacted(self, log_stream: io.StringIO) -> None:
        structlog.contextvars.bind_contextvars(phone="+31612345678")
        try:
            structlog.get_logger("test.pii").info("bound.context")
        finally:
            structlog.contextvars.clear_contextvars()
        _assert_absent(log_stream.getvalue())

    async def test_correlation_ids_survive_the_real_chain(self, log_stream: io.StringIO) -> None:
        """Bound as contextvars, exactly as RequestContextMiddleware and tenant_session
        bind them, and asserted on the bytes the handler actually emits -- a redactor that
        strips correlation ids passes every leak test above and still breaks every
        investigation that starts from a request id the user was shown."""
        request_id, tenant_id = uuid4().hex, str(uuid4())
        structlog.contextvars.bind_contextvars(request_id=request_id, tenant_id=tenant_id)
        try:
            structlog.get_logger("test.pii").info("http.request", status_code=200)
        finally:
            structlog.contextvars.clear_contextvars()
        record = json.loads(log_stream.getvalue().strip().splitlines()[-1])
        assert record["request_id"] == request_id
        assert record["tenant_id"] == tenant_id

    async def test_output_is_still_valid_json_with_useful_fields(
        self, log_stream: io.StringIO
    ) -> None:
        """Redaction must not render the logs useless."""
        structlog.get_logger("test.pii").info(
            "http.request", method="POST", route="/hook", status_code=200, duration_ms=12.5
        )
        record = json.loads(log_stream.getvalue().strip().splitlines()[-1])
        assert record["event"] == "http.request"
        assert record["status_code"] == 200
        assert record["route"] == "/hook"
        assert record["service"] == "personal-organizer"


class TestWhatsAppWebhookLogs:
    """The real webhook router, the real logging chain: nothing the user sent, nor who they
    are, reaches a log line -- on the accepted path or on either rejection path."""

    def _app(self, settings_factory: Callable[..., Settings]) -> FastAPI:
        application = create_app(settings_factory(**WHATSAPP_ENV))
        application.state.ingress_store = FakeIngressStore()
        return application

    async def _post(
        self,
        settings_factory: Callable[..., Settings],
        make_client: Callable[[FastAPI], AsyncClient],
        stream: io.StringIO,
        *,
        bad_signature: bool = False,
    ) -> str:
        payload = text_message(wamid=WAMID, body="Oncology appointment with Dr Meyer")
        body, headers = signed(payload)
        if bad_signature:
            headers["x-hub-signature-256"] = "sha256=" + "0" * 64
        async with make_client(self._app(settings_factory)) as client:
            await client.post("/webhooks/whatsapp", content=body, headers=headers)
            await client.get(
                "/webhooks/whatsapp",
                params={"hub.mode": "subscribe", "hub.verify_token": "x", "hub.challenge": "1"},
            )
        return stream.getvalue()

    async def test_accepted_path(
        self,
        settings_factory: Callable[..., Settings],
        make_client: Callable[[FastAPI], AsyncClient],
        log_stream: io.StringIO,
    ) -> None:
        captured = await self._post(settings_factory, make_client, log_stream)
        assert "ingress.recorded" in captured
        _assert_absent(captured)
        assert WAMID not in captured
        assert WA_ID not in captured

    async def test_rejected_path(
        self,
        settings_factory: Callable[..., Settings],
        make_client: Callable[[FastAPI], AsyncClient],
        log_stream: io.StringIO,
    ) -> None:
        captured = await self._post(settings_factory, make_client, log_stream, bad_signature=True)
        assert "whatsapp.signature_rejected" in captured
        _assert_absent(captured)
        assert WAMID not in captured


WAMID = "wamid.HBgLMzE2MTIzNDU2NzgVAgASGBQzQTdEMEY1QjQ1RjE4NjhBMUUwRQA="
