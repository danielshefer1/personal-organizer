# Iteration 03-lite, PR `4-connect`: Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A tenant in the `connect` step taps the link the bot sent, presses one button, connects
Google Calendar through Composio, comes back to a "you're connected" page, and gets "You're all
set" on WhatsApp exactly once.

**Architecture:** A `ConnectLinker` protocol (`interfaces/calendar.py`) with one adapter,
`ComposioConnector`, which runs Composio's synchronous SDK on a worker thread under a deadline.
Three routes under `/connect` (mounted only when Composio is enabled) call a small service module,
`onboarding/connect.py`, and render autoescaped jinja2 pages that carry D4's headers. The
callback binds the connection and activates the tenant in one tenant transaction, then defers
`onboarding:connected`, whose task sends the message through `send_once` keyed on
`connected:<connection_id>`.

**Tech Stack:** Python 3.14, FastAPI 0.141, SQLAlchemy 2 async (asyncpg), Procrastinate 3.10,
PostgreSQL 16, jinja2 3.1.6, itsdangerous 2.2 (through PR 3's `onboarding/tokens.py`),
composio 0.24.0 with composio-client 2.0.0rc8 (pinned in `uv.lock`), anyio 4.15, pytest with
pytest-asyncio (`asyncio_mode = "auto"`), httpx `MockTransport`.

**Spec:** `docs/plan-iteration-03.md` (D4, D5, D6, D10's "Callback success" paragraph, and PR
stack item 3). **Contract:** `docs/superpowers/plans/2026-10-07-iteration-03-contract.md`
("PR 4 produces"). PRs 2 and 3 are merged into this branch's base: everything their contract
sections list exists, with exactly those names and signatures.

**Branch:** `iteration-03/4-connect`, stacked on `iteration-03/3-onboarding-chat`.

---

## Contract deviations

None of these renames, re-signs or moves a contract item. Each is additive or a placement the
contract left open.

1. **`ConnectedAccount` lives in `interfaces/calendar.py`, not `providers/calendar/composio.py`.**
   The `ConnectLinker` protocol, which the contract puts in `interfaces/calendar.py`, returns it,
   and `interfaces/` must stay dependency-free (`pyproject.toml`'s layering note): a protocol
   there cannot import a provider. `providers/calendar/composio.py` re-exports it, so the
   contract's import path `personal_organizer.providers.calendar.composio.ConnectedAccount`
   works unchanged.
2. **`link()` passes `allow_multiple=True`; `ComposioMultipleConnectedAccountsError` is not the
   reconnect path.** The spec's risk list expected that error on a reconnect. In the real SDK,
   `connected_accounts.link()` first lists the user's `ACTIVE` accounts on the auth config and
   raises it if there is one, unless `allow_multiple=True`. Without the flag, a tenant whose
   first attempt reached `ACTIVE` at Composio but whose callback never arrived (tab closed,
   network) is stuck: every new link raises. With it, the new account binds and
   `bind_connection` revokes our old row (PR 2's contract). The orphaned account at Composio
   is left in place; removing it belongs with "reconnect on auth errors" in the calendar-read
   iteration. The error is still mapped (to `CalendarProviderRejectedError`) in case it is
   raised anyway.
3. **Composio has no `state` parameter.** `connected_accounts.link(user_id, auth_config_id, *,
   callback_url, alias, allow_multiple, experimental)` is the whole surface. The signed state
   rides in `callback_url` as `?state=…`, as the contract assumed. The callback reads the
   account id from `connected_account_id` or `connectedAccountId`, because nothing in the SDK
   names the parameter Composio appends. The day-1 staging run confirms which one arrives.
4. **New error types** in `core/errors.py`: `CalendarProviderError`,
   `CalendarProviderUnavailableError`, `CalendarProviderRejectedError(status_code)`.
5. **Page copy lives in `onboarding/page_text.py`, not `messaging/onboarding_text.py`.** D11 says
   all onboarding strings live in the latter. But that table's internal shape is PR 3's and not
   in the contract, and these are HTML page strings, not WhatsApp ones. The chat message
   ("all_set") still comes from PR 3's `t()`.
6. **Tests are split across two files.** The spec names `tests/api/test_connect.py`. That file
   holds everything that is refused before the database is reached. `tests/api` has no database
   fixtures, so the flow against Postgres is in `tests/db/test_connect.py`.
7. **`observability/redaction.py`** gains two scrubbing rules and the safe keys `connection_id`
   and `composio_enabled`. Sentry and the logs share `scrub_text`, so "Sentry's URL scrubbing
   learns `/connect/*`" (D4) is implemented there, once.

## Global Constraints

- Python `>=3.14`, `uv`. `uv run ruff check .`, `uv run ruff format --check .` and `uv run mypy`
  (strict, over `src` **and** `tests`) must pass. Every test function and fixture is fully
  annotated.
- D4: "the connect link is consumed on POST, never on GET." The page sets
  `Referrer-Policy: no-referrer`, `Cache-Control: no-store` and CSP `default-src 'none'`, and
  "Sentry's URL scrubbing learns `/connect/*`".
- D5: the callback "verifies our signed `state` (tenant + link nonce), then fetches the connected
  account and requires `user_id == str(tenant_id)`, the expected `auth_config_id` and `ACTIVE`.
  Only then does it bind the account."
- D6: the "You're all set" send carries idempotency key `connected:<connection_id>`.
- "Composio's `user_id` is the tenant UUID, never a phone number."
- Task kwargs carry ids only (`docs/adr/0001`): `onboarding:connected` takes
  `tenant_id: str, connection_id: str`.
- Pages: jinja2, autoescaped, no external assets. The router is mounted only when
  `settings.composio.enabled`.
- Exception messages are never logged or shown: log `error_type` / `error_code` /
  `status_code` (`core/errors.py` docstring). Log keys not in `SAFE_KEYS` are replaced by a
  shape summary, so new keys are allowlisted on purpose (Task 2).
- `tests/db` tests skip without Postgres, and **a skip is not a pass.** Before the DB steps, run
  `docker compose up -d`, `uv run po-db bootstrap` and `uv run alembic upgrade head` (README).

## Review Focus

These are the inputs a person will actually hit that no spec line spells out, most likely first.
Each one has a pinning test in the task named.

1. **Composio's callback query is not what we assumed.** It may name the account id in
   camelCase, or not keep our `?state=`. Expected: both param spellings work. A callback with no
   usable state or id gets the "ask the bot for a new link" page, never a 500. Pinned in Task 6
   (camelCase test, missing-param tests). The real shape is a day-1 staging check.
2. **The account is not `ACTIVE` yet when the browser arrives.** Composio can redirect while the
   account is still `INITIATED`. Expected: a "reload in a few seconds" page, and a reload then
   connects. Pinned in Task 6 (`test_an_account_not_yet_active_can_be_reloaded`).
3. **A double tap on the button.** Expected: one redirect to Composio and one "link used" page,
   with Composio called once. Pinned in Task 4 (`test_two_taps_at_once_make_one_redirect`).
4. **WhatsApp's link preview (GET, and possibly HEAD) fetches the link first.** Expected: the
   link still works when the user taps the button. Pinned in Task 4
   (`test_previews_do_not_spend_the_link`).
5. **The tenant last wrote on a channel this worker no longer runs** (GOWA switched off, say).
   Expected: the job logs and finishes. No crash loop, no retry storm. Pinned in Task 5
   (`test_a_channel_the_worker_lacks_is_logged_not_retried`).

---

## File structure

| File | Responsibility |
|---|---|
| `src/personal_organizer/core/errors.py` (modify) | `CalendarProvider*Error` types |
| `src/personal_organizer/interfaces/calendar.py` (modify) | `ConnectedAccount`, `ACCOUNT_ACTIVE`, `ConnectLinker` |
| `src/personal_organizer/providers/calendar/composio.py` (create) | `ComposioConnector`: SDK on a thread, deadline, telemetry off, error mapping |
| `src/personal_organizer/observability/redaction.py` (modify) | Scrub `/connect/<token>` and `state=`; new safe keys |
| `src/personal_organizer/onboarding/page_text.py` (create) | Page copy, he/en |
| `src/personal_organizer/api/pages.py` (create) | Render pages; D4 headers; CSP with the style hash |
| `src/personal_organizer/api/templates/{base,connect,message}.html` (create) | The three templates |
| `src/personal_organizer/onboarding/connect.py` (create) | `ConnectConfig`, `open_link`, `start_connect`, `complete_connect` |
| `src/personal_organizer/api/routers/connect.py` (create) | The three routes |
| `src/personal_organizer/api/{app,deps,lifespan}.py` (modify) | Mount, DI, build the connector |
| `src/personal_organizer/onboarding/connected.py` (create) | `announce_connected`: the task's logic |
| `src/personal_organizer/worker/tasks/onboarding.py` (create) | `ONBOARDING_CONNECTED_TASK`, `register` |
| `src/personal_organizer/worker/tasks/__init__.py` (modify) | Add to `REGISTRARS` |
| `tests/fixtures/connect.py` (create) | Settings, `FakeConnectLinker`, tokens, seeders, assertions |
| `tests/unit/test_composio_connector.py` (create) | The real SDK over `httpx.MockTransport` |
| `tests/unit/test_connect_pages.py` (create) | Headers, CSP hash, escaping, RTL, no assets |
| `tests/unit/test_redaction.py`, `tests/unit/test_sentry_scrubbing.py` (modify) | `/connect/*` scrubbing |
| `tests/api/test_connect.py` (create) | Everything refused before the database |
| `tests/db/test_connect.py` (create) | The flow against Postgres |
| `tests/db/test_onboarding_connected.py` (create) | The task's logic against Postgres |
| `tests/worker/test_app.py` (modify) | Registration, queue, retry |

---

### Task 1: `ConnectLinker` and `ComposioConnector`

What the real SDK (composio 0.24.0) does, verified against the installed package. The tests
below pin all of it:

- `Composio(api_key=…, allow_tracking=…, timeout=<int>, max_retries=…, logging_level=…,
  http_client=…)`. The constructor makes no network call.
- `sdk.connected_accounts.link(user_id, auth_config_id, *, callback_url=None, alias=None,
  allow_multiple=False, experimental=None) -> ConnectionRequest`. The request has `.id` (the
  `ca_…` connected account id) and `.redirect_url: str | None`. It makes two requests:
  `GET /api/v3.1/connected_accounts?user_ids=…&auth_config_ids=…&statuses=ACTIVE`, then
  `POST /api/v3.1/connected_accounts/link` with JSON `{auth_config_id, user_id, callback_url}`.
- `sdk.connected_accounts.get(nanoid=…)` is `composio_client`'s `retrieve`:
  `GET /api/v3.1/connected_accounts/{id}`. It returns a model with `.id`, `.user_id`,
  `.status` (`INITIALIZING|INITIATED|ACTIVE|FAILED|EXPIRED|INACTIVE|REVOKED`) and
  `.auth_config.id`. **It also carries the OAuth tokens** (`data`, `params`, `state`).
- HTTP errors are `composio_client.APIStatusError` subclasses with `.status_code`.
  Connection failures are `composio_client.APIConnectionError` (`APITimeoutError` is one).
  SDK-side refusals are `composio.exceptions.ComposioError` subclasses, a separate hierarchy.
  The client retries 408/409/429/5xx `max_retries` times unless the response carries
  `x-should-retry: false`.
- **Telemetry:** methods of `Resource` subclasses post to `telemetry.composio.dev` unless the
  `ContextVar` `composio.core.models.base.allow_tracking` is `False`.
  `Composio(allow_tracking=False)` sets it **only in the constructing task's context**. The api
  builds the connector in its lifespan, so request handlers would see the default `True`. In
  0.24.0, `ConnectedAccounts.link` and `.get` happen not to be traced. The connector still sets
  the variable inside every thread call, so an SDK upgrade cannot quietly start sending.
- **Logs:** `composio_client` logs each request at INFO through stdlib logging, which reaches our
  root redaction chain. `logging_level=LogLevel.WARNING` sets the `composio`/`composio_client`
  loggers to WARNING.

**Files:**
- Modify: `src/personal_organizer/core/errors.py`
- Modify: `src/personal_organizer/interfaces/calendar.py`
- Create: `src/personal_organizer/providers/calendar/composio.py`
- Test: `tests/unit/test_composio_connector.py`

**Interfaces:**
- Consumes: `ComposioSettings` (`api_key: SecretStr | None`, `request_timeout_s: float`) from
  `settings.py`.
- Produces:
  - `core.errors`: `CalendarProviderError(AppError)`,
    `CalendarProviderUnavailableError(CalendarProviderError)`,
    `CalendarProviderRejectedError(CalendarProviderError)` with `__init__(self, status_code:
    int | None)` and `.status_code`.
  - `interfaces.calendar`: `ACCOUNT_ACTIVE: Final = "ACTIVE"`;
    `@dataclass(frozen=True, slots=True) class ConnectedAccount: id: str; user_id: str;
    auth_config_id: str; status: str`; `@runtime_checkable class ConnectLinker(Protocol)` with
    `async def link(self, *, user_id: str, auth_config_id: str, callback_url: str) -> str` and
    `async def get_account(self, connected_account_id: str) -> ConnectedAccount`.
  - `providers.calendar.composio`: `class ComposioConnector` with
    `__init__(self, sdk: Any, *, timeout_s: float)`,
    `@classmethod from_settings(cls, settings: ComposioSettings, *, http_client: httpx.Client |
    None = None) -> ComposioConnector`, and the two protocol methods. Re-exports
    `ConnectedAccount`.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_composio_connector.py`:

```python
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


def connector(api: Callable[[httpx.Request], httpx.Response], *, timeout: float = 5.0) -> ComposioConnector:
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

    @pytest.mark.parametrize("redirect_url", [None, "http://connect.composio.dev/x", "javascript:x"])
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
        api = Api({account_route("ca_x"): lambda: httpx.Response(status, json={}, headers=NO_RETRY)})
        with pytest.raises(CalendarProviderRejectedError) as raised:
            await connector(api).get_account("ca_x")
        assert raised.value.status_code == status

    @pytest.mark.parametrize("status", [429, 500, 502, 503])
    async def test_throttling_and_5xx_are_unavailable(self, status: int) -> None:
        api = Api({account_route("ca_x"): lambda: httpx.Response(status, json={}, headers=NO_RETRY)})
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/unit/test_composio_connector.py -v`
Expected: collection error, `ModuleNotFoundError: No module named 'personal_organizer.providers.calendar.composio'`.

- [ ] **Step 3: Add the error types**

In `src/personal_organizer/core/errors.py`, after `class MessageTooLongError`, add:

```python
class CalendarProviderError(AppError):
    """The calendar connector (Composio) refused or failed a request.

    Messages are fixed strings. The SDK's own name the user id and echo request URLs, and our
    callback URL carries a signed state.
    """

    code = "calendar_provider_error"


class CalendarProviderUnavailableError(CalendarProviderError):
    """Timed out, unreachable, throttled or a 5xx. The same request may work later."""

    code = "calendar_provider_unavailable"


class CalendarProviderRejectedError(CalendarProviderError):
    """Refused: a 4xx, an SDK-side refusal, or a response we cannot use. Retrying won't help."""

    code = "calendar_provider_rejected"

    def __init__(self, status_code: int | None) -> None:
        super().__init__(f"Calendar provider rejected the request (status {status_code})")
        self.status_code = status_code
```

and add `"CalendarProviderError"`, `"CalendarProviderRejectedError"`,
`"CalendarProviderUnavailableError"` to `__all__`, keeping it sorted (they go after
`"BootstrapError"`).

- [ ] **Step 4: Extend the calendar interface**

Replace `src/personal_organizer/interfaces/calendar.py` with:

```python
"""Calendar interfaces.

Two seams. ``ConnectLinker`` takes a user through connecting their Google Calendar: Composio
today, under our own Google OAuth app (a Composio custom auth config). ``CalendarProvider``
reads and writes events once connected, from the calendar-read iteration. Both stay
dependency-free, so swapping an adapter never reaches the callers.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Final, Protocol, runtime_checkable

from personal_organizer.core.types import TenantId

#: The one connected-account status that means "tokens held, calls will work".
ACCOUNT_ACTIVE: Final = "ACTIVE"


@dataclass(frozen=True)
class CalendarEvent:
    external_id: str
    title: str
    start: datetime
    end: datetime
    all_day: bool = False


@dataclass(frozen=True, slots=True)
class ConnectedAccount:
    """What the connect callback needs to know about an account, and nothing more.

    The provider's own record also holds the OAuth tokens. An adapter copies these four fields
    out and drops the rest, so the tokens never reach a log line, an exception or Sentry.
    """

    id: str
    user_id: str
    auth_config_id: str
    status: str


@runtime_checkable
class ConnectLinker(Protocol):
    async def link(self, *, user_id: str, auth_config_id: str, callback_url: str) -> str:
        """Start a connection for ``user_id``; return the URL to send the browser to."""
        ...

    async def get_account(self, connected_account_id: str) -> ConnectedAccount: ...


@runtime_checkable
class CalendarProvider(Protocol):
    async def list_events(
        self, tenant_id: TenantId, *, start: datetime, end: datetime
    ) -> Sequence[CalendarEvent]: ...

    async def create_event(self, tenant_id: TenantId, event: CalendarEvent) -> CalendarEvent: ...


__all__ = [
    "ACCOUNT_ACTIVE",
    "CalendarEvent",
    "CalendarProvider",
    "ConnectLinker",
    "ConnectedAccount",
]
```

- [ ] **Step 5: Write the connector**

Create `src/personal_organizer/providers/calendar/composio.py`:

```python
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
                return await anyio.to_thread.run_sync(_untracked, fn, abandon_on_cancel=True)
        except TimeoutError:
            # Ours (fail_after), or the SDK's ComposioSDKTimeoutError, which is also one.
            raise CalendarProviderUnavailableError("Calendar provider timed out") from None
        except (composio_client.ComposioError, composio_exceptions.ComposioError) as exc:
            raise _translate(exc) from exc

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
        return ConnectedAccount(
            id=str(record.id),
            user_id=str(record.user_id),
            auth_config_id=str(record.auth_config.id),
            status=str(record.status),
        )


__all__ = ["ComposioConnector", "ConnectedAccount"]
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `uv run pytest tests/unit/test_composio_connector.py -v`
Expected: all PASS. If a request path assertion fails, the pinned SDK changed: fix the test's
route constants from the request the SDK actually made (`api.requests`), not the connector.

- [ ] **Step 7: Lint, type-check, commit**

```bash
uv run ruff format src tests && uv run ruff check src tests && uv run mypy
git add src/personal_organizer/core/errors.py src/personal_organizer/interfaces/calendar.py \
  src/personal_organizer/providers/calendar/composio.py tests/unit/test_composio_connector.py
git commit -m "feat: ConnectLinker and the Composio connector, telemetry off"
```

---

### Task 2: Scrub `/connect/*` from logs and Sentry

Onboarding links carry a bearer token in their path, and Composio's callback carries our signed
state in its query. Today `_JWT`/`_LONG_TOKEN` happen to match an itsdangerous token by shape.
These rules match by **position**, so a shorter or differently shaped token is scrubbed too.
Sentry and the logs share `scrub_text`, so one change covers both.

**Files:**
- Modify: `src/personal_organizer/observability/redaction.py`
- Test: `tests/unit/test_redaction.py`, `tests/unit/test_sentry_scrubbing.py`

**Interfaces:**
- Produces: `scrub_text` replaces `/connect/<token>` with `/connect/<redacted:link>` (except
  `/connect/callback` and route templates such as `/connect/{token}`), and `state=<value>` with
  `state=<redacted:state>`. `SAFE_KEYS` gains `connection_id` and `composio_enabled`.
  `OPAQUE_ID_KEYS` gains `connection_id`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_redaction.py`:

```python
class TestConnectUrls:
    """Onboarding links carry a bearer token in their path, and Composio's callback our signed
    state in its query. Both go by position, whatever the token's length or shape."""

    @pytest.mark.parametrize(
        "token",
        ["abc.def.ghi", "eyJ0IjoiMSJ9.ZxY1aQ.c2lnbmF0dXJlLXNpZ25hdHVyZS1zaWduYXR1cmU"],
    )
    def test_a_link_token_is_removed(self, token: str) -> None:
        assert (
            scrub_text(f"GET https://po.test/connect/{token} 410")
            == "GET https://po.test/connect/<redacted:link> 410"
        )

    def test_the_state_is_removed_and_the_account_id_kept(self) -> None:
        assert (
            scrub_text("/connect/callback?state=abc.def.ghi&connected_account_id=ca_1")
            == "/connect/callback?state=<redacted:state>&connected_account_id=ca_1"
        )

    @pytest.mark.parametrize(
        "value", ["/connect/{token}", "/connect/callback", "/webhooks/gowa", "disconnect/now"]
    )
    def test_route_templates_and_other_paths_survive(self, value: str) -> None:
        assert scrub_text(value) == value

    def test_a_logged_path_is_scrubbed(self) -> None:
        rendered = _render({"event": "request.invalid", "path": "/connect/abc.def.ghi"})
        assert "abc.def.ghi" not in rendered

    def test_a_connection_id_survives_for_correlation(self) -> None:
        connection_id = str(uuid4())
        assert connection_id in _render({"event": "connect.connected", "connection_id": connection_id})
```

Append to class `TestScrubEvent` in `tests/unit/test_sentry_scrubbing.py`:

```python
    def test_connect_tokens_are_removed_from_request_urls(self) -> None:
        """D4: the link token is in the URL's path, the callback's state in its query."""
        token = "abc.def.ghi"
        event: Any = {
            "transaction": "/connect/{token}",
            "request": {
                "url": f"https://po.test/connect/{token}",
                "query_string": f"state={token}&connected_account_id=ca_1",
            },
        }
        scrubbed: Any = scrub_event(event, {})
        assert all(token not in text for text in _flatten(scrubbed))
        assert scrubbed["transaction"] == "/connect/{token}"
        assert scrubbed["request"]["query_string"].endswith("&connected_account_id=ca_1")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/unit/test_redaction.py tests/unit/test_sentry_scrubbing.py -v -k "Connect or connect"`
Expected: FAIL. `abc.def.ghi` survives (too short for the token rules), and the
`connection_id` value is shape-redacted.

- [ ] **Step 3: Implement**

In `src/personal_organizer/observability/redaction.py`:

1. In `SAFE_KEYS`, under the `# messaging` group after `"outbox_id",` add `"connection_id",`.
   After `"gowa_enabled",` add `"composio_enabled",`.
2. In `OPAQUE_ID_KEYS`, after `"outbox_id",` add:

```python
        # Our UUID for a calendar connection: what the connect callback and the
        # onboarding:connected task log to tie a page view to the message that followed.
        "connection_id",
```

3. After the `_CALL_STRING` definition, add:

```python
# Onboarding links carry a bearer token in their path (/connect/<token>), and Composio's
# callback our signed state in its query (?state=<token>). The token rules below would catch
# today's shape by length; these catch it by position. A route template (/connect/{token}) and
# the callback path itself are kept.
_CONNECT_TOKEN = re.compile(r"(/connect/)(?!callback\b|\{)[^/?#\s\"'<>]+")
_STATE_PARAM = re.compile(r"(\bstate=)[^&#\s\"'<>]+")
```

4. In `scrub_text`, directly after the `_CALL_STRING.sub(...)` line, add:

```python
    value = _CONNECT_TOKEN.sub(r"\1<redacted:link>", value)
    value = _STATE_PARAM.sub(r"\1<redacted:state>", value)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/unit/test_redaction.py tests/unit/test_sentry_scrubbing.py tests/api/test_no_pii_in_logs.py -v`
Expected: all PASS. The existing `OPAQUE_ID_KEYS <= SAFE_KEYS` and disjointness tests cover the
new keys.

- [ ] **Step 5: Commit**

```bash
uv run ruff format src tests && uv run ruff check src tests && uv run mypy
git add src/personal_organizer/observability/redaction.py tests/unit/test_redaction.py \
  tests/unit/test_sentry_scrubbing.py
git commit -m "feat: scrub connect link tokens and callback state from logs and Sentry"
```

---

### Task 3: The pages, their copy and D4's headers

The CSP is `default-src 'none'` plus what the page actually needs:

- `style-src 'sha256-…'` for the one inline `<style>`, by hash, not `'unsafe-inline'`.
- `form-action 'self' https:`. The button POSTs to us, and the 303 goes on to Composio and may
  hop on to Google. Chrome applies `form-action` to every redirect of a form submission, so
  `'self'` alone would block the flow. Pinning Composio's and Google's hosts would break the day
  either adds a hop. `https:` still refuses `http:`, `data:` and `javascript:` targets, and with
  no script and autoescaped output, nothing on the page can retarget the form.
- `base-uri 'none'` and `frame-ancestors 'none'` (plus `X-Frame-Options: DENY` for old
  browsers), so nobody can frame the button and click-jack it.

**Files:**
- Create: `src/personal_organizer/onboarding/page_text.py`
- Create: `src/personal_organizer/api/pages.py`
- Create: `src/personal_organizer/api/templates/base.html`, `connect.html`, `message.html`
- Test: `tests/unit/test_connect_pages.py`

**Interfaces:**
- Produces:
  - `onboarding.page_text`: `LANGUAGES: Final = ("he", "en")`, `BILINGUAL: Final = LANGUAGES`,
    `PAGE_TEXT: Final[dict[str, dict[str, dict[str, str]]]]` with keys `connect` (`title`,
    `body`, `number_hint`, `button`), `connected`, `link_unusable`, `failed`, `not_ready`,
    `unavailable_new_link`, `unavailable_retry` (each `title`, `body`).
    `def page_language(language: str | None) -> str`,
    `def text_direction(language: str) -> str`,
    `def page_text(key: str, language: str | None) -> dict[str, str]`.
  - `api.pages`: `PAGE_CSS: Final[str]`, `CONTENT_SECURITY_POLICY: Final[str]`,
    `SECURITY_HEADERS: Final[dict[str, str]]`,
    `def connect_page(*, token: str, language: str, phone_suffix: str | None) -> HTMLResponse`,
    `def message_page(key: str, *, status_code: int, language: str | None = None) ->
    HTMLResponse` (`language=None` renders both languages, Hebrew first),
    `def redirect(url: str) -> Response` (303).

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_connect_pages.py`:

```python
"""The connect pages: D4's headers, a CSP that matches the page, escaping, direction."""

from __future__ import annotations

import base64
import hashlib
import html
import re

import markupsafe
import pytest
from fastapi import Response

from personal_organizer.api.pages import (
    CONTENT_SECURITY_POLICY,
    PAGE_CSS,
    SECURITY_HEADERS,
    connect_page,
    message_page,
    redirect,
)
from personal_organizer.onboarding.page_text import LANGUAGES, PAGE_TEXT, page_text

TOKEN = "eyJ0IjoiMSJ9.ZxY1aQ.c2lnbmF0dXJl"


def body(response: Response) -> str:
    return bytes(response.body).decode()


PAGES: dict[str, Response] = {
    "connect": connect_page(token=TOKEN, language="he", phone_suffix="4567"),
    "message": message_page("link_unusable", status_code=410),
    "redirect": redirect("https://connect.composio.test/link/ln_1"),
}


class TestHeaders:
    @pytest.mark.parametrize("name", sorted(PAGES))
    def test_every_response_carries_d4s_headers(self, name: str) -> None:
        for header, value in SECURITY_HEADERS.items():
            assert PAGES[name].headers[header] == value

    def test_the_headers_are_the_ones_d4_names(self) -> None:
        assert SECURITY_HEADERS["Referrer-Policy"] == "no-referrer"
        assert SECURITY_HEADERS["Cache-Control"] == "no-store"
        assert CONTENT_SECURITY_POLICY.startswith("default-src 'none'; ")
        assert "form-action 'self' https:" in CONTENT_SECURITY_POLICY
        assert "unsafe-inline" not in CONTENT_SECURITY_POLICY

    def test_the_redirect_is_a_303_to_the_url(self) -> None:
        assert PAGES["redirect"].status_code == 303
        assert PAGES["redirect"].headers["location"] == "https://connect.composio.test/link/ln_1"


class TestStyle:
    def test_the_csp_hash_matches_the_style_the_page_carries(self) -> None:
        """Browsers hash the exact text of the <style> element. Any drift and the page is
        unstyled, silently."""
        match = re.search(r"<style>(.*?)</style>", body(PAGES["connect"]), re.S)
        assert match is not None
        digest = base64.b64encode(hashlib.sha256(match.group(1).encode()).digest()).decode()
        assert f"style-src 'sha256-{digest}'" in CONTENT_SECURITY_POLICY

    def test_autoescaping_leaves_the_stylesheet_unchanged(self) -> None:
        assert str(markupsafe.escape(PAGE_CSS)) == PAGE_CSS


class TestContent:
    @pytest.mark.parametrize("name", ["connect", "message"])
    def test_nothing_is_loaded_from_anywhere(self, name: str) -> None:
        text = body(PAGES[name]).lower()
        for marker in ("<script", "<link", "<img", "src=", "http:", "https:", "@import"):
            assert marker not in text

    def test_the_button_posts_back_to_the_link(self) -> None:
        assert f'<form method="post" action="/connect/{TOKEN}">' in body(PAGES["connect"])

    def test_the_token_is_escaped(self) -> None:
        page = connect_page(token='"><script>alert(1)</script>', language="en", phone_suffix=None)
        assert "<script>" not in body(page)

    def test_hebrew_is_right_to_left(self) -> None:
        assert '<html lang="he" dir="rtl">' in body(PAGES["connect"])

    def test_english_is_left_to_right(self) -> None:
        page = connect_page(token=TOKEN, language="en", phone_suffix=None)
        assert '<html lang="en" dir="ltr">' in body(page)

    def test_an_unknown_language_gets_english(self) -> None:
        page = connect_page(token=TOKEN, language="fr", phone_suffix=None)
        assert '<html lang="en" dir="ltr">' in body(page)

    def test_the_last_digits_of_the_number_are_shown(self) -> None:
        """So someone handed another person's link sees it is not theirs."""
        assert "4567" in body(PAGES["connect"])

    def test_no_number_no_hint(self) -> None:
        page = connect_page(token=TOKEN, language="en", phone_suffix=None)
        assert page_text("connect", "en")["number_hint"].split("{")[0] not in html.unescape(
            body(page)
        )

    def test_a_page_for_nobody_in_particular_has_both_languages(self) -> None:
        text = html.unescape(body(PAGES["message"]))
        assert '<section lang="he" dir="rtl">' in text
        assert '<section lang="en" dir="ltr">' in text
        assert text.index(page_text("link_unusable", "he")["title"]) < text.index(
            page_text("link_unusable", "en")["title"]
        )

    def test_a_page_for_a_known_tenant_has_one(self) -> None:
        text = html.unescape(body(message_page("connected", status_code=200, language="en")))
        assert page_text("connected", "en")["title"] in text
        assert page_text("connected", "he")["title"] not in text


class TestCopy:
    @pytest.mark.parametrize("key", sorted(PAGE_TEXT))
    def test_every_page_has_both_languages_with_the_same_fields(self, key: str) -> None:
        assert set(PAGE_TEXT[key]) == set(LANGUAGES)
        assert PAGE_TEXT[key]["he"].keys() == PAGE_TEXT[key]["en"].keys()
        for language in LANGUAGES:
            assert all(value.strip() for value in PAGE_TEXT[key][language].values())
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/unit/test_connect_pages.py -v`
Expected: collection error, `ModuleNotFoundError: No module named 'personal_organizer.api.pages'`.

- [ ] **Step 3: Write the copy**

Create `src/personal_organizer/onboarding/page_text.py`:

```python
"""Copy for the connect pages, in both languages (D11).

The chat strings are in :mod:`personal_organizer.messaging.onboarding_text` and are written for
WhatsApp. These are page copy, rendered into autoescaped HTML. A page shown before we know who
is asking (a refused link, a refused callback) carries both languages, Hebrew first.

``connect.number_hint`` takes ``{last_digits}``.
"""

from __future__ import annotations

from typing import Final

LANGUAGES: Final = ("he", "en")
BILINGUAL: Final = LANGUAGES
_RIGHT_TO_LEFT: Final = frozenset({"he"})

PAGE_TEXT: Final[dict[str, dict[str, dict[str, str]]]] = {
    "connect": {
        "he": {
            "title": "חיבור יומן Google",
            "body": "הכפתור יעביר אתכם ל-Google כדי לאשר גישה ליומן. "
            "אפשר לבטל את הגישה בכל עת בחשבון Google.",
            "number_hint": "עבור מספר הוואטסאפ שמסתיים ב-{last_digits}",
            "button": "המשך ל-Google",
        },
        "en": {
            "title": "Connect Google Calendar",
            "body": "This button takes you to Google to allow access to your calendar. "
            "You can remove access at any time in your Google Account.",
            "number_hint": "For the WhatsApp number ending in {last_digits}",
            "button": "Continue to Google",
        },
    },
    "connected": {
        "he": {"title": "היומן מחובר", "body": "הכול מוכן. אפשר לחזור לוואטסאפ."},
        "en": {"title": "Calendar connected", "body": "All set. You can go back to WhatsApp now."},
    },
    "link_unusable": {
        "he": {
            "title": "הקישור כבר לא בתוקף",
            "body": "פג תוקפו או שכבר השתמשו בו. "
            "שלחו הודעה כלשהי לבוט בוואטסאפ ותקבלו קישור חדש.",
        },
        "en": {
            "title": "This link has expired",
            "body": "It has expired or was already used. "
            "Send any message to the bot on WhatsApp and it will send you a new one.",
        },
    },
    "failed": {
        "he": {
            "title": "היומן לא חובר",
            "body": "שלחו הודעה כלשהי לבוט בוואטסאפ ותקבלו קישור חדש.",
        },
        "en": {
            "title": "Calendar not connected",
            "body": "Send any message to the bot on WhatsApp and it will send you a new link.",
        },
    },
    "not_ready": {
        "he": {
            "title": "החיבור עוד לא הושלם",
            "body": "אם סיימתם להתחבר ב-Google, רעננו את הדף בעוד כמה שניות. "
            "אחרת, שלחו הודעה כלשהי לבוט בוואטסאפ ותקבלו קישור חדש.",
        },
        "en": {
            "title": "Not connected yet",
            "body": "If you finished signing in with Google, reload this page in a few seconds. "
            "Otherwise, send any message to the bot on WhatsApp for a new link.",
        },
    },
    "unavailable_new_link": {
        "he": {
            "title": "משהו השתבש אצלנו",
            "body": "נסו שוב בעוד דקה: שלחו הודעה כלשהי לבוט בוואטסאפ ותקבלו קישור חדש.",
        },
        "en": {
            "title": "Something went wrong on our side",
            "body": "Please try again in a minute: "
            "send any message to the bot on WhatsApp for a new link.",
        },
    },
    "unavailable_retry": {
        "he": {"title": "משהו השתבש אצלנו", "body": "רעננו את הדף בעוד דקה."},
        "en": {
            "title": "Something went wrong on our side",
            "body": "Please reload this page in a minute.",
        },
    },
}


def page_language(language: str | None) -> str:
    """The tenant's language if a page exists in it, else English."""
    return language if language in LANGUAGES else "en"


def text_direction(language: str) -> str:
    return "rtl" if language in _RIGHT_TO_LEFT else "ltr"


def page_text(key: str, language: str | None) -> dict[str, str]:
    return PAGE_TEXT[key][page_language(language)]


__all__ = ["BILINGUAL", "LANGUAGES", "PAGE_TEXT", "page_language", "page_text", "text_direction"]
```

- [ ] **Step 4: Write the templates**

Create `src/personal_organizer/api/templates/base.html`:

```html
<!doctype html>
<html lang="{{ lang }}" dir="{{ direction }}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="referrer" content="no-referrer">
<meta name="robots" content="noindex, nofollow">
<title>{{ title }}</title>
<style>{{ css }}</style>
</head>
<body>
<main>
{% block body %}{% endblock %}
</main>
</body>
</html>
```

Create `src/personal_organizer/api/templates/connect.html`:

```html
{% extends "base.html" %}
{% block body %}
<h1>{{ text.title }}</h1>
<p>{{ text.body }}</p>
{% if number_hint %}
<p>{{ number_hint }}</p>
{% endif %}
<form method="post" action="/connect/{{ token }}">
<button type="submit">{{ text.button }}</button>
</form>
{% endblock %}
```

Create `src/personal_organizer/api/templates/message.html`:

```html
{% extends "base.html" %}
{% block body %}
{% for section in sections %}
<section lang="{{ section.lang }}" dir="{{ section.direction }}">
<h1>{{ section.title }}</h1>
<p>{{ section.body }}</p>
</section>
{% endfor %}
{% endblock %}
```

- [ ] **Step 5: Write the renderer**

Create `src/personal_organizer/api/pages.py`:

```python
"""The connect pages: rendering, and the headers every response carries (D4).

- ``Referrer-Policy: no-referrer``: the page URL holds the link token, and the next hops are
  Composio and Google.
- ``Cache-Control: no-store``: on a shared phone, the back button must not bring back a page
  holding a live token.
- A CSP of ``default-src 'none'``, since the page loads nothing. Allowed back in: its one inline
  ``<style>``, by hash; ``form-action 'self' https:``, because Chrome checks ``form-action`` on
  every redirect of a form submission and ours redirects to Composio and on to Google; and
  nothing else. ``frame-ancestors 'none'`` and ``X-Frame-Options`` stop anyone framing the
  button.

Templates are autoescaped with ``StrictUndefined``. Copy is in
:mod:`personal_organizer.onboarding.page_text`.
"""

from __future__ import annotations

import base64
import hashlib
from pathlib import Path
from typing import Final

from fastapi import Response
from fastapi.responses import HTMLResponse
from jinja2 import Environment, FileSystemLoader, StrictUndefined
from starlette.status import HTTP_200_OK, HTTP_303_SEE_OTHER

from personal_organizer.onboarding.page_text import (
    BILINGUAL,
    page_language,
    page_text,
    text_direction,
)

#: The whole stylesheet. It has no quotes, angle brackets or ampersands, so autoescaping leaves
#: it byte-identical and the hash below is the hash of what the browser sees.
PAGE_CSS: Final = (
    "body{font-family:system-ui,sans-serif;max-width:32rem;margin:2rem auto;"
    "padding:0 1rem;line-height:1.5;color:#1a1a1a;background:#fff}"
    "h1{font-size:1.4rem}"
    "button{font-size:1.1rem;padding:.8rem 1.4rem;border:0;border-radius:.5rem;"
    "background:#1a73e8;color:#fff;width:100%}"
    "section+section{margin-top:2rem;border-top:1px solid #ddd;padding-top:1rem}"
)


def _sha256(text: str) -> str:
    return base64.b64encode(hashlib.sha256(text.encode()).digest()).decode()


CONTENT_SECURITY_POLICY: Final = "; ".join(
    (
        "default-src 'none'",
        f"style-src 'sha256-{_sha256(PAGE_CSS)}'",
        "form-action 'self' https:",
        "base-uri 'none'",
        "frame-ancestors 'none'",
    )
)

SECURITY_HEADERS: Final[dict[str, str]] = {
    "Content-Security-Policy": CONTENT_SECURITY_POLICY,
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "X-Robots-Tag": "noindex, nofollow",
}

_templates = Environment(
    loader=FileSystemLoader(Path(__file__).parent / "templates"),
    autoescape=True,
    undefined=StrictUndefined,
    trim_blocks=True,
    lstrip_blocks=True,
)


def _html(template: str, status_code: int, **context: object) -> HTMLResponse:
    rendered = _templates.get_template(template).render(css=PAGE_CSS, **context)
    return HTMLResponse(rendered, status_code=status_code, headers=SECURITY_HEADERS)


def connect_page(*, token: str, language: str, phone_suffix: str | None) -> HTMLResponse:
    """The one-button page. ``phone_suffix`` names the number the link was issued to."""
    lang = page_language(language)
    text = page_text("connect", lang)
    hint = text["number_hint"].format(last_digits=phone_suffix) if phone_suffix else None
    return _html(
        "connect.html",
        HTTP_200_OK,
        lang=lang,
        direction=text_direction(lang),
        title=text["title"],
        text=text,
        token=token,
        number_hint=hint,
    )


def message_page(key: str, *, status_code: int, language: str | None = None) -> HTMLResponse:
    """A title and a line. With no ``language``, both languages, Hebrew first."""
    languages: tuple[str, ...] = BILINGUAL if language is None else (page_language(language),)
    sections = [
        {"lang": lang, "direction": text_direction(lang), **page_text(key, lang)}
        for lang in languages
    ]
    first = sections[0]
    return _html(
        "message.html",
        status_code,
        lang=first["lang"],
        direction=first["direction"],
        title=first["title"],
        sections=sections,
    )


def redirect(url: str) -> Response:
    """303 to ``url``. ``Referrer-Policy`` on the redirect itself keeps the token off the next hop."""
    return Response(status_code=HTTP_303_SEE_OTHER, headers={**SECURITY_HEADERS, "Location": url})


__all__ = [
    "CONTENT_SECURITY_POLICY",
    "PAGE_CSS",
    "SECURITY_HEADERS",
    "connect_page",
    "message_page",
    "redirect",
]
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `uv run pytest tests/unit/test_connect_pages.py -v`
Expected: all PASS.

- [ ] **Step 7: Commit**

```bash
uv run ruff format src tests && uv run ruff check src tests && uv run mypy
git add src/personal_organizer/onboarding/page_text.py src/personal_organizer/api/pages.py \
  src/personal_organizer/api/templates tests/unit/test_connect_pages.py
git commit -m "feat: connect pages with D4's headers and a hashed stylesheet"
```

---

### Task 4: `GET` and `POST /connect/{token}`, mounted and wired

**Files:**
- Create: `src/personal_organizer/onboarding/connect.py`
- Create: `src/personal_organizer/api/routers/connect.py`
- Modify: `src/personal_organizer/api/app.py`, `src/personal_organizer/api/deps.py`,
  `src/personal_organizer/api/lifespan.py`
- Create: `tests/fixtures/connect.py`
- Test: `tests/api/test_connect.py`, `tests/db/test_connect.py`

**Interfaces:**
- Consumes (contract, PR 2): `db.repositories.tenants.get_tenant(session, tenant_id) -> Tenant |
  None`, `primary_phone(session, tenant_id) -> str | None`, `activate`, `create_tenant`,
  `set_onboarding_step`; `db.repositories.links.consume_link(session, tenant_id, *, nonce, now)
  -> bool`, `create_link(session, tenant_id, *, nonce, expires_at)`;
  `db.repositories.messages.record_inbound(...)`; `db.models.tenant.Tenant`, `NETWORK_WHATSAPP`.
  (PR 3): `onboarding.tokens.LINK_SALT`, `STATE_SALT`, `TokenPayload(tenant_id: UUID, nonce:
  str)`, `sign(payload, *, secret, salt) -> str`, `verify(token, *, secret, salt, max_age_s) ->
  TokenPayload | None`. (Task 1): `ConnectLinker`, `CalendarProvider*Error`. (Task 3):
  `api.pages`, `onboarding.page_text.page_text`.
- Produces:
  - `onboarding.connect`: `@dataclass(frozen=True, slots=True) class ConnectConfig: secret:
    str; base_url: str; auth_config_id: str; link_ttl_s: int` with `@classmethod of(cls,
    settings: Settings) -> ConnectConfig` and property `callback_base -> str`;
    `class LinkPage: language: str; phone_suffix: str | None`;
    `async def open_link(token: str, *, db: Database, config: ConnectConfig) -> LinkPage | None`;
    `async def start_connect(token: str, *, db: Database, linker: ConnectLinker, config:
    ConnectConfig, now: datetime) -> str | None`.
  - `api.deps`: `get_connect_linker(request) -> ConnectLinker`, `ConnectLinkerDep`.
  - `api.routers.connect.router` (`prefix="/connect"`).
  - `app.state.connect_linker`, built in the lifespan when Composio is enabled.
  - `tests/fixtures/connect.py`: `BASE_URL`, `AUTH_CONFIG_ID`, `LINK_SECRET`, `OTHER_SECRET`,
    `REDIRECT_URL`, `ACCOUNT_ID`, `PHONE`, `CONNECT_ENV`, `TRUNCATE_CONNECT_TABLES`,
    `with_connect`, `FakeConnectLinker`, `account`, `link_token`, `state_token`, `connect_app`,
    `seed_tenant`, `make_active`, `suspend`, `seed_link`, `seed_inbound`, `assert_hardened`,
    `assert_page`.

- [ ] **Step 1: Write the shared test fixtures**

Create `tests/fixtures/connect.py`:

```python
"""Shared pieces for the connect tests: settings, a fake Composio, tokens and seeders.

``FakeConnectLinker`` stands in for ``ComposioConnector`` behind the ``ConnectLinker``
protocol. ``tests/unit/test_composio_connector.py`` runs the real SDK. Every other connect
test uses this.
"""

from __future__ import annotations

import html
import secrets
from datetime import UTC, datetime, timedelta
from typing import Final
from uuid import UUID, uuid4

import httpx
from fastapi import FastAPI
from pydantic import SecretStr
from sqlalchemy import insert, update

from personal_organizer.api.app import create_app
from personal_organizer.api.pages import SECURITY_HEADERS
from personal_organizer.core.errors import CalendarProviderError, CalendarProviderRejectedError
from personal_organizer.core.types import TenantId
from personal_organizer.db.engine import Database
from personal_organizer.db.models.channel import ChannelInbox
from personal_organizer.db.models.tenant import NETWORK_WHATSAPP, Tenant
from personal_organizer.db.repositories.links import create_link
from personal_organizer.db.repositories.messages import record_inbound
from personal_organizer.db.repositories.tenants import (
    activate,
    create_tenant,
    set_onboarding_step,
)
from personal_organizer.interfaces.calendar import ACCOUNT_ACTIVE, ConnectedAccount
from personal_organizer.onboarding.page_text import page_text
from personal_organizer.onboarding.tokens import LINK_SALT, STATE_SALT, TokenPayload, sign
from personal_organizer.settings import ComposioSettings, OnboardingSettings, Settings

BASE_URL: Final = "https://po.test"
AUTH_CONFIG_ID: Final = "ac_test"
LINK_SECRET: Final = "connect-test-link-secret-0123456789abcdef"
OTHER_SECRET: Final = "someone-elses-link-secret-0123456789abcdef"
REDIRECT_URL: Final = "https://connect.composio.test/link/ln_test"
ACCOUNT_ID: Final = "ca_test1"
PHONE: Final = "+972501234567"

#: Everything Settings requires once COMPOSIO__ENABLED is true.
CONNECT_ENV: Final[dict[str, str]] = {
    "COMPOSIO__ENABLED": "true",
    "COMPOSIO__API_KEY": "composio-test-key",
    "COMPOSIO__CALENDAR_AUTH_CONFIG_ID": AUTH_CONFIG_ID,
    "APP__PUBLIC_BASE_URL": BASE_URL,
    "ONBOARDING__LINK_SECRET": LINK_SECRET,
}

#: Every table a connect test writes. Tenants cascade to identities, links, connections and
#: messages. Run as the owner: TRUNCATE is not subject to RLS.
TRUNCATE_CONNECT_TABLES: Final = (
    "TRUNCATE tenants, channel_outbox, channel_inbox, procrastinate_jobs CASCADE"
)


def with_connect(settings: Settings) -> Settings:
    """``settings`` (the real database's) with Composio and onboarding links switched on."""
    return settings.model_copy(
        update={
            "app": settings.app.model_copy(update={"public_base_url": BASE_URL}),
            "composio": ComposioSettings(
                enabled=True,
                api_key=SecretStr("composio-test-key"),
                calendar_auth_config_id=AUTH_CONFIG_ID,
            ),
            "onboarding": OnboardingSettings(link_secret=SecretStr(LINK_SECRET)),
        }
    )


class FakeConnectLinker:
    """Records every call. ``accounts`` is what Composio's API says about each account;
    ``failure``, while set, is raised by both methods."""

    def __init__(self) -> None:
        self.links: list[dict[str, str]] = []
        self.lookups: list[str] = []
        self.accounts: dict[str, ConnectedAccount] = {}
        self.failure: CalendarProviderError | None = None

    async def link(self, *, user_id: str, auth_config_id: str, callback_url: str) -> str:
        self.links.append(
            {"user_id": user_id, "auth_config_id": auth_config_id, "callback_url": callback_url}
        )
        if self.failure is not None:
            raise self.failure
        return REDIRECT_URL

    async def get_account(self, connected_account_id: str) -> ConnectedAccount:
        self.lookups.append(connected_account_id)
        if self.failure is not None:
            raise self.failure
        try:
            return self.accounts[connected_account_id]
        except KeyError:
            raise CalendarProviderRejectedError(404) from None


def account(
    tenant_id: UUID,
    *,
    account_id: str = ACCOUNT_ID,
    user_id: str | None = None,
    auth_config_id: str = AUTH_CONFIG_ID,
    status: str = ACCOUNT_ACTIVE,
) -> ConnectedAccount:
    """An account as Composio would describe it, by default this tenant's and ACTIVE."""
    return ConnectedAccount(
        id=account_id,
        user_id=user_id if user_id is not None else str(tenant_id),
        auth_config_id=auth_config_id,
        status=status,
    )


def link_token(
    tenant_id: UUID | None = None, *, nonce: str = "nonce-1", secret: str = LINK_SECRET
) -> str:
    payload = TokenPayload(tenant_id=tenant_id or uuid4(), nonce=nonce)
    return sign(payload, secret=secret, salt=LINK_SALT)


def state_token(
    tenant_id: UUID | None = None, *, nonce: str = "nonce-1", secret: str = LINK_SECRET
) -> str:
    payload = TokenPayload(tenant_id=tenant_id or uuid4(), nonce=nonce)
    return sign(payload, secret=secret, salt=STATE_SALT)


def connect_app(
    settings: Settings, *, db: object, linker: FakeConnectLinker, queue: object
) -> FastAPI:
    """The api with stubbed state and no lifespan, as ``tests/api/conftest.py`` builds it."""
    application = create_app(settings)
    application.state.db = db
    application.state.procrastinate = queue
    application.state.connect_linker = linker
    return application


def assert_hardened(response: httpx.Response) -> None:
    for name, value in SECURITY_HEADERS.items():
        assert response.headers[name] == value


def assert_page(response: httpx.Response, key: str, *languages: str) -> None:
    """The page ``key`` was rendered, in each of ``languages``."""
    text = html.unescape(response.text)
    for language in languages:
        assert page_text(key, language)["title"] in text


async def seed_tenant(
    db: Database,
    *,
    phone: str | None = PHONE,
    language: str = "he",
    step: str | None = "connect",
    external_id: str | None = None,
) -> UUID:
    """An onboarding tenant at ``step``. Pass ``external_id`` when ``phone`` is None."""
    async with db.system_session() as session:
        tenant_id = await create_tenant(
            session,
            network=NETWORK_WHATSAPP,
            external_id=external_id or f"tel:{phone}",
            phone=phone,
            language=language,
        )
    async with db.tenant_session(TenantId(tenant_id)) as session:
        await set_onboarding_step(session, tenant_id, step)
    return tenant_id


async def make_active(db: Database, tenant_id: UUID) -> None:
    async with db.tenant_session(TenantId(tenant_id)) as session:
        await activate(session, tenant_id)


async def suspend(db: Database, tenant_id: UUID) -> None:
    async with db.tenant_session(TenantId(tenant_id)) as session:
        await session.execute(
            update(Tenant).where(Tenant.id == tenant_id).values(status="suspended")
        )


async def seed_link(db: Database, tenant_id: UUID, *, expires_at: datetime | None = None) -> str:
    """A stored, unused link row and the token that names it."""
    nonce = secrets.token_urlsafe(16)
    async with db.tenant_session(TenantId(tenant_id)) as session:
        await create_link(
            session,
            tenant_id,
            nonce=nonce,
            expires_at=expires_at or datetime.now(UTC) + timedelta(minutes=15),
        )
    return link_token(tenant_id, nonce=nonce)


async def seed_inbound(
    db: Database, tenant_id: UUID, *, channel: str, sent_at: datetime, phone: str = PHONE
) -> None:
    """An inbound message from the tenant on ``channel``: the inbox row and its D7 copy."""
    async with db.system_session() as session:
        inbox_id = await session.scalar(
            insert(ChannelInbox)
            .values(
                channel=channel,
                provider_message_id=f"msg.{uuid4().hex}",
                sender_key=f"tel:{phone}",
                sender_phone=phone,
                message_type="text",
                body=None,
                sent_at=sent_at,
            )
            .returning(ChannelInbox.id)
        )
    assert inbox_id is not None
    async with db.tenant_session(TenantId(tenant_id)) as session:
        await record_inbound(
            session,
            tenant_id,
            inbox_id=inbox_id,
            channel=channel,
            message_type="text",
            body="hello",
            sent_at=sent_at,
        )
```

- [ ] **Step 2: Write the failing API tests (no database)**

Create `tests/api/test_connect.py`:

```python
"""``/connect`` without a database: everything refused before one is reached.

The fake database has no ``tenant_session``, so a request that reached it would answer 500
instead of the page asserted. That is how "refused before touching the database" is pinned.
The flow against Postgres is ``tests/db/test_connect.py``.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Callable

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from itsdangerous import TimestampSigner

from personal_organizer.api.app import create_app
from personal_organizer.settings import Settings
from tests.api.conftest import FakeDatabase, FakeProcrastinate
from tests.fixtures.connect import (
    CONNECT_ENV,
    OTHER_SECRET,
    FakeConnectLinker,
    assert_hardened,
    assert_page,
    connect_app,
    link_token,
    state_token,
)


@pytest.fixture
def connect_settings(settings_factory: Callable[..., Settings]) -> Settings:
    return settings_factory(**CONNECT_ENV)


@pytest.fixture
def linker() -> FakeConnectLinker:
    return FakeConnectLinker()


@pytest.fixture
def queue() -> FakeProcrastinate:
    return FakeProcrastinate()


@pytest.fixture
async def http(
    connect_settings: Settings,
    linker: FakeConnectLinker,
    queue: FakeProcrastinate,
    make_client: Callable[[FastAPI], AsyncClient],
) -> AsyncIterator[AsyncClient]:
    application = connect_app(connect_settings, db=FakeDatabase(), linker=linker, queue=queue)
    async with make_client(application) as client:
        yield client


def stale(make: Callable[[], str]) -> str:
    """A token signed two hours ago: past both the link's and the state's max age."""
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(TimestampSigner, "get_timestamp", lambda _self: int(time.time()) - 7200)
        return make()


REFUSED_LINKS: dict[str, Callable[[], str]] = {
    "garbage": lambda: "not-a-token",
    "forged": lambda: link_token(secret=OTHER_SECRET),
    "a_state_not_a_link": lambda: state_token(),
    "expired": lambda: stale(link_token),
}


class TestMounting:
    async def test_nothing_is_mounted_while_composio_is_off(
        self, settings: Settings, make_client: Callable[[FastAPI], AsyncClient]
    ) -> None:
        application = create_app(settings)
        application.state.db = FakeDatabase()
        async with make_client(application) as client:
            assert (await client.get("/connect/anything")).status_code == 404
            assert (await client.post("/connect/anything")).status_code == 404
            assert (await client.get("/connect/callback")).status_code == 404


class TestRefusedLinks:
    @pytest.mark.parametrize("kind", sorted(REFUSED_LINKS))
    async def test_the_page_is_refused_in_both_languages(
        self, http: AsyncClient, kind: str
    ) -> None:
        response = await http.get(f"/connect/{REFUSED_LINKS[kind]()}")
        assert response.status_code == 410
        assert_page(response, "link_unusable", "he", "en")
        assert_hardened(response)

    @pytest.mark.parametrize("kind", sorted(REFUSED_LINKS))
    async def test_the_button_is_refused_and_composio_never_called(
        self, http: AsyncClient, linker: FakeConnectLinker, kind: str
    ) -> None:
        response = await http.post(f"/connect/{REFUSED_LINKS[kind]()}")
        assert response.status_code == 410
        assert_hardened(response)
        assert linker.links == []

    async def test_the_refusal_does_not_say_why(self, http: AsyncClient) -> None:
        bodies = {(await http.get(f"/connect/{make()}")).text for make in REFUSED_LINKS.values()}
        assert len(bodies) == 1
```

- [ ] **Step 3: Write the failing database tests**

Create `tests/db/test_connect.py`:

```python
"""The connect flow against Postgres, with a fake Composio and a fake queue."""

from __future__ import annotations

import asyncio
import html
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from httpx import ASGITransport, AsyncClient

from personal_organizer.core.errors import CalendarProviderUnavailableError
from personal_organizer.db.engine import Database
from personal_organizer.onboarding.page_text import page_text
from personal_organizer.onboarding.tokens import LINK_SALT, STATE_SALT, verify
from personal_organizer.settings import Settings
from tests.api.conftest import FakeProcrastinate
from tests.fixtures.connect import (
    AUTH_CONFIG_ID,
    BASE_URL,
    LINK_SECRET,
    REDIRECT_URL,
    TRUNCATE_CONNECT_TABLES,
    FakeConnectLinker,
    assert_hardened,
    assert_page,
    connect_app,
    make_active,
    seed_link,
    seed_tenant,
    with_connect,
)

pytestmark = [pytest.mark.db]


@pytest.fixture
def connect_settings(db_settings: Settings) -> Settings:
    return with_connect(db_settings)


@pytest.fixture
async def db(connect_settings: Settings, owner_conn: Any) -> AsyncIterator[Database]:
    await owner_conn.execute(TRUNCATE_CONNECT_TABLES)
    database = Database(connect_settings)
    try:
        yield database
    finally:
        await database.dispose()
        await owner_conn.execute(TRUNCATE_CONNECT_TABLES)


@pytest.fixture
def linker() -> FakeConnectLinker:
    return FakeConnectLinker()


@pytest.fixture
def queue() -> FakeProcrastinate:
    return FakeProcrastinate()


@pytest.fixture
async def http(
    connect_settings: Settings, db: Database, linker: FakeConnectLinker, queue: FakeProcrastinate
) -> AsyncIterator[AsyncClient]:
    application = connect_app(connect_settings, db=db, linker=linker, queue=queue)
    transport = ASGITransport(app=application, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


class TestConnectPage:
    async def test_it_speaks_the_tenants_language(self, http: AsyncClient, db: Database) -> None:
        tenant_id = await seed_tenant(db, language="he")
        response = await http.get(f"/connect/{await seed_link(db, tenant_id)}")
        assert response.status_code == 200
        assert '<html lang="he" dir="rtl">' in response.text
        assert page_text("connect", "he")["button"] in html.unescape(response.text)
        assert "4567" in response.text  # the last digits of PHONE
        assert_hardened(response)

    async def test_english_reads_left_to_right(self, http: AsyncClient, db: Database) -> None:
        tenant_id = await seed_tenant(db, language="en")
        response = await http.get(f"/connect/{await seed_link(db, tenant_id)}")
        assert '<html lang="en" dir="ltr">' in response.text
        assert page_text("connect", "en")["button"] in html.unescape(response.text)

    async def test_previews_do_not_spend_the_link(
        self, http: AsyncClient, db: Database, linker: FakeConnectLinker
    ) -> None:
        """WhatsApp fetches a link to build its preview. Only the button's POST may spend it."""
        tenant_id = await seed_tenant(db)
        token = await seed_link(db, tenant_id)
        for _ in range(3):
            await http.get(f"/connect/{token}")
        await http.head(f"/connect/{token}")
        response = await http.post(f"/connect/{token}")
        assert response.status_code == 303
        assert len(linker.links) == 1

    async def test_a_tenant_already_connected_is_refused(
        self, http: AsyncClient, db: Database, linker: FakeConnectLinker
    ) -> None:
        tenant_id = await seed_tenant(db)
        token = await seed_link(db, tenant_id)
        await make_active(db, tenant_id)
        assert (await http.get(f"/connect/{token}")).status_code == 410
        assert (await http.post(f"/connect/{token}")).status_code == 410
        assert linker.links == []


class TestStart:
    async def test_the_button_goes_to_composio(
        self, http: AsyncClient, db: Database, linker: FakeConnectLinker
    ) -> None:
        tenant_id = await seed_tenant(db)
        response = await http.post(f"/connect/{await seed_link(db, tenant_id)}")
        assert response.status_code == 303
        assert response.headers["location"] == REDIRECT_URL
        assert_hardened(response)
        [call] = linker.links
        assert call["user_id"] == str(tenant_id)  # the tenant UUID, never a phone number
        assert call["auth_config_id"] == AUTH_CONFIG_ID
        callback = urlsplit(call["callback_url"])
        assert f"{callback.scheme}://{callback.netloc}{callback.path}" == (
            f"{BASE_URL}/connect/callback"
        )
        [state] = parse_qs(callback.query)["state"]
        payload = verify(state, secret=LINK_SECRET, salt=STATE_SALT, max_age_s=60)
        assert payload is not None
        assert payload.tenant_id == tenant_id
        # A state is not a link: the salts keep the two kinds of token apart.
        assert verify(state, secret=LINK_SECRET, salt=LINK_SALT, max_age_s=60) is None

    async def test_a_link_works_once(
        self, http: AsyncClient, db: Database, linker: FakeConnectLinker
    ) -> None:
        tenant_id = await seed_tenant(db)
        url = f"/connect/{await seed_link(db, tenant_id)}"
        first, second = await http.post(url), await http.post(url)
        assert (first.status_code, second.status_code) == (303, 410)
        assert_page(second, "link_unusable", "he", "en")
        assert len(linker.links) == 1

    async def test_two_taps_at_once_make_one_redirect(
        self, http: AsyncClient, db: Database, linker: FakeConnectLinker
    ) -> None:
        tenant_id = await seed_tenant(db)
        url = f"/connect/{await seed_link(db, tenant_id)}"
        responses = await asyncio.gather(http.post(url), http.post(url))
        assert sorted(r.status_code for r in responses) == [303, 410]
        assert len(linker.links) == 1

    async def test_an_expired_link_is_refused(
        self, http: AsyncClient, db: Database, linker: FakeConnectLinker
    ) -> None:
        tenant_id = await seed_tenant(db)
        token = await seed_link(
            db, tenant_id, expires_at=datetime.now(UTC) - timedelta(minutes=1)
        )
        assert (await http.post(f"/connect/{token}")).status_code == 410
        assert linker.links == []

    async def test_a_composio_outage_spends_the_link(
        self, http: AsyncClient, db: Database, linker: FakeConnectLinker
    ) -> None:
        """Spent before Composio is called, so two taps race to one redirect. The user asks
        the bot again, and the connect step issues a fresh link because none is left unused."""
        tenant_id = await seed_tenant(db)
        url = f"/connect/{await seed_link(db, tenant_id)}"
        linker.failure = CalendarProviderUnavailableError("down")
        first = await http.post(url)
        assert first.status_code == 503
        assert_page(first, "unavailable_new_link", "he", "en")
        assert_hardened(first)
        linker.failure = None
        assert (await http.post(url)).status_code == 410
```

- [ ] **Step 4: Run the tests to verify they fail**

Run: `uv run pytest tests/api/test_connect.py tests/db/test_connect.py -v`
Expected: `TestMounting` passes (nothing mounts yet). The rest FAIL with 404 instead of 410,
200 or 303. With Postgres down, the `tests/db` file skips: bring it up first (Global
Constraints).

- [ ] **Step 5: Write the service**

Create `src/personal_organizer/onboarding/connect.py`:

```python
"""The connect flow behind ``/connect``: open a link, then hand the browser to Composio.

D4: a link is spent on POST, never on GET, because WhatsApp fetches links to build previews.
:func:`open_link` reads (the tenant's language and number, for the page) and writes nothing.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Final
from urllib.parse import urlencode

from personal_organizer.core.errors import ConfigError
from personal_organizer.core.types import TenantId
from personal_organizer.db.engine import Database
from personal_organizer.db.repositories.links import consume_link
from personal_organizer.db.repositories.tenants import get_tenant, primary_phone
from personal_organizer.interfaces.calendar import ConnectLinker
from personal_organizer.onboarding.tokens import (
    LINK_SALT,
    STATE_SALT,
    TokenPayload,
    sign,
    verify,
)
from personal_organizer.settings import Settings

#: Only a tenant still onboarding may use a link. An active one has connected already, through
#: another link; reconnecting arrives with the calendar-read iteration.
_LINKABLE: Final = "onboarding"
#: How many trailing digits of the number the page shows: enough for someone handed another
#: person's link to see that it is not theirs before connecting their Google account to it.
PHONE_SUFFIX_DIGITS: Final = 4


@dataclass(frozen=True, slots=True)
class ConnectConfig:
    """The four settings the flow needs, narrowed from optional. Composio being enabled makes
    them required (``Settings._composio_is_complete``), and the router mounts only then."""

    secret: str
    base_url: str
    auth_config_id: str
    link_ttl_s: int

    @classmethod
    def of(cls, settings: Settings) -> ConnectConfig:
        secret = settings.onboarding.link_secret
        base_url = settings.app.public_base_url
        auth_config_id = settings.composio.calendar_auth_config_id
        if secret is None or base_url is None or auth_config_id is None:
            msg = "The connect pages need the settings COMPOSIO__ENABLED requires"
            raise ConfigError(msg)
        return cls(
            secret=secret.get_secret_value(),
            base_url=base_url,
            auth_config_id=auth_config_id,
            link_ttl_s=settings.onboarding.link_ttl_s,
        )

    @property
    def callback_base(self) -> str:
        return f"{self.base_url}/connect/callback"


@dataclass(frozen=True, slots=True)
class LinkPage:
    language: str
    phone_suffix: str | None


def _link_payload(token: str, config: ConnectConfig) -> TokenPayload | None:
    return verify(token, secret=config.secret, salt=LINK_SALT, max_age_s=config.link_ttl_s)


async def open_link(token: str, *, db: Database, config: ConnectConfig) -> LinkPage | None:
    """What the button page shows, or ``None`` for a link that cannot be used. Writes nothing.

    It checks the signature and the tenant, not whether the link row is spent or expired:
    there is no read for that, and the POST is the authority either way.
    """
    payload = _link_payload(token, config)
    if payload is None:
        return None
    async with db.tenant_session(TenantId(payload.tenant_id)) as session:
        tenant = await get_tenant(session, payload.tenant_id)
        if tenant is None or tenant.status != _LINKABLE:
            return None
        phone = await primary_phone(session, payload.tenant_id)
    return LinkPage(
        language=tenant.language,
        phone_suffix=phone[-PHONE_SUFFIX_DIGITS:] if phone else None,
    )


async def start_connect(
    token: str, *, db: Database, linker: ConnectLinker, config: ConnectConfig, now: datetime
) -> str | None:
    """Spend the link and return Composio's URL, or ``None`` if the link cannot be used.

    The link is spent *before* Composio is called, in its own transaction, so two taps racing
    each other get one redirect between them: ``consume_link`` is one atomic UPDATE. The cost:
    if Composio then fails, this link is gone, and the user asks the bot for a new one, which
    the connect step issues because none is left unused.

    Raises :class:`~personal_organizer.core.errors.CalendarProviderError` from ``linker``.
    """
    payload = _link_payload(token, config)
    if payload is None:
        return None
    tenant_id = payload.tenant_id
    async with db.tenant_session(TenantId(tenant_id)) as session:
        tenant = await get_tenant(session, tenant_id)
        if tenant is None or tenant.status != _LINKABLE:
            return None
        if not await consume_link(session, tenant_id, nonce=payload.nonce, now=now):
            return None
    state = sign(
        TokenPayload(tenant_id=tenant_id, nonce=payload.nonce),
        secret=config.secret,
        salt=STATE_SALT,
    )
    return await linker.link(
        user_id=str(tenant_id),
        auth_config_id=config.auth_config_id,
        callback_url=f"{config.callback_base}?{urlencode({'state': state})}",
    )


__all__ = [
    "PHONE_SUFFIX_DIGITS",
    "ConnectConfig",
    "LinkPage",
    "open_link",
    "start_connect",
]
```

- [ ] **Step 6: Add the dependency**

In `src/personal_organizer/api/deps.py`:

- add `from personal_organizer.interfaces.calendar import ConnectLinker` to the imports;
- after `get_ingress_store`, add:

```python
def get_connect_linker(request: Request) -> ConnectLinker:
    """Composio, built by the lifespan when ``COMPOSIO__ENABLED``. Tests set a fake."""
    linker: ConnectLinker = request.app.state.connect_linker
    return linker
```

- after `IngressStoreDep = …`, add
  `ConnectLinkerDep = Annotated[ConnectLinker, Depends(get_connect_linker)]`;
- add `"ConnectLinkerDep"` and `"get_connect_linker"` to `__all__`, keeping it sorted.

- [ ] **Step 7: Write the router**

Create `src/personal_organizer/api/routers/connect.py`:

```python
"""``/connect``: the page a WhatsApp link opens, and the hop to Composio.

Mounted only when ``COMPOSIO__ENABLED`` (see :func:`create_app`).

- ``GET /connect/{token}`` renders one button and changes nothing. WhatsApp fetches links to
  build previews, so a GET that spent the link would spend it before the user saw it (D4).
- ``POST /connect/{token}`` spends the link and answers 303 to Composio.

Every response is built by :mod:`personal_organizer.api.pages`, which sets D4's headers. A
refused link gets a page that says what to do next and never why. The reason goes to the log.
"""

from __future__ import annotations

from datetime import UTC, datetime

import sentry_sdk
import structlog
from fastapi import APIRouter, Response
from starlette.status import HTTP_410_GONE, HTTP_503_SERVICE_UNAVAILABLE

from personal_organizer.api import pages
from personal_organizer.api.deps import ConnectLinkerDep, DbDep, SettingsDep
from personal_organizer.core.errors import (
    CalendarProviderRejectedError,
    CalendarProviderUnavailableError,
)
from personal_organizer.onboarding.connect import ConnectConfig, open_link, start_connect

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/connect", tags=["connect"])


@router.get("/{token}", summary="The connect page. Reads only: link previews fetch it.")
async def connect_page(token: str, db: DbDep, settings: SettingsDep) -> Response:
    page = await open_link(token, db=db, config=ConnectConfig.of(settings))
    if page is None:
        log.info("connect.page_refused")
        return pages.message_page("link_unusable", status_code=HTTP_410_GONE)
    return pages.connect_page(token=token, language=page.language, phone_suffix=page.phone_suffix)


@router.post("/{token}", summary="Spend the link and send the browser to Composio.")
async def start(
    token: str, db: DbDep, linker: ConnectLinkerDep, settings: SettingsDep
) -> Response:
    try:
        redirect_url = await start_connect(
            token,
            db=db,
            linker=linker,
            config=ConnectConfig.of(settings),
            now=datetime.now(UTC),
        )
    except CalendarProviderUnavailableError as exc:
        log.warning("connect.link_provider_unavailable", error_type=type(exc).__name__)
        return pages.message_page("unavailable_new_link", status_code=HTTP_503_SERVICE_UNAVAILABLE)
    except CalendarProviderRejectedError as exc:
        # Our configuration (API key, auth config), not the user's doing: someone must look.
        sentry_sdk.capture_exception(exc)
        log.error(
            "connect.link_provider_rejected",
            error_type=type(exc).__name__,
            status_code=exc.status_code,
        )
        return pages.message_page("unavailable_new_link", status_code=HTTP_503_SERVICE_UNAVAILABLE)
    if redirect_url is None:
        log.info("connect.link_refused")
        return pages.message_page("link_unusable", status_code=HTTP_410_GONE)
    log.info("connect.redirected")
    return pages.redirect(redirect_url)


__all__ = ["router"]
```

- [ ] **Step 8: Mount it, and build the connector in the lifespan**

In `src/personal_organizer/api/app.py`, change the routers import to
`from personal_organizer.api.routers import connect, health, internal, webhooks`, and after the
`if settings.gowa.enabled:` block (before `app.state.inbound_channels = …`) add:

```python
    # The connect pages and Composio's callback, under the same rule: off until configured.
    if settings.composio.enabled:
        app.include_router(connect.router)
```

In `src/personal_organizer/api/lifespan.py`, directly after the `app.state.ingress_store = …`
statement, add:

```python
            if settings.composio.enabled:
                # Imported here: the SDK pulls in openai and takes seconds to import, which a
                # deploy without Composio should not pay at every start.
                from personal_organizer.providers.calendar.composio import ComposioConnector

                app.state.connect_linker = ComposioConnector.from_settings(settings.composio)
```

and add `composio_enabled=settings.composio.enabled,` to the `log.info("api.started", …)` call,
after `gowa_enabled=…` (Task 2 allowlisted the key).

- [ ] **Step 9: Run the tests to verify they pass**

Run: `uv run pytest tests/api/test_connect.py tests/db/test_connect.py tests/api -v`
Expected: all PASS, and none of `tests/db/test_connect.py` skipped.

- [ ] **Step 10: Commit**

```bash
uv run ruff format src tests && uv run ruff check src tests && uv run mypy
git add src/personal_organizer/onboarding/connect.py src/personal_organizer/api/routers/connect.py \
  src/personal_organizer/api/app.py src/personal_organizer/api/deps.py \
  src/personal_organizer/api/lifespan.py tests/fixtures/connect.py tests/api/test_connect.py \
  tests/db/test_connect.py
git commit -m "feat: /connect/{token}, spent on POST and never on GET"
```

---

### Task 5: The `onboarding:connected` task

**Files:**
- Create: `src/personal_organizer/onboarding/connected.py`
- Create: `src/personal_organizer/worker/tasks/onboarding.py`
- Modify: `src/personal_organizer/worker/tasks/__init__.py`
- Test: `tests/db/test_onboarding_connected.py`, `tests/worker/test_app.py`

**Interfaces:**
- Consumes (contract): `db.repositories.tenants.get_tenant`, `primary_phone`;
  `db.repositories.messages.latest_inbound_channel(session, tenant_id) -> str | None`;
  `messaging.outbox.send_once(db, channel, *, inbox_id: UUID | None, kind, recipient_key, to,
  text, idempotency_key: str | None = None) -> str`; `messaging.onboarding_text.t(key, language)`;
  `messaging.runtime.get_outbound_channel`; `messaging.inbound.ChannelResolver`;
  `worker.tasks.channel.INBOUND_RETRY`.
- Produces:
  - `onboarding.connected`: `ALL_SET_KIND: Final = "onboarding:all_set"`,
    `def all_set_key(connection_id: UUID) -> str` (returns `f"connected:{connection_id}"`),
    `async def announce_connected(tenant_id: UUID, connection_id: UUID, *, db: Database,
    channels: ChannelResolver) -> str | None`.
  - `worker.tasks.onboarding`: `ONBOARDING_CONNECTED_TASK: Final = "onboarding:connected"`,
    `register(app)`. Task kwargs: `tenant_id: str, connection_id: str`. Queue `webhooks`,
    retry `INBOUND_RETRY`.

- [ ] **Step 1: Write the failing tests**

Create `tests/db/test_onboarding_connected.py`:

```python
"""``onboarding:connected``: "You're all set", once, on the tenant's latest channel.

The message answers no inbound row, so D6's key, not ``(inbox_id, kind)``, makes it
at-most-once. It goes to the channel the tenant last wrote on (D10).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest

from personal_organizer.core.errors import ChannelNotConfiguredError, TransientChannelError
from personal_organizer.db.engine import Database
from personal_organizer.interfaces.channel import OutboundChannel
from personal_organizer.messaging.onboarding_text import t
from personal_organizer.onboarding.connected import (
    ALL_SET_KIND,
    all_set_key,
    announce_connected,
)
from personal_organizer.settings import Settings
from tests.db.test_handle_inbound import FakeOutbound
from tests.fixtures.connect import (
    PHONE,
    TRUNCATE_CONNECT_TABLES,
    make_active,
    seed_inbound,
    seed_tenant,
)

pytestmark = [pytest.mark.db]


@pytest.fixture
async def db(db_settings: Settings, owner_conn: Any) -> AsyncIterator[Database]:
    await owner_conn.execute(TRUNCATE_CONNECT_TABLES)
    database = Database(db_settings)
    try:
        yield database
    finally:
        await database.dispose()
        await owner_conn.execute(TRUNCATE_CONNECT_TABLES)


def resolver(*channels: FakeOutbound) -> Callable[[str], OutboundChannel]:
    """What the worker's registry does: a configured channel, or ChannelNotConfiguredError."""
    by_name = {channel.name: channel for channel in channels}

    def resolve(name: str) -> OutboundChannel:
        try:
            return by_name[name]
        except KeyError:
            raise ChannelNotConfiguredError(name) from None

    return resolve


async def active_tenant(db: Database, **kwargs: Any) -> UUID:
    tenant_id = await seed_tenant(db, **kwargs)
    await make_active(db, tenant_id)
    return tenant_id


async def outbox(owner_conn: Any) -> list[dict[str, Any]]:
    rows = await owner_conn.fetch(
        "SELECT kind, inbox_id, idempotency_key, status, channel FROM channel_outbox"
    )
    return [dict(row) for row in rows]


class TestAnnounceConnected:
    async def test_it_goes_out_on_the_latest_channel_to_the_tenants_phone(
        self, db: Database, owner_conn: Any
    ) -> None:
        tenant_id = await active_tenant(db, language="he")
        now = datetime.now(UTC)
        await seed_inbound(db, tenant_id, channel="whatsapp", sent_at=now - timedelta(minutes=2))
        await seed_inbound(db, tenant_id, channel="gowa", sent_at=now - timedelta(minutes=1))
        meta, gowa = FakeOutbound(name="whatsapp"), FakeOutbound(name="gowa")
        connection_id = uuid4()

        status = await announce_connected(
            tenant_id, connection_id, db=db, channels=resolver(meta, gowa)
        )

        assert status == "accepted"
        assert meta.sent == []
        [message] = gowa.sent
        assert (message.recipient, message.body) == (PHONE, t("all_set", "he"))
        assert await outbox(owner_conn) == [
            {
                "kind": ALL_SET_KIND,
                "inbox_id": None,
                "idempotency_key": all_set_key(connection_id),
                "status": "accepted",
                "channel": "gowa",
            }
        ]

    async def test_a_second_run_sends_nothing(self, db: Database) -> None:
        tenant_id = await active_tenant(db)
        await seed_inbound(db, tenant_id, channel="gowa", sent_at=datetime.now(UTC))
        gowa = FakeOutbound(name="gowa")
        connection_id = uuid4()
        for _ in range(2):
            await announce_connected(tenant_id, connection_id, db=db, channels=resolver(gowa))
        assert len(gowa.sent) == 1

    async def test_a_transient_failure_is_sent_by_the_retry(self, db: Database) -> None:
        tenant_id = await active_tenant(db)
        await seed_inbound(db, tenant_id, channel="gowa", sent_at=datetime.now(UTC))
        gowa = FakeOutbound(TransientChannelError("gateway down"), name="gowa")
        connection_id = uuid4()
        with pytest.raises(TransientChannelError):
            await announce_connected(tenant_id, connection_id, db=db, channels=resolver(gowa))
        status = await announce_connected(
            tenant_id, connection_id, db=db, channels=resolver(gowa)
        )
        assert status == "accepted"
        assert len(gowa.sent) == 1

    async def test_no_inbound_message_means_no_send(self, db: Database) -> None:
        tenant_id = await active_tenant(db)
        gowa = FakeOutbound(name="gowa")
        assert await announce_connected(tenant_id, uuid4(), db=db, channels=resolver(gowa)) is None
        assert gowa.attempts == []

    async def test_no_phone_means_no_send(self, db: Database) -> None:
        tenant_id = await active_tenant(db, phone=None, external_id="uid:BSUID.1")
        await seed_inbound(db, tenant_id, channel="gowa", sent_at=datetime.now(UTC))
        gowa = FakeOutbound(name="gowa")
        assert await announce_connected(tenant_id, uuid4(), db=db, channels=resolver(gowa)) is None
        assert gowa.attempts == []

    async def test_a_tenant_not_active_is_not_told(self, db: Database) -> None:
        tenant_id = await seed_tenant(db)
        await seed_inbound(db, tenant_id, channel="gowa", sent_at=datetime.now(UTC))
        gowa = FakeOutbound(name="gowa")
        assert await announce_connected(tenant_id, uuid4(), db=db, channels=resolver(gowa)) is None
        assert gowa.attempts == []

    async def test_a_channel_the_worker_lacks_is_logged_not_retried(self, db: Database) -> None:
        """GOWA switched off after the tenant last wrote there: finish, don't crash-loop."""
        tenant_id = await active_tenant(db)
        await seed_inbound(db, tenant_id, channel="gowa", sent_at=datetime.now(UTC))
        meta = FakeOutbound(name="whatsapp")
        assert await announce_connected(tenant_id, uuid4(), db=db, channels=resolver(meta)) is None
        assert meta.attempts == []
```

Append to `tests/worker/test_app.py` (and add the two imports at the top:
`from personal_organizer.worker.tasks.channel import HANDLE_INBOUND_TASK, INBOUND_RETRY`,
replacing the existing `HANDLE_INBOUND_TASK` import, and
`from personal_organizer.worker.tasks.onboarding import ONBOARDING_CONNECTED_TASK`):

```python
class TestOnboardingTasks:
    def test_the_connected_task_runs_on_a_queue_the_worker_takes(self, settings: Settings) -> None:
        """Otherwise the callback says "connected" and the message never comes."""
        task = build_procrastinate_app(settings).tasks[ONBOARDING_CONNECTED_TASK]
        assert task.queue == Queue.WEBHOOKS.value
        assert task.queue in settings.worker.queues

    def test_it_retries_what_the_inbound_task_retries(self, settings: Settings) -> None:
        """Only definitely-unsent failures: an ambiguous send is never repeated (ADR 0003)."""
        task = build_procrastinate_app(settings).tasks[ONBOARDING_CONNECTED_TASK]
        assert task.retry_strategy is INBOUND_RETRY
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/db/test_onboarding_connected.py tests/worker/test_app.py -v`
Expected: collection errors, `ModuleNotFoundError: No module named
'personal_organizer.onboarding.connected'` and `'personal_organizer.worker.tasks.onboarding'`.

- [ ] **Step 3: Write the logic**

Create `src/personal_organizer/onboarding/connected.py`:

```python
"""Telling a tenant they are connected: the logic of the ``onboarding:connected`` task.

This send answers no inbound message, so it has no inbox row to key on and no channel to
follow. It is keyed instead (D6, ``connected:<connection_id>``), which makes a refreshed
callback, and so two jobs, one message. It goes to the tenant's phone on the channel they last
wrote on: the first instance of ADR 0004's "proactive sends need a channel choice" seam.
"""

from __future__ import annotations

from typing import Final
from uuid import UUID

import structlog

from personal_organizer.core.errors import ChannelNotConfiguredError
from personal_organizer.core.types import TenantId
from personal_organizer.db.engine import Database
from personal_organizer.db.repositories.messages import latest_inbound_channel
from personal_organizer.db.repositories.tenants import get_tenant, primary_phone
from personal_organizer.messaging.inbound import ChannelResolver
from personal_organizer.messaging.onboarding_text import t
from personal_organizer.messaging.outbox import send_once

log = structlog.get_logger(__name__)

ALL_SET_KIND: Final = "onboarding:all_set"


def all_set_key(connection_id: UUID) -> str:
    """D6's key: one "all set" per connection, however many callbacks bound it."""
    return f"connected:{connection_id}"


async def announce_connected(
    tenant_id: UUID, connection_id: UUID, *, db: Database, channels: ChannelResolver
) -> str | None:
    """Send "You're all set" at most once.

    Returns the outbox status, or ``None`` when there is nowhere to send it: no inbound message,
    no phone, a channel this worker does not run, or a tenant no longer active. Each of those is
    logged and finishes the job, because a retry would find the same thing. Re-raises
    ``TransientChannelError`` from :func:`send_once`, which the task's retry strategy retries.
    """
    async with db.tenant_session(TenantId(tenant_id)) as session:
        tenant = await get_tenant(session, tenant_id)
        channel_name = await latest_inbound_channel(session, tenant_id)
        phone = await primary_phone(session, tenant_id)
    if tenant is None or tenant.status != "active":
        log.warning("onboarding.all_set_skipped", tenant_id=str(tenant_id), reason="not_active")
        return None
    if channel_name is None or phone is None:
        reason = "no_channel" if channel_name is None else "no_phone"
        log.warning("onboarding.all_set_skipped", tenant_id=str(tenant_id), reason=reason)
        return None
    try:
        channel = channels(channel_name)
    except ChannelNotConfiguredError:
        log.error(
            "onboarding.all_set_skipped",
            tenant_id=str(tenant_id),
            channel=channel_name,
            reason="channel_not_configured",
        )
        return None
    return await send_once(
        db,
        channel,
        inbox_id=None,
        kind=ALL_SET_KIND,
        recipient_key=f"tel:{phone}",
        to=phone,
        text=t("all_set", tenant.language),
        idempotency_key=all_set_key(connection_id),
    )


__all__ = ["ALL_SET_KIND", "all_set_key", "announce_connected"]
```

- [ ] **Step 4: Register the task**

Create `src/personal_organizer/worker/tasks/onboarding.py`:

```python
"""Onboarding tasks.

``onboarding:connected`` is deferred by the api's connect callback once the connection is bound
and the tenant active. Its kwargs are ids only (docs/adr/0001). It runs on ``webhooks``, the
queue that already sends replies, which every worker takes by default.
"""

from __future__ import annotations

from typing import Final
from uuid import UUID

import procrastinate

from personal_organizer.db.engine import get_database
from personal_organizer.messaging.runtime import get_outbound_channel
from personal_organizer.onboarding.connected import announce_connected
from personal_organizer.worker.queues import Queue
from personal_organizer.worker.tasks.channel import INBOUND_RETRY

ONBOARDING_CONNECTED_TASK: Final = "onboarding:connected"


def register(app: procrastinate.App) -> None:
    @app.task(queue=Queue.WEBHOOKS.value, name=ONBOARDING_CONNECTED_TASK, retry=INBOUND_RETRY)
    async def onboarding_connected(tenant_id: str, connection_id: str) -> None:
        await announce_connected(
            UUID(tenant_id),
            UUID(connection_id),
            db=get_database(),
            channels=get_outbound_channel,
        )


__all__ = ["ONBOARDING_CONNECTED_TASK", "register"]
```

In `src/personal_organizer/worker/tasks/__init__.py`, change the import to
`from personal_organizer.worker.tasks import channel, onboarding, system` and the list to
`[system.register, channel.register, onboarding.register]`.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/db/test_onboarding_connected.py tests/worker -v`
Expected: all PASS, none skipped. If `test_it_retries_what_the_inbound_task_retries` fails on
identity because Procrastinate copies the strategy, compare `retry_exceptions`,
`max_attempts` and `exponential_wait` instead.

- [ ] **Step 6: Commit**

```bash
uv run ruff format src tests && uv run ruff check src tests && uv run mypy
git add src/personal_organizer/onboarding/connected.py src/personal_organizer/worker/tasks \
  tests/db/test_onboarding_connected.py tests/worker/test_app.py
git commit -m "feat: onboarding:connected sends You're all set once"
```

---

### Task 6: `GET /connect/callback`

**Files:**
- Modify: `src/personal_organizer/onboarding/connect.py` (full replacement below)
- Modify: `src/personal_organizer/api/routers/connect.py` (full replacement below)
- Test: `tests/api/test_connect.py`, `tests/db/test_connect.py`

**Interfaces:**
- Consumes (contract, PR 2): `db.repositories.connections.bind_connection(session, tenant_id, *,
  connected_account_id: str, auth_config_id: str) -> CalendarConnection`,
  `db.repositories.tenants.activate`, `db.models.tenant.CalendarConnection`. (Task 1):
  `ACCOUNT_ACTIVE`. (Task 5): `ONBOARDING_CONNECTED_TASK`, `announce_connected`.
- Produces: `onboarding.connect`: `STATE_MAX_AGE_S: Final = 3600`;
  `@dataclass(frozen=True, slots=True) class Connected: tenant_id: UUID; connection_id: UUID;
  language: str`; `class Refused: reason: str`; `async def complete_connect(*, state: str |
  None, connected_account_id: str | None, db: Database, linker: ConnectLinker, config:
  ConnectConfig) -> Connected | Refused`. Route `GET /connect/callback`.

- [ ] **Step 1: Write the failing API tests (refusals before the database)**

In `tests/api/test_connect.py`, replace the import block with:

```python
from __future__ import annotations

import time
from collections.abc import AsyncIterator, Callable
from urllib.parse import urlencode
from uuid import uuid4

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from itsdangerous import TimestampSigner

from personal_organizer.api.app import create_app
from personal_organizer.core.errors import CalendarProviderUnavailableError
from personal_organizer.settings import Settings
from tests.api.conftest import FakeDatabase, FakeProcrastinate
from tests.fixtures.connect import (
    ACCOUNT_ID,
    CONNECT_ENV,
    OTHER_SECRET,
    FakeConnectLinker,
    account,
    assert_hardened,
    assert_page,
    connect_app,
    link_token,
    state_token,
)
```

and append:

```python
def callback(state: str | None, account_id: str | None = ACCOUNT_ID) -> str:
    params = {
        name: value
        for name, value in {"state": state, "connected_account_id": account_id}.items()
        if value is not None
    }
    return f"/connect/callback?{urlencode(params)}"


REFUSED_STATES: dict[str, Callable[[], str | None]] = {
    "missing": lambda: None,
    "garbage": lambda: "not-a-state",
    "forged": lambda: state_token(secret=OTHER_SECRET),
    "a_link_not_a_state": lambda: link_token(),
    "expired": lambda: stale(state_token),
}


class TestCallbackRefusals:
    """D5, everything decided before the tenant's rows are touched."""

    @pytest.mark.parametrize("kind", sorted(REFUSED_STATES))
    async def test_a_bad_state_is_refused_before_composio_is_asked(
        self, http: AsyncClient, linker: FakeConnectLinker, queue: FakeProcrastinate, kind: str
    ) -> None:
        response = await http.get(callback(REFUSED_STATES[kind]()))
        assert response.status_code == 400
        assert_page(response, "failed", "he", "en")
        assert_hardened(response)
        assert linker.lookups == []
        assert queue.calls == []

    @pytest.mark.parametrize(
        "account_id", [None, "", "../../admin", "ca_" + "x" * 65, "ac_test", "ca_a/b"]
    )
    async def test_a_malformed_account_id_never_reaches_composio(
        self, http: AsyncClient, linker: FakeConnectLinker, account_id: str | None
    ) -> None:
        response = await http.get(callback(state_token(), account_id))
        assert response.status_code == 400
        assert linker.lookups == []

    async def test_another_users_account_is_refused(
        self, http: AsyncClient, linker: FakeConnectLinker, queue: FakeProcrastinate
    ) -> None:
        tenant_id = uuid4()
        linker.accounts[ACCOUNT_ID] = account(tenant_id, user_id=str(uuid4()))
        response = await http.get(callback(state_token(tenant_id)))
        assert response.status_code == 400
        assert_page(response, "failed", "he", "en")
        assert linker.lookups == [ACCOUNT_ID]
        assert queue.calls == []

    async def test_an_account_under_another_auth_config_is_refused(
        self, http: AsyncClient, linker: FakeConnectLinker, queue: FakeProcrastinate
    ) -> None:
        tenant_id = uuid4()
        linker.accounts[ACCOUNT_ID] = account(tenant_id, auth_config_id="ac_someone_else")
        response = await http.get(callback(state_token(tenant_id)))
        assert response.status_code == 400
        assert queue.calls == []

    @pytest.mark.parametrize(
        "status", ["INITIALIZING", "INITIATED", "FAILED", "EXPIRED", "INACTIVE", "REVOKED"]
    )
    async def test_an_account_that_is_not_active_is_not_bound(
        self,
        http: AsyncClient,
        linker: FakeConnectLinker,
        queue: FakeProcrastinate,
        status: str,
    ) -> None:
        tenant_id = uuid4()
        linker.accounts[ACCOUNT_ID] = account(tenant_id, status=status)
        response = await http.get(callback(state_token(tenant_id)))
        assert response.status_code == 400
        assert_page(response, "not_ready", "he", "en")
        assert queue.calls == []

    async def test_an_account_composio_does_not_know_is_refused(self, http: AsyncClient) -> None:
        response = await http.get(callback(state_token()))
        assert response.status_code == 400
        assert_page(response, "failed", "he", "en")

    async def test_composio_unreachable_asks_for_a_reload(
        self, http: AsyncClient, linker: FakeConnectLinker
    ) -> None:
        linker.failure = CalendarProviderUnavailableError("down")
        response = await http.get(callback(state_token()))
        assert response.status_code == 503
        assert_page(response, "unavailable_retry", "he", "en")
        assert_hardened(response)

    async def test_the_callback_is_not_mistaken_for_a_link(self, http: AsyncClient) -> None:
        """``/{token}`` would take "callback" as a token if it were declared first."""
        response = await http.get("/connect/callback")
        assert response.status_code == 400
        assert_page(response, "failed", "he", "en")
```

- [ ] **Step 2: Write the failing database tests**

In `tests/db/test_connect.py`, add these imports:

```python
from uuid import UUID

from sqlalchemy import select

from personal_organizer.core.types import TenantId
from personal_organizer.db.models.tenant import CalendarConnection, Tenant
from personal_organizer.db.repositories.connections import bind_connection
from personal_organizer.db.repositories.tenants import get_tenant
from personal_organizer.messaging.onboarding_text import t
from personal_organizer.onboarding.connected import announce_connected
from personal_organizer.worker.tasks.onboarding import ONBOARDING_CONNECTED_TASK
from tests.api.conftest import DeferredCall
from tests.db.test_handle_inbound import FakeOutbound
```

and extend the `tests.fixtures.connect` import with `ACCOUNT_ID`, `account`, `seed_inbound` and
`suspend`. Then append:

```python
class FailingProcrastinate(FakeProcrastinate):
    def configure_task(self, name: str, **options: Any) -> Any:
        class Deferrer:
            async def defer_async(self, **kwargs: Any) -> int:
                msg = "queue down"
                raise ConnectionError(msg)

        return Deferrer()


async def callback_url(
    http: AsyncClient,
    db: Database,
    linker: FakeConnectLinker,
    tenant_id: UUID,
    *,
    param: str = "connected_account_id",
) -> str:
    """Press the button, then build the URL Composio sends the browser back to."""
    response = await http.post(f"/connect/{await seed_link(db, tenant_id)}")
    assert response.status_code == 303
    sent = urlsplit(linker.links[-1]["callback_url"])
    return f"{sent.path}?{sent.query}&{param}={ACCOUNT_ID}"


async def tenant_row(db: Database, tenant_id: UUID) -> Tenant:
    async with db.tenant_session(TenantId(tenant_id)) as session:
        tenant = await get_tenant(session, tenant_id)
    assert tenant is not None
    return tenant


async def connections(db: Database, tenant_id: UUID) -> list[CalendarConnection]:
    async with db.tenant_session(TenantId(tenant_id)) as session:
        rows = await session.scalars(
            select(CalendarConnection).order_by(CalendarConnection.connected_at)
        )
        return list(rows.all())


class TestCallback:
    async def test_it_binds_activates_and_defers_the_message(
        self, http: AsyncClient, db: Database, linker: FakeConnectLinker, queue: FakeProcrastinate
    ) -> None:
        tenant_id = await seed_tenant(db, language="he")
        url = await callback_url(http, db, linker, tenant_id)
        linker.accounts[ACCOUNT_ID] = account(tenant_id)

        response = await http.get(url)

        assert response.status_code == 200
        assert '<html lang="he" dir="rtl">' in response.text
        assert_page(response, "connected", "he")
        assert_hardened(response)
        tenant = await tenant_row(db, tenant_id)
        assert (tenant.status, tenant.onboarding_step) == ("active", None)
        [connection] = await connections(db, tenant_id)
        assert (
            connection.connected_account_id,
            connection.auth_config_id,
            connection.status,
            connection.composio_user_id,
        ) == (ACCOUNT_ID, AUTH_CONFIG_ID, "active", str(tenant_id))
        # Ids only (docs/adr/0001).
        assert queue.calls == [
            DeferredCall(
                ONBOARDING_CONNECTED_TASK,
                {},
                {"tenant_id": str(tenant_id), "connection_id": str(connection.id)},
            )
        ]

    async def test_composios_camel_case_parameter_works_too(
        self, http: AsyncClient, db: Database, linker: FakeConnectLinker
    ) -> None:
        tenant_id = await seed_tenant(db)
        url = await callback_url(http, db, linker, tenant_id, param="connectedAccountId")
        linker.accounts[ACCOUNT_ID] = account(tenant_id)
        assert (await http.get(url)).status_code == 200

    async def test_a_refreshed_callback_sends_one_message(
        self, http: AsyncClient, db: Database, linker: FakeConnectLinker, queue: FakeProcrastinate
    ) -> None:
        tenant_id = await seed_tenant(db, language="he")
        await seed_inbound(db, tenant_id, channel="gowa", sent_at=datetime.now(UTC))
        url = await callback_url(http, db, linker, tenant_id)
        linker.accounts[ACCOUNT_ID] = account(tenant_id)

        assert (await http.get(url)).status_code == 200
        assert (await http.get(url)).status_code == 200

        assert len(await connections(db, tenant_id)) == 1
        assert len(queue.calls) == 2
        assert queue.calls[0] == queue.calls[1]
        gowa = FakeOutbound(name="gowa")
        for call in queue.calls:  # what the worker does with each deferred job
            await announce_connected(
                UUID(call.kwargs["tenant_id"]),
                UUID(call.kwargs["connection_id"]),
                db=db,
                channels={"gowa": gowa}.__getitem__,
            )
        assert [message.body for message in gowa.sent] == [t("all_set", "he")]

    async def test_an_account_not_yet_active_can_be_reloaded(
        self, http: AsyncClient, db: Database, linker: FakeConnectLinker, queue: FakeProcrastinate
    ) -> None:
        tenant_id = await seed_tenant(db)
        url = await callback_url(http, db, linker, tenant_id)
        linker.accounts[ACCOUNT_ID] = account(tenant_id, status="INITIATED")

        first = await http.get(url)
        assert first.status_code == 400
        assert_page(first, "not_ready", "he", "en")
        assert (await tenant_row(db, tenant_id)).status == "onboarding"
        assert queue.calls == []

        linker.accounts[ACCOUNT_ID] = account(tenant_id)
        second = await http.get(url)
        assert second.status_code == 200
        assert_page(second, "connected", "he")

    async def test_a_suspended_tenant_is_not_bound(
        self, http: AsyncClient, db: Database, linker: FakeConnectLinker, queue: FakeProcrastinate
    ) -> None:
        tenant_id = await seed_tenant(db)
        url = await callback_url(http, db, linker, tenant_id)
        await suspend(db, tenant_id)
        linker.accounts[ACCOUNT_ID] = account(tenant_id)

        assert (await http.get(url)).status_code == 400
        assert await connections(db, tenant_id) == []
        assert (await tenant_row(db, tenant_id)).status == "suspended"
        assert queue.calls == []

    async def test_reconnecting_revokes_the_old_connection(
        self, http: AsyncClient, db: Database, linker: FakeConnectLinker
    ) -> None:
        tenant_id = await seed_tenant(db)
        async with db.tenant_session(TenantId(tenant_id)) as session:
            await bind_connection(
                session, tenant_id, connected_account_id="ca_old", auth_config_id=AUTH_CONFIG_ID
            )
        url = await callback_url(http, db, linker, tenant_id)
        linker.accounts[ACCOUNT_ID] = account(tenant_id)

        assert (await http.get(url)).status_code == 200

        statuses = {c.connected_account_id: c.status for c in await connections(db, tenant_id)}
        assert statuses == {"ca_old": "revoked", ACCOUNT_ID: "active"}

    async def test_a_failed_defer_still_shows_connected(
        self, connect_settings: Settings, db: Database, linker: FakeConnectLinker
    ) -> None:
        """The tenant is bound and active. The page says so, and a reload re-defers."""
        application = connect_app(
            connect_settings, db=db, linker=linker, queue=FailingProcrastinate()
        )
        transport = ASGITransport(app=application, raise_app_exceptions=False)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            tenant_id = await seed_tenant(db)
            url = await callback_url(client, db, linker, tenant_id)
            linker.accounts[ACCOUNT_ID] = account(tenant_id)
            response = await client.get(url)
        assert response.status_code == 200
        assert (await tenant_row(db, tenant_id)).status == "active"
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `uv run pytest tests/api/test_connect.py tests/db/test_connect.py -v -k "Callback or callback"`
Expected: FAIL. `/connect/callback` is taken as a token, so most answer 410 where 400, 200 or
503 is asserted.

- [ ] **Step 4: Add `complete_connect` to the service**

Replace `src/personal_organizer/onboarding/connect.py` with the Task 4 file plus the callback.
The full file:

```python
"""The connect flow behind ``/connect``: open a link, hand the browser to Composio, finish on
the callback.

D4: a link is spent on POST, never on GET, because WhatsApp fetches links to build previews.
:func:`open_link` reads (the tenant's language and number, for the page) and writes nothing.

D5: the callback believes Composio's API, not its query string. :func:`complete_connect`
verifies our signed state, fetches the account and checks it before it binds anything.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Final
from urllib.parse import urlencode
from uuid import UUID

from personal_organizer.core.errors import ConfigError
from personal_organizer.core.types import TenantId
from personal_organizer.db.engine import Database
from personal_organizer.db.repositories.connections import bind_connection
from personal_organizer.db.repositories.links import consume_link
from personal_organizer.db.repositories.tenants import activate, get_tenant, primary_phone
from personal_organizer.interfaces.calendar import ACCOUNT_ACTIVE, ConnectLinker
from personal_organizer.onboarding.tokens import (
    LINK_SALT,
    STATE_SALT,
    TokenPayload,
    sign,
    verify,
)
from personal_organizer.settings import Settings

#: Only a tenant still onboarding may use a link. An active one has connected already, through
#: another link; reconnecting arrives with the calendar-read iteration.
_LINKABLE: Final = "onboarding"
#: A callback may land again (a reload) after the first one activated the tenant.
_BINDABLE: Final = frozenset({"onboarding", "active"})
#: How many trailing digits of the number the page shows: enough for someone handed another
#: person's link to see that it is not theirs before connecting their Google account to it.
PHONE_SUFFIX_DIGITS: Final = 4
#: How long a signed state stays good: the time a user may spend on Composio's and Google's
#: screens, unverified-app warning included. Generous, because the state is not the authority
#: (Composio's API is, D5), and a late callback can only bind what it verifies.
STATE_MAX_AGE_S: Final = 3600
#: Composio's connected-account ids. Checked before an id from the query reaches an API path.
_ACCOUNT_ID = re.compile(r"\Aca_[A-Za-z0-9_-]{1,64}\Z")


@dataclass(frozen=True, slots=True)
class ConnectConfig:
    """The four settings the flow needs, narrowed from optional. Composio being enabled makes
    them required (``Settings._composio_is_complete``), and the router mounts only then."""

    secret: str
    base_url: str
    auth_config_id: str
    link_ttl_s: int

    @classmethod
    def of(cls, settings: Settings) -> ConnectConfig:
        secret = settings.onboarding.link_secret
        base_url = settings.app.public_base_url
        auth_config_id = settings.composio.calendar_auth_config_id
        if secret is None or base_url is None or auth_config_id is None:
            msg = "The connect pages need the settings COMPOSIO__ENABLED requires"
            raise ConfigError(msg)
        return cls(
            secret=secret.get_secret_value(),
            base_url=base_url,
            auth_config_id=auth_config_id,
            link_ttl_s=settings.onboarding.link_ttl_s,
        )

    @property
    def callback_base(self) -> str:
        return f"{self.base_url}/connect/callback"


@dataclass(frozen=True, slots=True)
class LinkPage:
    language: str
    phone_suffix: str | None


@dataclass(frozen=True, slots=True)
class Connected:
    tenant_id: UUID
    connection_id: UUID
    language: str


@dataclass(frozen=True, slots=True)
class Refused:
    """Why a callback bound nothing: a fixed, log-safe word, never shown to the user."""

    reason: str


def _link_payload(token: str, config: ConnectConfig) -> TokenPayload | None:
    return verify(token, secret=config.secret, salt=LINK_SALT, max_age_s=config.link_ttl_s)


async def open_link(token: str, *, db: Database, config: ConnectConfig) -> LinkPage | None:
    """What the button page shows, or ``None`` for a link that cannot be used. Writes nothing.

    It checks the signature and the tenant, not whether the link row is spent or expired:
    there is no read for that, and the POST is the authority either way.
    """
    payload = _link_payload(token, config)
    if payload is None:
        return None
    async with db.tenant_session(TenantId(payload.tenant_id)) as session:
        tenant = await get_tenant(session, payload.tenant_id)
        if tenant is None or tenant.status != _LINKABLE:
            return None
        phone = await primary_phone(session, payload.tenant_id)
    return LinkPage(
        language=tenant.language,
        phone_suffix=phone[-PHONE_SUFFIX_DIGITS:] if phone else None,
    )


async def start_connect(
    token: str, *, db: Database, linker: ConnectLinker, config: ConnectConfig, now: datetime
) -> str | None:
    """Spend the link and return Composio's URL, or ``None`` if the link cannot be used.

    The link is spent *before* Composio is called, in its own transaction, so two taps racing
    each other get one redirect between them: ``consume_link`` is one atomic UPDATE. The cost:
    if Composio then fails, this link is gone, and the user asks the bot for a new one, which
    the connect step issues because none is left unused.

    Raises :class:`~personal_organizer.core.errors.CalendarProviderError` from ``linker``.
    """
    payload = _link_payload(token, config)
    if payload is None:
        return None
    tenant_id = payload.tenant_id
    async with db.tenant_session(TenantId(tenant_id)) as session:
        tenant = await get_tenant(session, tenant_id)
        if tenant is None or tenant.status != _LINKABLE:
            return None
        if not await consume_link(session, tenant_id, nonce=payload.nonce, now=now):
            return None
    state = sign(
        TokenPayload(tenant_id=tenant_id, nonce=payload.nonce),
        secret=config.secret,
        salt=STATE_SALT,
    )
    return await linker.link(
        user_id=str(tenant_id),
        auth_config_id=config.auth_config_id,
        callback_url=f"{config.callback_base}?{urlencode({'state': state})}",
    )


async def complete_connect(
    *,
    state: str | None,
    connected_account_id: str | None,
    db: Database,
    linker: ConnectLinker,
    config: ConnectConfig,
) -> Connected | Refused:
    """D5: verify our state, then believe only what Composio's API says about the account.

    Everything that can refuse does so before a tenant row is touched, except the tenant's own
    status. Binding and activating are one tenant transaction. Idempotent: the same callback
    twice binds once (``bind_connection`` returns the existing row) and reports the same
    connection id, so the jobs it defers send one message between them.

    Raises :class:`~personal_organizer.core.errors.CalendarProviderError` from ``linker``.
    """
    payload = (
        verify(state, secret=config.secret, salt=STATE_SALT, max_age_s=STATE_MAX_AGE_S)
        if state
        else None
    )
    if payload is None:
        return Refused("bad_state")
    if connected_account_id is None or not _ACCOUNT_ID.match(connected_account_id):
        return Refused("bad_account_id")
    account = await linker.get_account(connected_account_id)
    tenant_id = payload.tenant_id
    if account.user_id != str(tenant_id):
        return Refused("user_mismatch")
    if account.auth_config_id != config.auth_config_id:
        return Refused("auth_config_mismatch")
    if account.status != ACCOUNT_ACTIVE:
        return Refused("not_active")
    async with db.tenant_session(TenantId(tenant_id)) as session:
        tenant = await get_tenant(session, tenant_id)
        if tenant is None or tenant.status not in _BINDABLE:
            return Refused("tenant_unavailable")
        connection = await bind_connection(
            session,
            tenant_id,
            connected_account_id=account.id,
            auth_config_id=account.auth_config_id,
        )
        await activate(session, tenant_id)
    return Connected(tenant_id=tenant_id, connection_id=connection.id, language=tenant.language)


__all__ = [
    "PHONE_SUFFIX_DIGITS",
    "STATE_MAX_AGE_S",
    "ConnectConfig",
    "Connected",
    "LinkPage",
    "Refused",
    "complete_connect",
    "open_link",
    "start_connect",
]
```

- [ ] **Step 5: Add the callback route above `/{token}`**

Replace `src/personal_organizer/api/routers/connect.py` with:

```python
"""``/connect``: the page a WhatsApp link opens, the hop to Composio, and the way back.

Mounted only when ``COMPOSIO__ENABLED`` (see :func:`create_app`).

- ``GET /connect/{token}`` renders one button and changes nothing. WhatsApp fetches links to
  build previews, so a GET that spent the link would spend it before the user saw it (D4).
- ``POST /connect/{token}`` spends the link and answers 303 to Composio.
- ``GET /connect/callback`` is where Composio sends the browser back. It believes Composio's
  API about the account, never the query string (D5).

Every response is built by :mod:`personal_organizer.api.pages`, which sets D4's headers. A
refused link or callback gets a page that says what to do next and never why. The reason goes
to the log, as a fixed word.

``/callback`` is declared before ``/{token}``. Routes match in order, and ``{token}`` would
otherwise take "callback" as a token.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Final

import procrastinate
import sentry_sdk
import structlog
from fastapi import APIRouter, Request, Response
from starlette.status import (
    HTTP_200_OK,
    HTTP_400_BAD_REQUEST,
    HTTP_410_GONE,
    HTTP_503_SERVICE_UNAVAILABLE,
)

from personal_organizer.api import pages
from personal_organizer.api.deps import ConnectLinkerDep, DbDep, ProcrastinateDep, SettingsDep
from personal_organizer.core.errors import (
    CalendarProviderRejectedError,
    CalendarProviderUnavailableError,
)
from personal_organizer.onboarding.connect import (
    ConnectConfig,
    Connected,
    Refused,
    complete_connect,
    open_link,
    start_connect,
)
from personal_organizer.worker.tasks.onboarding import ONBOARDING_CONNECTED_TASK

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/connect", tags=["connect"])

#: Composio appends the account id to our callback URL. The snake_case name is the one its API
#: uses everywhere else. The camelCase one is accepted too, until the day-1 staging run shows
#: which arrives.
_ACCOUNT_ID_PARAMS: Final = ("connected_account_id", "connectedAccountId")
#: The page for each refusal that is not "ask the bot for a new link".
_REFUSAL_PAGES: Final = {"not_active": "not_ready"}


def _account_id(request: Request) -> str | None:
    for name in _ACCOUNT_ID_PARAMS:
        if value := request.query_params.get(name):
            return value
    return None


async def _defer_all_set(queue: procrastinate.App, outcome: Connected) -> None:
    """Defer the "all set" message, ids only (docs/adr/0001).

    Every successful callback defers, a repeat included: D6's outbox key makes the extra job a
    no-op, and a callback whose defer failed is mended by a reload. A failed defer still leaves
    the tenant connected, so the page says so and the failure goes to Sentry.
    """
    try:
        await queue.configure_task(ONBOARDING_CONNECTED_TASK).defer_async(
            tenant_id=str(outcome.tenant_id), connection_id=str(outcome.connection_id)
        )
    except Exception as exc:
        sentry_sdk.capture_exception(exc)
        log.error("connect.defer_failed", error_type=type(exc).__name__)


@router.get("/callback", summary="Composio sends the browser back here.")
async def connect_callback(
    request: Request,
    db: DbDep,
    linker: ConnectLinkerDep,
    queue: ProcrastinateDep,
    settings: SettingsDep,
) -> Response:
    try:
        outcome = await complete_connect(
            state=request.query_params.get("state"),
            connected_account_id=_account_id(request),
            db=db,
            linker=linker,
            config=ConnectConfig.of(settings),
        )
    except CalendarProviderUnavailableError as exc:
        log.warning("connect.callback_provider_unavailable", error_type=type(exc).__name__)
        return pages.message_page("unavailable_retry", status_code=HTTP_503_SERVICE_UNAVAILABLE)
    except CalendarProviderRejectedError as exc:
        # Usually a 404 for an id that is not one of ours: someone guessing, not a fault.
        log.warning(
            "connect.callback_provider_rejected",
            error_type=type(exc).__name__,
            status_code=exc.status_code,
        )
        return pages.message_page("failed", status_code=HTTP_400_BAD_REQUEST)
    if isinstance(outcome, Refused):
        log.warning("connect.callback_refused", reason=outcome.reason)
        page = _REFUSAL_PAGES.get(outcome.reason, "failed")
        return pages.message_page(page, status_code=HTTP_400_BAD_REQUEST)
    await _defer_all_set(queue, outcome)
    log.info(
        "connect.connected",
        tenant_id=str(outcome.tenant_id),
        connection_id=str(outcome.connection_id),
    )
    return pages.message_page("connected", status_code=HTTP_200_OK, language=outcome.language)


@router.get("/{token}", summary="The connect page. Reads only: link previews fetch it.")
async def connect_page(token: str, db: DbDep, settings: SettingsDep) -> Response:
    page = await open_link(token, db=db, config=ConnectConfig.of(settings))
    if page is None:
        log.info("connect.page_refused")
        return pages.message_page("link_unusable", status_code=HTTP_410_GONE)
    return pages.connect_page(token=token, language=page.language, phone_suffix=page.phone_suffix)


@router.post("/{token}", summary="Spend the link and send the browser to Composio.")
async def start(
    token: str, db: DbDep, linker: ConnectLinkerDep, settings: SettingsDep
) -> Response:
    try:
        redirect_url = await start_connect(
            token,
            db=db,
            linker=linker,
            config=ConnectConfig.of(settings),
            now=datetime.now(UTC),
        )
    except CalendarProviderUnavailableError as exc:
        log.warning("connect.link_provider_unavailable", error_type=type(exc).__name__)
        return pages.message_page("unavailable_new_link", status_code=HTTP_503_SERVICE_UNAVAILABLE)
    except CalendarProviderRejectedError as exc:
        # Our configuration (API key, auth config), not the user's doing: someone must look.
        sentry_sdk.capture_exception(exc)
        log.error(
            "connect.link_provider_rejected",
            error_type=type(exc).__name__,
            status_code=exc.status_code,
        )
        return pages.message_page("unavailable_new_link", status_code=HTTP_503_SERVICE_UNAVAILABLE)
    if redirect_url is None:
        log.info("connect.link_refused")
        return pages.message_page("link_unusable", status_code=HTTP_410_GONE)
    log.info("connect.redirected")
    return pages.redirect(redirect_url)


__all__ = ["router"]
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `uv run pytest tests/api/test_connect.py tests/db/test_connect.py tests/db/test_onboarding_connected.py -v`
Expected: all PASS, none skipped.

- [ ] **Step 7: Commit**

```bash
uv run ruff format src tests && uv run ruff check src tests && uv run mypy
git add src/personal_organizer/onboarding/connect.py src/personal_organizer/api/routers/connect.py \
  tests/api/test_connect.py tests/db/test_connect.py
git commit -m "feat: /connect/callback binds a verified account and defers You're all set"
```

---

### Final check (no new code)

- [ ] **Step 1: The whole gate, as CI runs it**

```bash
docker compose up -d && uv run po-db bootstrap && uv run alembic upgrade head
uv run ruff check . && uv run ruff format --check . && uv run mypy
uv run pytest
uv run pytest -m db -rs tests/db/test_connect.py tests/db/test_onboarding_connected.py
```

Expected: everything green. The last command's `-rs` summary must show **no skips**. A skip
means Postgres was not reached, and the D5/D6 guarantees were not tested.

- [ ] **Step 2: Spec coverage spot-check against D4, D5, D6 and D10**

- D4: the GET never spends (`test_previews_do_not_spend_the_link`); the headers are on every
  page and on the redirect (`assert_hardened` throughout, `TestHeaders`); Sentry scrubbing
  (`test_connect_tokens_are_removed_from_request_urls`).
- D5: forged, expired and wrong-salt state; wrong `user_id`; wrong auth config; every
  non-`ACTIVE` status (`TestCallbackRefusals`).
- D6: a refreshed callback sends one message (`test_a_refreshed_callback_sends_one_message`).
- D10: activation and `onboarding_step = NULL` (`test_it_binds_activates_and_defers_the_message`);
  latest channel, primary phone (`TestAnnounceConnected`).
