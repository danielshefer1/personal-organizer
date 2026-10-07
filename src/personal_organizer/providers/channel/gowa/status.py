"""Is the gateway's WhatsApp session up?

One question, asked by ``po-gowa status`` and by the worker's ``system:gowa_health`` task,
so the operator's check and the alert can never disagree about the answer.

The gateway answers ``GET /app/status`` with
``{"results": {"is_connected": <bool>, "is_logged_in": <bool>}}``. *Logged in* is the
linked-device session: when it is false, someone has to scan a QR code from the bot's
phone, and nothing recovers by itself. *Connected* is the socket to WhatsApp: false while
logged in means the gateway is reconnecting, and sends fail with 401 until it has.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final

import httpx

from personal_organizer.providers.channel.gowa.outbound import DEVICE_HEADER
from personal_organizer.settings import GowaSettings

STATUS_PATH: Final = "app/status"


class Unhealthy(StrEnum):
    """Why the gateway cannot carry messages. Logged as ``reason`` and tagged in Sentry."""

    UNREACHABLE = "unreachable"
    NOT_CONNECTED = "not_connected"
    NOT_LOGGED_IN = "not_logged_in"
    BAD_RESPONSE = "bad_response"


@dataclass(frozen=True, slots=True)
class GatewayHealth:
    #: ``None`` when the gateway is connected and logged in.
    reason: Unhealthy | None
    connected: bool | None = None
    logged_in: bool | None = None
    #: The HTTP status of a non-2xx answer (``BAD_RESPONSE`` only).
    status_code: int | None = None
    #: The exception's class for ``UNREACHABLE``. Never its message, which quotes the URL.
    error_type: str | None = None

    @property
    def ok(self) -> bool:
        return self.reason is None


def _auth(gowa: GowaSettings) -> httpx.BasicAuth:
    if gowa.basic_auth_user is None or gowa.basic_auth_password is None:
        msg = "GOWA__BASIC_AUTH_USER and GOWA__BASIC_AUTH_PASSWORD must be set"
        raise ValueError(msg)
    return httpx.BasicAuth(gowa.basic_auth_user, gowa.basic_auth_password.get_secret_value())


def _headers(gowa: GowaSettings) -> dict[str, str]:
    return {DEVICE_HEADER: gowa.device_id} if gowa.device_id else {}


def _url(gowa: GowaSettings) -> str:
    return f"{gowa.base_url}/{STATUS_PATH}"


def interpret(response: httpx.Response) -> GatewayHealth:
    """Map the gateway's answer onto a health. Strict: anything but two booleans is bad."""
    if not response.is_success:
        return GatewayHealth(Unhealthy.BAD_RESPONSE, status_code=response.status_code)
    try:
        results = response.json()["results"]
        connected, logged_in = results["is_connected"], results["is_logged_in"]
    except ValueError, KeyError, TypeError:
        return GatewayHealth(Unhealthy.BAD_RESPONSE)
    if not isinstance(connected, bool) or not isinstance(logged_in, bool):
        return GatewayHealth(Unhealthy.BAD_RESPONSE)
    # Logged out wins: it is the one that needs a person, whatever the socket says.
    if not logged_in:
        reason: Unhealthy | None = Unhealthy.NOT_LOGGED_IN
    elif not connected:
        reason = Unhealthy.NOT_CONNECTED
    else:
        reason = None
    return GatewayHealth(reason, connected=connected, logged_in=logged_in)


def _unreachable(exc: httpx.HTTPError) -> GatewayHealth:
    return GatewayHealth(Unhealthy.UNREACHABLE, error_type=type(exc).__name__)


async def check_status(client: httpx.AsyncClient, gowa: GowaSettings) -> GatewayHealth:
    """Ask the gateway once. Never raises for a network or protocol failure: that is the
    answer. Raises ``ValueError`` only when the basic-auth pair is not configured."""
    auth = _auth(gowa)
    try:
        response = await client.get(_url(gowa), auth=auth, headers=_headers(gowa))
    except httpx.HTTPError as exc:
        return _unreachable(exc)
    return interpret(response)


def check_status_sync(client: httpx.Client, gowa: GowaSettings) -> GatewayHealth:
    """:func:`check_status` for the synchronous CLI."""
    auth = _auth(gowa)
    try:
        response = client.get(_url(gowa), auth=auth, headers=_headers(gowa))
    except httpx.HTTPError as exc:
        return _unreachable(exc)
    return interpret(response)


__all__ = [
    "STATUS_PATH",
    "GatewayHealth",
    "Unhealthy",
    "check_status",
    "check_status_sync",
    "interpret",
]
