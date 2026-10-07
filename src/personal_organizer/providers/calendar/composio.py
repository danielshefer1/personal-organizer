"""Composio as the :class:`~personal_organizer.interfaces.calendar.ConnectLinker`.

Composio's Python SDK is synchronous, so every call runs on a worker thread under one deadline,
``COMPOSIO__REQUEST_TIMEOUT_S``, which also covers the SDK's own retry. Three SDK defaults are
changed here:

- **Telemetry.** Methods of the SDK's ``Resource`` classes post each call to
  ``telemetry.composio.dev``, with redacted error text and a stack trace. The off switch is a
  ``ContextVar``, and ``Composio(allow_tracking=False)`` sets it only in the context that
  constructs the client. The api constructs this one in its lifespan, so request handlers would
  still see the default, ``True``. Each call therefore sets it again inside the thread's copied
  context. (In 0.24.0, neither ``connected_accounts.link`` nor ``.get`` is traced. This keeps
  it that way after an upgrade.)
- **Request logs.** The client logs every request at INFO. ``LogLevel.WARNING`` keeps only
  failures. They go through the root logger, so the redaction chain sees them.
- **The multiple-accounts guard.** ``link`` refuses a user who already has an ``ACTIVE``
  account on the auth config. A tenant whose first attempt reached ``ACTIVE`` but whose callback
  never arrived would then be stuck for good. So ``allow_multiple=True``, and the callback's
  ``bind_connection`` revokes our old row.

The SDK's account record carries the OAuth tokens (``data``, ``params``, ``state``).
:meth:`ComposioConnector.get_account` copies four fields out and lets the record go.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from typing import Any, Final

import anyio
import anyio.to_thread
import composio_client
import httpx
from composio import Composio
from composio import exceptions as composio_exceptions
from composio.core.models.base import allow_tracking
from composio.sdk import SDKConfig
from composio.utils.logging import LogLevel

from personal_organizer.core.errors import (
    CalendarProviderError,
    CalendarProviderRejectedError,
    CalendarProviderUnavailableError,
    ConfigError,
)
from personal_organizer.interfaces.calendar import ConnectedAccount
from personal_organizer.settings import ComposioSettings

#: Statuses worth another attempt later. Any 5xx is too.
_TRANSIENT_STATUSES: Final = frozenset({408, 409, 429})
#: One retry, for a quick 5xx. A slow failure uses up the deadline before a retry could finish.
_SDK_MAX_RETRIES: Final = 1
#: Worker threads for SDK calls. A call past its deadline is abandoned, not killed, and keeps its
#: thread until the SDK gives up. Sharing anyio's default pool (40) would let a slow Composio
#: starve every other thread user, so the connector gets its own, small, pool.
SDK_LIMITER: Final = anyio.CapacityLimiter(4)


def _untracked[T](fn: Callable[[], T]) -> T:
    """Run ``fn`` with Composio's telemetry off, in this thread's context only."""
    token = allow_tracking.set(False)
    try:
        return fn()
    finally:
        allow_tracking.reset(token)


def _translate(exc: Exception) -> CalendarProviderError:
    if isinstance(exc, composio_client.APIConnectionError):  # APITimeoutError is one too
        return CalendarProviderUnavailableError("Calendar provider unreachable")
    if isinstance(exc, composio_client.APIStatusError):
        status = exc.status_code
        if status >= 500 or status in _TRANSIENT_STATUSES:
            return CalendarProviderUnavailableError(f"Calendar provider answered {status}")
        return CalendarProviderRejectedError(status)
    return CalendarProviderRejectedError(None)


class ComposioConnector:
    def __init__(self, sdk: Any, *, timeout_s: float) -> None:
        self._sdk = sdk
        self._timeout_s = timeout_s

    @classmethod
    def from_settings(
        cls, settings: ComposioSettings, *, http_client: httpx.Client | None = None
    ) -> ComposioConnector:
        """``http_client`` is for tests: the SDK sends every request through it."""
        if settings.api_key is None:
            msg = "ComposioConnector needs COMPOSIO__API_KEY"
            raise ConfigError(msg)
        config: SDKConfig = {
            "api_key": settings.api_key.get_secret_value(),
            "allow_tracking": False,
            "timeout": math.ceil(settings.request_timeout_s),
            "max_retries": _SDK_MAX_RETRIES,
            "logging_level": LogLevel.WARNING,
        }
        if http_client is not None:
            config["http_client"] = http_client
        return cls(Composio(**config), timeout_s=settings.request_timeout_s)

    async def _call[T](self, fn: Callable[[], T]) -> T:
        try:
            with anyio.fail_after(self._timeout_s):
                # abandon_on_cancel: past the deadline the request is left to finish on its
                # thread rather than holding the caller.
                return await anyio.to_thread.run_sync(
                    _untracked, fn, abandon_on_cancel=True, limiter=SDK_LIMITER
                )
        except TimeoutError:
            # Ours (fail_after), or the SDK's ComposioSDKTimeoutError, which is also one.
            raise CalendarProviderUnavailableError("Calendar provider timed out") from None
        except (composio_client.ComposioError, composio_exceptions.ComposioError) as exc:
            # No chaining: the SDK's message is the response body, and Sentry sends __cause__.
            raise _translate(exc) from None

    async def link(self, *, user_id: str, auth_config_id: str, callback_url: str) -> str:
        request = await self._call(
            lambda: self._sdk.connected_accounts.link(
                user_id, auth_config_id, callback_url=callback_url, allow_multiple=True
            )
        )
        redirect_url = request.redirect_url
        # We 303 the browser to it, so it must be a page, not a scheme of someone's choosing.
        if not isinstance(redirect_url, str) or not redirect_url.startswith("https://"):
            raise CalendarProviderRejectedError(None)
        return redirect_url

    async def get_account(self, connected_account_id: str) -> ConnectedAccount:
        record = await self._call(
            lambda: self._sdk.connected_accounts.get(nanoid=connected_account_id)
        )
        try:
            return ConnectedAccount(
                id=str(record.id),
                user_id=str(record.user_id),
                auth_config_id=str(record.auth_config.id),
                status=str(record.status),
            )
        except AttributeError, TypeError:
            raise CalendarProviderRejectedError(None) from None


__all__ = ["ComposioConnector", "ConnectedAccount"]
