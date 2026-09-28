"""``/webhooks/whatsapp`` -- Meta's Cloud API webhook.

Thin by design: verify, persist, answer. Everything that could be slow or could fail for
reasons other than our own database happens in the worker, because Meta retries anything it
does not see acknowledged and a slow ack becomes a duplicate delivery.

Status codes are chosen for what Meta does with them, not for REST purity:

- **401** for a bad signature. Not from Meta, so nobody retries it.
- **200** for a validly signed body we cannot use (bad JSON, a shape we do not know). A
  retry cannot fix it, and anything else earns seven days of redeliveries.
- **500** when our database fails. Nothing was committed, and a redelivery is exactly
  what should happen.

Mounted only when ``WHATSAPP__ENABLED`` is true; see :func:`create_app`.
"""

from __future__ import annotations

import json
import secrets
from typing import Annotated, Final

import structlog
from fastapi import APIRouter, Query, Request, Response
from fastapi.responses import PlainTextResponse
from starlette.status import HTTP_200_OK, HTTP_401_UNAUTHORIZED, HTTP_403_FORBIDDEN

from personal_organizer.api.deps import InboundChannelDep, IngressStoreDep, SettingsDep
from personal_organizer.api.routing import RawBodyRoute
from personal_organizer.providers.channel.whatsapp.signature import (
    SIGNATURE_HEADER,
    rejection_reason,
)

log = structlog.get_logger(__name__)

#: Meta's challenge is a short random string. A bound keeps this endpoint from being an
#: echo service for arbitrary content even for a caller who has the verify token.
MAX_CHALLENGE_LENGTH: Final = 128

router = APIRouter(prefix="/webhooks", tags=["webhooks"], route_class=RawBodyRoute)


@router.get("/whatsapp", summary="Meta's subscription handshake.")
async def verify_subscription(
    settings: SettingsDep,
    mode: Annotated[str | None, Query(alias="hub.mode")] = None,
    token: Annotated[str | None, Query(alias="hub.verify_token")] = None,
    challenge: Annotated[str | None, Query(alias="hub.challenge")] = None,
) -> Response:
    """Echo ``hub.challenge`` if and only if ``hub.verify_token`` is ours.

    Every parameter is optional at the framework level so that a malformed request gets a
    403 like any other rejection, rather than a 422 that describes what was expected.
    """
    expected = settings.whatsapp.verify_token
    ok = (
        mode == "subscribe"
        and expected is not None
        and token is not None
        and challenge is not None
        and len(challenge) <= MAX_CHALLENGE_LENGTH
        and secrets.compare_digest(token.encode(), expected.get_secret_value().encode())
    )
    if not ok:
        log.warning("whatsapp.verify_rejected", channel="whatsapp")
        return Response(status_code=HTTP_403_FORBIDDEN)
    log.info("whatsapp.verified", channel="whatsapp")
    return PlainTextResponse(challenge)


@router.post("/whatsapp", summary="Inbound messages and delivery statuses.")
async def receive(request: Request, channel: InboundChannelDep, store: IngressStoreDep) -> Response:
    raw_body: bytes = request.state.raw_body
    header = request.headers.get(SIGNATURE_HEADER)
    if not channel.verify_signature(raw_body, header):
        # Not a Sentry event: this is the internet knocking, and a flood of them is not a bug.
        log.warning(
            "whatsapp.signature_rejected", channel=channel.name, reason=rejection_reason(header)
        )
        return Response(status_code=HTTP_401_UNAUTHORIZED)

    try:
        payload = json.loads(raw_body)
    except ValueError:
        log.warning("whatsapp.payload_unparseable", channel=channel.name)
        return Response(status_code=HTTP_200_OK)

    batch = channel.parse_webhook(payload)
    result = await store.record(channel.name, batch)
    log.info(
        "ingress.recorded",
        channel=channel.name,
        message_count=result.inserted,
        duplicate_count=result.duplicates,
        status_count=result.statuses_applied,
        skipped_count=batch.skipped,
    )
    return Response(status_code=HTTP_200_OK)


__all__ = ["MAX_CHALLENGE_LENGTH", "router"]
