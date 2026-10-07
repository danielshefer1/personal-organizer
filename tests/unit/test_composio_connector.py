"""ComposioConnector against the real, pinned SDK, with only HTTP stubbed.

The SDK is the part most likely to surprise: its method names, the requests it actually makes,
what it raises, and what it does by default (telemetry, request logs). So these tests run
composio end to end over ``httpx.MockTransport`` and stub nothing else.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from composio.core.models import _telemetry
from composio.core.models.base import allow_tracking
from composio.exceptions import ComposioMultipleConnectedAccountsError
from pydantic import SecretStr

from personal_organizer.core.errors import (
    CalendarProviderRejectedError,
    CalendarProviderUnavailableError,
)
from personal_organizer.interfaces.calendar import ConnectedAccount, ConnectLinker
from personal_organizer.providers.calendar.composio import ComposioConnector
from personal_organizer.settings import ComposioSettings

API = "https://backend.composio.dev"
TENANT = "3f2504e0-4f89-11d3-9a0c-0305e82c3301"
CALLBACK = "https://po.test/connect/callback?state=abc.def.ghi"
REDIRECT = "https://connect.composio.dev/link/ln_test"

LIST = ("GET", "/api/v3.1/connected_accounts")
LINK = ("POST", "/api/v3.1/connected_accounts/link")
NO_RETRY = {"x-should-retry": "false"}

Route = Callable[[], httpx.Response]


def account_route(account_id: str) -> tuple[str, str]:
    return ("GET", f"/api/v3.1/connected_accounts/{account_id}")


def listed(*account_ids: str) -> Route:
    return lambda: httpx.Response(
        200,
        json={"items": [{"id": i, "status": "ACTIVE"} for i in account_ids], "next_cursor": None},
    )


def linked(redirect_url: str | None = REDIRECT) -> Route:
    return lambda: httpx.Response(
        201,
        json={
            "connected_account_id": "ca_new",
            "redirect_url": redirect_url,
            "link_token": "ln_test",
            "expires_at": "2026-10-08T00:00:00Z",
        },
    )


def retrieved(**overrides: Any) -> Route:
    body: dict[str, Any] = {
        "id": "ca_new",
        "user_id": TENANT,
        "status": "ACTIVE",
        "auth_config": {"id": "ac_test"},
        # What the real response carries, and what must never leave the adapter.
        "data": {"access_token": "ya29.SECRET-ACCESS-TOKEN", "refresh_token": "1//SECRET"},
        **overrides,
    }
    return lambda: httpx.Response(200, json=body)


class Api:
    """A scripted Composio: one response factory per (method, path), every request kept."""

    def __init__(self, routes: dict[tuple[str, str], Route]) -> None:
        self.routes = routes
        self.requests: list[httpx.Request] = []
        self.tracking: list[bool] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        # Runs on the SDK's thread, inside the context the connector hands it.
        self.tracking.append(allow_tracking.get())
        route = self.routes.get((request.method, request.url.path))
        if route is None:
            return httpx.Response(404, json={"error": {"message": "no route"}}, headers=NO_RETRY)
        return route()


def connector(
    api: Callable[[httpx.Request], httpx.Response], *, timeout: float = 5.0
) -> ComposioConnector:
    settings = ComposioSettings(
        enabled=True,
        api_key=SecretStr("composio-test-key"),
        calendar_auth_config_id="ac_test",
        request_timeout_s=timeout,
    )
    client = httpx.Client(transport=httpx.MockTransport(api), base_url=API)
    return ComposioConnector.from_settings(settings, http_client=client)


async def link(target: ComposioConnector) -> str:
    return await target.link(user_id=TENANT, auth_config_id="ac_test", callback_url=CALLBACK)


def test_it_is_a_connect_linker() -> None:
    assert isinstance(connector(Api({})), ConnectLinker)


class TestLink:
    async def test_it_returns_composios_redirect_url(self) -> None:
        api = Api({LIST: listed(), LINK: linked()})
        assert await link(connector(api)) == REDIRECT

    async def test_it_sends_the_tenant_uuid_the_auth_config_and_our_callback(self) -> None:
        api = Api({LIST: listed(), LINK: linked()})
        await link(connector(api))
        [create] = [r for r in api.requests if (r.method, r.url.path) == LINK]
        assert json.loads(create.content) == {
            "auth_config_id": "ac_test",
            "user_id": TENANT,
            "callback_url": CALLBACK,
        }
        assert create.headers["x-api-key"] == "composio-test-key"

    async def test_an_existing_active_account_does_not_block_a_new_link(self) -> None:
        """Without allow_multiple the SDK raises here, and a tenant whose first callback was
        lost could never connect again."""
        api = Api({LIST: listed("ca_old"), LINK: linked()})
        assert await link(connector(api)) == REDIRECT

    @pytest.mark.parametrize(
        "redirect_url", [None, "http://connect.composio.dev/x", "javascript:x"]
    )
    async def test_a_redirect_that_is_not_https_is_refused(self, redirect_url: str | None) -> None:
        api = Api({LIST: listed(), LINK: linked(redirect_url)})
        with pytest.raises(CalendarProviderRejectedError):
            await link(connector(api))


class TestGetAccount:
    async def test_it_keeps_four_fields_and_drops_the_tokens(self) -> None:
        api = Api({account_route("ca_new"): retrieved()})
        result = await connector(api).get_account("ca_new")
        assert result == ConnectedAccount(
            id="ca_new", user_id=TENANT, auth_config_id="ac_test", status="ACTIVE"
        )
        assert "SECRET" not in repr(result)

    async def test_it_reports_the_status_as_composio_does(self) -> None:
        api = Api({account_route("ca_new"): retrieved(status="INITIATED")})
        assert (await connector(api).get_account("ca_new")).status == "INITIATED"


class TestErrors:
    @pytest.mark.parametrize("status", [400, 401, 403, 404])
    async def test_a_4xx_is_rejected_with_its_status(self, status: int) -> None:
        api = Api(
            {account_route("ca_x"): lambda: httpx.Response(status, json={}, headers=NO_RETRY)}
        )
        with pytest.raises(CalendarProviderRejectedError) as raised:
            await connector(api).get_account("ca_x")
        assert raised.value.status_code == status

    @pytest.mark.parametrize("status", [429, 500, 502, 503])
    async def test_throttling_and_5xx_are_unavailable(self, status: int) -> None:
        api = Api(
            {account_route("ca_x"): lambda: httpx.Response(status, json={}, headers=NO_RETRY)}
        )
        with pytest.raises(CalendarProviderUnavailableError):
            await connector(api).get_account("ca_x")

    async def test_one_quick_5xx_is_retried_inside_the_deadline(self) -> None:
        responses = [
            httpx.Response(503, json={}, headers={"retry-after-ms": "0"}),
            retrieved()(),
        ]
        api = Api({account_route("ca_new"): lambda: responses.pop(0)})
        assert (await connector(api).get_account("ca_new")).id == "ca_new"

    async def test_an_unreachable_api_is_unavailable(self) -> None:
        def refuse(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused", request=request)

        with pytest.raises(CalendarProviderUnavailableError):
            await connector(refuse).get_account("ca_x")

    async def test_a_slow_api_is_cut_off_at_the_deadline(self) -> None:
        def slow(request: httpx.Request) -> httpx.Response:
            time.sleep(2)
            return retrieved()()

        started = time.monotonic()
        with pytest.raises(CalendarProviderUnavailableError):
            await connector(slow, timeout=0.2).get_account("ca_new")
        assert time.monotonic() - started < 1.5

    async def test_an_sdk_side_refusal_is_rejected(self) -> None:
        def refuse(*_: Any, **__: Any) -> Any:
            raise ComposioMultipleConnectedAccountsError("Multiple connected accounts found")

        sdk = SimpleNamespace(connected_accounts=SimpleNamespace(link=refuse, get=refuse))
        with pytest.raises(CalendarProviderRejectedError) as raised:
            await link(ComposioConnector(sdk, timeout_s=1.0))
        assert raised.value.status_code is None

    async def test_the_sdks_message_is_not_ours(self) -> None:
        """Its messages name the user id and echo URLs; ours are fixed strings."""
        api = Api({account_route("ca_x"): lambda: httpx.Response(404, json={}, headers=NO_RETRY)})
        with pytest.raises(CalendarProviderRejectedError) as raised:
            await connector(api).get_account("ca_x")
        assert "ca_x" not in str(raised.value)


class TestQuiet:
    async def test_calls_run_with_telemetry_off_whatever_the_callers_context(self) -> None:
        api = Api({LIST: listed(), LINK: linked(), account_route("ca_new"): retrieved()})
        target = connector(api)
        # The constructor turned tracking off in *this* context. A request handler's context
        # is not the lifespan's, so put back what a handler would actually see.
        token = allow_tracking.set(True)
        try:
            await link(target)
            await target.get_account("ca_new")
        finally:
            allow_tracking.reset(token)
        assert api.tracking
        assert not any(api.tracking)
        assert _telemetry._thread is None

    def test_the_sdk_logs_failures_only(self) -> None:
        connector(Api({}))
        assert logging.getLogger("composio_client").getEffectiveLevel() >= logging.WARNING
        assert logging.getLogger("composio").getEffectiveLevel() >= logging.WARNING
