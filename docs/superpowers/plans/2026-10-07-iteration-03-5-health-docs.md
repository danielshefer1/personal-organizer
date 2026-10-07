# Iteration 03 PR 5 — GOWA health check and the Iteration 03 docs — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make a logged-out or unreachable GOWA gateway raise a Sentry issue within five minutes, and document everything Iteration 03 needs outside the code: the Google OAuth app, Composio, the variables, the migration, the staging end to end, the day-1 and day-8 checks, and ADR 0005.

**Architecture:** `po-gowa status`'s request-and-parse logic moves into `providers/channel/gowa/status.py`, which both the CLI (sync) and a new periodic worker task `system:gowa_health` (async) call. The task logs `gowa.unhealthy` at error level and sends one explicit Sentry event with a fixed fingerprint, because `init_sentry` switches the logging integration's events off. `build_procrastinate_app` registers it only when `settings.gowa.enabled`. The rest is docs.

**Tech Stack:** Python 3.14, httpx (`MockTransport` in tests), Procrastinate 3.10 (`periodic`, `queueing_lock`, `testing.InMemoryConnector`), sentry-sdk 2.70, structlog, pytest + pytest-asyncio (`asyncio_mode = "auto"`).

**Spec:** `docs/plan-iteration-03.md` (PR stack item 4, "Prerequisites", "Ops checklist", "Risks and open questions"). **Contract:** `docs/superpowers/plans/2026-10-07-iteration-03-contract.md` (section "PR 5"; PRs 2–4 are on this branch's base when you start).

## Contract deviations

1. **Unhealthy covers four reasons, not two.** The contract says `gowa.unhealthy` fires when the gateway is unreachable or `logged_in` is false, and `gowa.healthy` otherwise. This plan also reports `not_connected` (logged in, socket down) and `bad_response` (a non-2xx answer, such as 401 for a wrong basic-auth pair, or a body that is not two booleans). The reason: in both cases every send fails (the gateway answers 401 "not connected", or our credentials are refused), so "healthy" would be false. The reason enum is `unreachable | not_connected | not_logged_in | bad_response`. When both flags are false, `not_logged_in` wins, because that is the case that needs a person with the phone.
2. **"Reaching Sentry" is an explicit capture, not a side effect of the log level.** `observability/sentry.py` initialises `LoggingIntegration(level=None, event_level=None)`, so **no log line reaches Sentry at any level**. The task therefore calls `sentry_sdk.capture_message("gowa.unhealthy", level="error")` inside a scope with `fingerprint = ["gowa.unhealthy"]` and a `reason` tag. That one fingerprint gives one Sentry issue, which is the alert. The global Sentry setup stays as it is: turning logging events on would also send every other error line, and send `request.failed` twice.
3. **Not a deviation, recorded so nobody "fixes" it:** `REGISTRARS` keeps its `Callable[[procrastinate.App], None]` signature (PR 4 appends `onboarding.register` to it). The health task is settings-dependent, so `build_procrastinate_app` registers it after the loop with `if settings.gowa.enabled: gowa_health.register(app, settings.gowa)`.

## Spec points this plan had to resolve

- **The day-8 check has no calendar read to use.** Calendar reads come in a later iteration. The runbook checks the connection through Composio directly: the dashboard shows the account as ACTIVE and runs a read-only Calendar tool, and an SDK snippet does the same.
- **"Bootstrap must be re-run first":** on Railway this already happens, because the api's pre-deploy command is `sh -c "po-db bootstrap && alembic upgrade head"`. The runbook says so prominently, has you confirm that command before merging, and gives the manual order for local and hand-run migrations.
- **PR 3 and PR 4 log event names are not in the contract.** The runbook relies only on names that exist or that the contract fixes (`inbound.handled` + `disposition`, `outbox.accepted`, `http.request` + `route`/`status_code`, task names, table state). Task 4 then has a step that greps PR 3 and PR 4 for their log events and adds them to the failure table.
- **The day-1 "webhook 5xx or timeout" check** cannot be simulated exactly on Railway. The runbook first reads the answer from GOWA's source, then confirms it with three real probes: a 401 (a secret mismatch), a timeout (an unroutable webhook URL) and a 502 (an api restart).
- **Two more stale "managed auth / cutover" texts** besides the `ComposioSettings` docstring: `.env.example` and the `interfaces/calendar.py` module docstring. Task 5 fixes all three.

## Global Constraints

- Python `>=3.14`; ruff (line length 100, the repo's rule set, `except A, B:` without parentheses as the formatter writes it), `mypy --strict` over `src` and `tests`. All three must pass: `uv run ruff check . && uv run ruff format --check . && uv run mypy`.
- Task kwargs carry ids only (ADR 0001). The health task takes only Procrastinate's `timestamp`.
- Logging is an allowlist (`observability/redaction.py:SAFE_KEYS`): use only keys that are already allowlisted. The task logs `reason`, `status_code` and `error_type`, all already in `SAFE_KEYS`. Do **not** add keys to `SAFE_KEYS`.
- Never log or send an exception's message, a URL with credentials, or the basic-auth password. Use the error **class** name only.
- Contract names, verbatim: `GOWA_HEALTH_TASK: Final = "system:gowa_health"`, cron `*/5 * * * *`, events `gowa.unhealthy` (error) and `gowa.healthy` (debug), and no persisted state.
- Docs use the house style: plain sentences, tables for variables and failures, `**bold**` for the one thing not to miss, and no emoji. ADRs use `Status / Context / Decision / Alternatives rejected / Consequences`.
- Railway names: the api service is `personal-organizer` and the worker is `worker`. The CLI defaults to production, so every command passes `--environment staging`. The dashboard shows our JSON log lines blank, so read logs with `railway logs --json`.
- Commit messages end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## Review Focus

1. **The gateway answers 200 with something that is not two booleans** (a reverse proxy's login page, `"false"` as a string, `results: null`). A reasonable person expects "unhealthy, bad_response", never "healthy". Pinned in Task 1 (`test_garbage_is_a_bad_response`, `test_strings_are_not_booleans`) and in the CLI (`TestStatusSharesTheHealthCheck`).
2. **The gateway accepts the connection and never answers.** One run should end within seconds as `unreachable`, and runs should never overlap. Pinned in Task 1 (`test_a_timeout_is_unreachable`) and Task 2 (`test_a_hung_gateway_cannot_outlast_the_period`).
3. **No Sentry DSN (local, CI) or Sentry down.** The check should still log and never raise. Pinned in Task 2 (`test_without_sentry_it_still_logs_and_does_not_raise`).
4. **A worker with GOWA off, or GOWA on for the api only.** The worker should not register the task, so it cannot alarm about a gateway it does not use. Pinned in Task 2 (`test_not_registered_without_the_gateway`). A job left queued from before a disable is a single harmless "Task was not found"; that row is in the runbook's failure table (Task 4).
5. **An operator migrates a database bootstrapped by an older checkout.** The migration should fail with a named error, and the runbook should map that error to "run bootstrap first". This is a docs check, pinned in Task 4 (the warning box, the failure table rows, and the cross-check step that greps those exact error strings in PR 2's migration and bootstrap).

---

## File structure

| File | Responsibility |
|---|---|
| `src/personal_organizer/providers/channel/gowa/status.py` (new) | Ask the gateway's `/app/status` once and map the answer onto `GatewayHealth`. Sync and async entry points share URL, auth, headers and parsing. |
| `src/personal_organizer/providers/channel/gowa/cli.py` (modify) | `status` prints `check_status_sync`'s result. Output and exit codes stay the same. |
| `src/personal_organizer/worker/tasks/gowa_health.py` (new) | The periodic task: check, log, report to Sentry. |
| `src/personal_organizer/worker/app.py` (modify) | Registers the task when `settings.gowa.enabled`. |
| `tests/unit/test_gowa_status.py` (new), `tests/unit/test_gowa_cli.py` (modify), `tests/worker/test_gowa_health.py` (new) | Tests. |
| `docs/runbook-whatsapp-gateway.md` (modify) | The alarm: what it says, how to prove it, how to resolve it. |
| `docs/adr/0005-tenant-resolution-and-the-connect-flow.md` (new) | D1, D12, D4, D5. |
| `docs/runbook-iteration-03.md` (new) | Everything outside the code for Iteration 03. |
| `README.md`, `docs/runbook-iteration-02.md`, `src/personal_organizer/settings.py`, `.env.example`, `src/personal_organizer/interfaces/calendar.py` (modify) | Fixes. |

---

### Task 1: One gateway status check, shared by `po-gowa status` and the worker

**Files:**
- Create: `src/personal_organizer/providers/channel/gowa/status.py`
- Modify: `src/personal_organizer/providers/channel/gowa/cli.py` (module docstring `status` paragraph, imports, `def status`)
- Test: `tests/unit/test_gowa_status.py` (new), `tests/unit/test_gowa_cli.py` (append a class)

**Interfaces:**
- Consumes: `GowaSettings` (`base_url` without trailing slash, `basic_auth_user`, `basic_auth_password: SecretStr | None`, `device_id`), `DEVICE_HEADER` from `gowa/outbound.py`.
- Produces (Task 2 relies on these exact names):
  - `class Unhealthy(StrEnum)`: `UNREACHABLE = "unreachable"`, `NOT_CONNECTED = "not_connected"`, `NOT_LOGGED_IN = "not_logged_in"`, `BAD_RESPONSE = "bad_response"`
  - `@dataclass(frozen=True, slots=True) class GatewayHealth`: `reason: Unhealthy | None`, `connected: bool | None = None`, `logged_in: bool | None = None`, `status_code: int | None = None`, `error_type: str | None = None`, and a `property ok -> bool`
  - `async def check_status(client: httpx.AsyncClient, gowa: GowaSettings) -> GatewayHealth`
  - `def check_status_sync(client: httpx.Client, gowa: GowaSettings) -> GatewayHealth`
  - `def interpret(response: httpx.Response) -> GatewayHealth`, `STATUS_PATH: Final = "app/status"`

- [ ] **Step 1: Write the failing tests for the shared check**

Create `tests/unit/test_gowa_status.py`:

```python
from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx
import pytest

from personal_organizer.providers.channel.gowa.status import (
    GatewayHealth,
    Unhealthy,
    check_status,
    check_status_sync,
)
from personal_organizer.settings import Settings
from tests.api.test_gowa_webhook import GOWA_ENV

Respond = Callable[[httpx.Request], httpx.Response]


@pytest.fixture
def gowa_settings(settings_factory: Callable[..., Settings]) -> Settings:
    return settings_factory(**GOWA_ENV)


def status_body(*, connected: Any, logged_in: Any) -> Respond:
    return lambda _r: httpx.Response(
        200,
        json={"code": "SUCCESS", "results": {"is_connected": connected, "is_logged_in": logged_in}},
    )


def refuse(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("refused", request=request)


async def _check(settings: Settings, respond: Respond) -> tuple[GatewayHealth, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return respond(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        return await check_status(client, settings.gowa), seen


class TestCheckStatus:
    async def test_connected_and_logged_in_is_healthy(self, gowa_settings: Settings) -> None:
        health, _ = await _check(gowa_settings, status_body(connected=True, logged_in=True))
        assert health.ok
        assert health == GatewayHealth(None, connected=True, logged_in=True)

    async def test_it_asks_the_status_endpoint_with_basic_auth(
        self, gowa_settings: Settings
    ) -> None:
        _, (request,) = await _check(gowa_settings, status_body(connected=True, logged_in=True))
        assert request.method == "GET"
        assert str(request.url) == "http://localhost:3000/app/status"
        assert request.headers["authorization"].startswith("Basic ")
        assert "x-device-id" not in request.headers

    async def test_a_device_id_is_sent_when_configured(
        self, settings_factory: Callable[..., Settings]
    ) -> None:
        settings = settings_factory(**GOWA_ENV, GOWA__DEVICE_ID="po-bot")
        _, (request,) = await _check(settings, status_body(connected=True, logged_in=True))
        assert request.headers["x-device-id"] == "po-bot"

    async def test_logged_out(self, gowa_settings: Settings) -> None:
        health, _ = await _check(gowa_settings, status_body(connected=True, logged_in=False))
        assert health.reason is Unhealthy.NOT_LOGGED_IN
        assert not health.ok

    async def test_reconnecting(self, gowa_settings: Settings) -> None:
        health, _ = await _check(gowa_settings, status_body(connected=False, logged_in=True))
        assert health.reason is Unhealthy.NOT_CONNECTED

    async def test_logged_out_wins_over_disconnected(self, gowa_settings: Settings) -> None:
        """Only one of the two needs a person with the bot's phone in hand."""
        health, _ = await _check(gowa_settings, status_body(connected=False, logged_in=False))
        assert health.reason is Unhealthy.NOT_LOGGED_IN

    async def test_unreachable_names_the_error_class_only(self, gowa_settings: Settings) -> None:
        health, _ = await _check(gowa_settings, refuse)
        assert health.reason is Unhealthy.UNREACHABLE
        assert health.error_type == "ConnectError"

    async def test_a_timeout_is_unreachable(self, gowa_settings: Settings) -> None:
        def slow(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("slow", request=request)

        health, _ = await _check(gowa_settings, slow)
        assert health.reason is Unhealthy.UNREACHABLE
        assert health.error_type == "ReadTimeout"

    async def test_a_wrong_password_is_a_bad_response(self, gowa_settings: Settings) -> None:
        health, _ = await _check(gowa_settings, lambda _r: httpx.Response(401))
        assert health.reason is Unhealthy.BAD_RESPONSE
        assert health.status_code == 401

    @pytest.mark.parametrize(
        "respond",
        [
            lambda _r: httpx.Response(200, text="<html>login</html>"),
            lambda _r: httpx.Response(200, json={"results": None}),
            lambda _r: httpx.Response(200, json={"results": {"is_connected": True}}),
            lambda _r: httpx.Response(200, json=["not", "an", "object"]),
        ],
    )
    async def test_garbage_is_a_bad_response(
        self, gowa_settings: Settings, respond: Respond
    ) -> None:
        health, _ = await _check(gowa_settings, respond)
        assert health.reason is Unhealthy.BAD_RESPONSE
        assert health.status_code is None

    async def test_strings_are_not_booleans(self, gowa_settings: Settings) -> None:
        """``"false"`` is truthy: reading it loosely would report a logged-out gateway as
        healthy, which is the one failure this check exists to catch."""
        health, _ = await _check(gowa_settings, status_body(connected="true", logged_in="false"))
        assert health.reason is Unhealthy.BAD_RESPONSE

    async def test_it_refuses_without_credentials(self, settings: Settings) -> None:
        with pytest.raises(ValueError, match="GOWA__BASIC_AUTH_USER"):
            await _check(settings, status_body(connected=True, logged_in=True))


class TestCheckStatusSync:
    def test_it_sends_the_same_request_and_reads_the_same_answer(
        self, gowa_settings: Settings
    ) -> None:
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return status_body(connected=True, logged_in=False)(request)

        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            health = check_status_sync(client, gowa_settings.gowa)
        assert health.reason is Unhealthy.NOT_LOGGED_IN
        (request,) = seen
        assert str(request.url) == "http://localhost:3000/app/status"
        assert request.headers["authorization"].startswith("Basic ")

    def test_unreachable(self, gowa_settings: Settings) -> None:
        with httpx.Client(transport=httpx.MockTransport(refuse)) as client:
            health = check_status_sync(client, gowa_settings.gowa)
        assert health.reason is Unhealthy.UNREACHABLE
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/unit/test_gowa_status.py -q`
Expected: collection error, `ModuleNotFoundError: No module named 'personal_organizer.providers.channel.gowa.status'`.

- [ ] **Step 3: Write `status.py`**

Create `src/personal_organizer/providers/channel/gowa/status.py`:

```python
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
```

- [ ] **Step 4: Run the new tests**

Run: `uv run pytest tests/unit/test_gowa_status.py -q`
Expected: all pass (17 tests).

- [ ] **Step 5: Write the failing CLI tests**

Append to `tests/unit/test_gowa_cli.py`. It already imports `httpx`, `Any`, `Settings` and `status`, and defines `_client` and `TestStatus._status`.

```python


class TestStatusSharesTheHealthCheck:
    """``status`` and the worker's ``system:gowa_health`` read the gateway through one
    function, so the operator's check and the alert cannot disagree."""

    def test_a_wrong_password_names_the_status(self, gowa_settings: Settings, capsys: Any) -> None:
        client, _ = _client(lambda _r: httpx.Response(401))
        assert status(gowa_settings, client=client) == 1
        assert "failed: HTTP 401" in capsys.readouterr().out

    def test_reconnecting_is_a_failing_exit_code(
        self, gowa_settings: Settings, capsys: Any
    ) -> None:
        client, _ = _client(TestStatus._status(False, True))
        assert status(gowa_settings, client=client) == 1
        out = capsys.readouterr().out
        assert "connected=False logged_in=True" in out
        assert "scan the QR code" not in out

    def test_strings_are_not_booleans(self, gowa_settings: Settings, capsys: Any) -> None:
        client, _ = _client(
            lambda _r: httpx.Response(
                200, json={"results": {"is_connected": "true", "is_logged_in": "false"}}
            )
        )
        assert status(gowa_settings, client=client) == 1
        assert "unexpected response" in capsys.readouterr().out
```

- [ ] **Step 6: Run them to see the one that fails**

Run: `uv run pytest tests/unit/test_gowa_cli.py -q`
Expected: `test_strings_are_not_booleans` FAILS: the old code treats `"true"` and `"false"` as truthy, prints `connected=true logged_in=false` and exits 0. The other two pass already, and they guard the refactor.

- [ ] **Step 7: Put `status` on the shared check**

In `src/personal_organizer/providers/channel/gowa/cli.py`:

Replace the docstring paragraph

```text
``status``
    Ask the gateway whether its WhatsApp session is connected and logged in -- the first
    thing to check when replies stop (docs/runbook-whatsapp-gateway.md).
```

with

```text
``status``
    Ask the gateway whether its WhatsApp session is connected and logged in -- the first
    thing to check when replies stop (docs/runbook-whatsapp-gateway.md). The worker's
    ``system:gowa_health`` task asks the same question every five minutes (``status.py``).
```

Replace the import `from personal_organizer.providers.channel.gowa.outbound import DEVICE_HEADER` with (after the `parser` import, so isort order holds):

```python
from personal_organizer.providers.channel.gowa.status import Unhealthy, check_status_sync
```

Replace the whole `def status(...)` with:

```python
def status(settings: Settings, *, client: httpx.Client) -> int:
    gowa = settings.gowa
    if gowa.basic_auth_user is None or gowa.basic_auth_password is None:
        _out("GOWA__BASIC_AUTH_USER and GOWA__BASIC_AUTH_PASSWORD must be set")
        return 2
    health = check_status_sync(client, gowa)
    if health.reason is Unhealthy.UNREACHABLE:
        _out(f"unreachable: {health.error_type} at {gowa.base_url}")
        return 1
    if health.reason is Unhealthy.BAD_RESPONSE:
        if health.status_code is not None:
            _out(f"failed: HTTP {health.status_code}")
        else:
            _out("failed: unexpected response from the gateway")
        return 1
    _out(f"connected={health.connected} logged_in={health.logged_in}")
    if health.reason is Unhealthy.NOT_LOGGED_IN:
        _out("not logged in: open the gateway's UI and scan the QR code from the bot's phone")
    return 0 if health.ok else 1
```

- [ ] **Step 8: Run the gateway tests and the static checks**

Run: `uv run pytest tests/unit/test_gowa_status.py tests/unit/test_gowa_cli.py -q && uv run ruff check src tests && uv run ruff format --check src tests && uv run mypy`
Expected: all pass. The existing `TestStatus` tests (URL, basic auth, `x-device-id`, the QR hint, unreachable, garbage) still pass unchanged, which proves the CLI output did not change.

- [ ] **Step 9: Commit**

```bash
git add src/personal_organizer/providers/channel/gowa/status.py src/personal_organizer/providers/channel/gowa/cli.py tests/unit/test_gowa_status.py tests/unit/test_gowa_cli.py
git commit -m "$(cat <<'EOF'
refactor: one gateway status check for po-gowa status and the worker

The request and its parsing move to gowa/status.py, with sync and async
entry points, so the CLI and the coming health task cannot disagree. The
parse is now strict: "false" as a string is a bad response, not healthy.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: `system:gowa_health`, registered only with the gateway on

**Files:**
- Create: `src/personal_organizer/worker/tasks/gowa_health.py`
- Modify: `src/personal_organizer/worker/app.py` (import line, and the end of `build_procrastinate_app`)
- Modify: `docs/runbook-whatsapp-gateway.md` (Phase C step 8, the "When replies stop" table, "Re-linking")
- Test: `tests/worker/test_gowa_health.py` (new)

**Interfaces:**
- Consumes (Task 1): `Unhealthy`, `GatewayHealth`, `check_status(client, gowa)`.
- Produces:
  - `GOWA_HEALTH_TASK: Final = "system:gowa_health"`, `GOWA_HEALTH_CRON: Final = "*/5 * * * *"`, `SENTRY_FINGERPRINT: Final = ("gowa.unhealthy",)`, `GOWA_HEALTH_TIMEOUT: Final = httpx.Timeout(10.0, connect=5.0)`
  - `async def run_health_check(gowa: GowaSettings, client: httpx.AsyncClient) -> GatewayHealth`
  - `def register(app: procrastinate.App, gowa: GowaSettings, *, transport: httpx.AsyncBaseTransport | None = None) -> None`
  - Log events: `gowa.unhealthy` (error; `reason`, `status_code`, `error_type`) and `gowa.healthy` (debug). Sentry: message `gowa.unhealthy`, level `error`, fingerprint `["gowa.unhealthy"]`, tags `reason` and (when known) `status_code`.

- [ ] **Step 1: Write the failing tests**

Create `tests/worker/test_gowa_health.py`:

```python
"""``system:gowa_health``: a logged-out linked device must never be silent."""

from __future__ import annotations

import io
import json
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

import httpx
import procrastinate
import pytest
import sentry_sdk
from procrastinate.testing import InMemoryConnector
from sentry_sdk.integrations.logging import LoggingIntegration
from sentry_sdk.types import Event, Hint

from personal_organizer.observability.logging import configure_logging
from personal_organizer.settings import Settings
from personal_organizer.worker.app import build_procrastinate_app
from personal_organizer.worker.tasks.gowa_health import (
    GOWA_HEALTH_TASK,
    GOWA_HEALTH_TIMEOUT,
    SENTRY_FINGERPRINT,
    register,
    run_health_check,
)
from tests.api.test_gowa_webhook import GOWA_ENV

Respond = Callable[[httpx.Request], httpx.Response]


@pytest.fixture
def gowa_settings(settings_factory: Callable[..., Settings]) -> Settings:
    return settings_factory(**GOWA_ENV, LOGGING__LEVEL="DEBUG", LOGGING__RENDERER="json")


@pytest.fixture
def log_stream(gowa_settings: Settings) -> io.StringIO:
    """The real processor chain, redaction included, writing to a buffer."""
    stream = io.StringIO()
    configure_logging(gowa_settings, stream=stream)
    return stream


def _lines(stream: io.StringIO) -> list[dict[str, Any]]:
    return [json.loads(line) for line in stream.getvalue().splitlines() if line.strip()]


@contextmanager
def sentry_events() -> Iterator[list[Event]]:
    """A real Sentry client whose ``before_send`` keeps each event and drops it.

    Its logging integration is off, as ``init_sentry`` turns it off in production, so the
    only events counted are ones the code captures explicitly.
    """
    events: list[Event] = []

    def keep(event: Event, _hint: Hint) -> Event | None:
        events.append(event)
        return None

    client = sentry_sdk.Client(
        dsn="https://k@o.ingest.sentry.io/1",
        before_send=keep,
        integrations=[LoggingIntegration(level=None, event_level=None)],
    )
    with sentry_sdk.new_scope() as scope:
        scope.set_client(client)
        yield events
    client.close()


def status(*, connected: bool, logged_in: bool) -> Respond:
    return lambda _r: httpx.Response(
        200, json={"results": {"is_connected": connected, "is_logged_in": logged_in}}
    )


def refuse(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("refused", request=request)


async def _run(settings: Settings, respond: Respond) -> None:
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        await run_health_check(settings.gowa, client)


class TestRegistration:
    def test_registered_on_a_worker_with_the_gateway(
        self, settings_factory: Callable[..., Settings]
    ) -> None:
        settings = settings_factory(**GOWA_ENV)
        app = build_procrastinate_app(settings)
        assert GOWA_HEALTH_TASK in app.tasks
        periodic = app.periodic_registry.periodic_tasks[(GOWA_HEALTH_TASK, "")]
        assert periodic.cron == "*/5 * * * *"

    def test_not_registered_without_the_gateway(self, settings: Settings) -> None:
        """A worker with no gateway would alarm every five minutes about nothing."""
        app = build_procrastinate_app(settings)
        assert GOWA_HEALTH_TASK not in app.tasks
        assert all(name != GOWA_HEALTH_TASK for name, _ in app.periodic_registry.periodic_tasks)

    def test_it_runs_on_a_queue_the_worker_subscribes_to(
        self, settings_factory: Callable[..., Settings]
    ) -> None:
        """Otherwise the job is deferred every five minutes and nothing ever runs it."""
        settings = settings_factory(**GOWA_ENV)
        app = build_procrastinate_app(settings)
        assert app.tasks[GOWA_HEALTH_TASK].queue in settings.worker.queues

    def test_runs_never_pile_up(self, settings_factory: Callable[..., Settings]) -> None:
        app = build_procrastinate_app(settings_factory(**GOWA_ENV))
        assert app.tasks[GOWA_HEALTH_TASK].queueing_lock == GOWA_HEALTH_TASK


class TestTheTask:
    async def test_the_registered_task_asks_the_gateway(self, gowa_settings: Settings) -> None:
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return status(connected=True, logged_in=True)(request)

        app = procrastinate.App(connector=InMemoryConnector())
        register(app, gowa_settings.gowa, transport=httpx.MockTransport(handler))
        await app.tasks[GOWA_HEALTH_TASK](timestamp=0)

        (request,) = seen
        assert str(request.url) == "http://localhost:3000/app/status"

    def test_a_hung_gateway_cannot_outlast_the_period(self) -> None:
        """Every phase of the request is bounded, far inside the five minutes between runs,
        so a gateway that accepts and never answers ends one run as `unreachable`."""
        phases = (
            GOWA_HEALTH_TIMEOUT.connect,
            GOWA_HEALTH_TIMEOUT.read,
            GOWA_HEALTH_TIMEOUT.write,
            GOWA_HEALTH_TIMEOUT.pool,
        )
        assert all(phase is not None and phase <= 30 for phase in phases)

    async def test_without_sentry_it_still_logs_and_does_not_raise(
        self, gowa_settings: Settings, log_stream: io.StringIO
    ) -> None:
        """Local and CI have no DSN: capture_message must be a quiet no-op there."""
        with sentry_sdk.new_scope() as scope:
            scope.set_client(None)
            await _run(gowa_settings, refuse)
        assert "gowa.unhealthy" in log_stream.getvalue()


class TestUnhealthy:
    @pytest.mark.parametrize(
        ("respond", "reason"),
        [
            (refuse, "unreachable"),
            (status(connected=True, logged_in=False), "not_logged_in"),
            (status(connected=False, logged_in=True), "not_connected"),
            (lambda _r: httpx.Response(401), "bad_response"),
            (lambda _r: httpx.Response(200, text="<html>"), "bad_response"),
        ],
    )
    async def test_logs_at_error_with_the_reason(
        self, gowa_settings: Settings, log_stream: io.StringIO, respond: Respond, reason: str
    ) -> None:
        with sentry_events():
            await _run(gowa_settings, respond)
        (line,) = [entry for entry in _lines(log_stream) if entry["event"] == "gowa.unhealthy"]
        assert line["level"] == "error"
        assert line["reason"] == reason, "reason must survive redaction (SAFE_KEYS)"

    async def test_the_status_code_and_error_class_survive_redaction(
        self, gowa_settings: Settings, log_stream: io.StringIO
    ) -> None:
        with sentry_events():
            await _run(gowa_settings, lambda _r: httpx.Response(401))
            await _run(gowa_settings, refuse)
        lines = [entry for entry in _lines(log_stream) if entry["event"] == "gowa.unhealthy"]
        assert [line["status_code"] for line in lines] == [401, None]
        assert [line["error_type"] for line in lines] == [None, "ConnectError"]

    async def test_reaches_sentry_as_one_issue(self, gowa_settings: Settings) -> None:
        """The log line alone would not: init_sentry turns logging events off. One fixed
        fingerprint for every reason is what makes Sentry's issue the alert."""
        with sentry_events() as events:
            await _run(gowa_settings, refuse)
            await _run(gowa_settings, status(connected=True, logged_in=False))
        assert [event["message"] for event in events] == ["gowa.unhealthy"] * 2
        assert all(event["level"] == "error" for event in events)
        assert all(event["fingerprint"] == list(SENTRY_FINGERPRINT) for event in events)
        assert [event["tags"]["reason"] for event in events] == ["unreachable", "not_logged_in"]

    async def test_the_password_never_leaves(
        self, gowa_settings: Settings, log_stream: io.StringIO
    ) -> None:
        with sentry_events() as events:
            await _run(gowa_settings, lambda _r: httpx.Response(401))
        assert "gateway-password" not in log_stream.getvalue()
        assert "gateway-password" not in json.dumps(events, default=str)


class TestHealthy:
    async def test_logs_at_debug_and_sends_nothing_to_sentry(
        self, gowa_settings: Settings, log_stream: io.StringIO
    ) -> None:
        with sentry_events() as events:
            await _run(gowa_settings, status(connected=True, logged_in=True))
        assert events == []
        ours = [line for line in _lines(log_stream) if line["event"].startswith("gowa.")]
        assert [(line["event"], line["level"]) for line in ours] == [("gowa.healthy", "debug")]
```

Two notes on these tests. The `sentry_events()` client must keep `integrations=[LoggingIntegration(level=None, event_level=None)]`: with Sentry's default logging integration, the `log.error` line would become a second, `logentry`-shaped event, and the test would pass for the wrong reason. The healthy test filters on `gowa.*` because, at DEBUG, asyncio logs `Using selector: EpollSelector` to the same stream.

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/worker/test_gowa_health.py -q`
Expected: collection error, `ModuleNotFoundError: No module named 'personal_organizer.worker.tasks.gowa_health'`.

- [ ] **Step 3: Write the task module**

Create `src/personal_organizer/worker/tasks/gowa_health.py`:

```python
"""``system:gowa_health`` -- the alarm for a linked device that has stopped working.

A gateway whose device was logged out still answers its REST API. Nothing fails loudly:
inbound messages simply stop arriving, and replies fail as transient 401s. So every five
minutes the worker asks the gateway what ``po-gowa status`` asks, and while the answer is
bad it says so twice: an error-level ``gowa.unhealthy`` log line, and a Sentry event.

The Sentry event is explicit because nothing else would send it: ``init_sentry`` switches
the logging integration's events off (``event_level=None``), so no log line, at any level,
becomes a Sentry event by itself. Every report carries one fixed fingerprint, so Sentry
groups them all into a single issue -- *that issue is the alert* -- with the reason as a
tag. Resolve it once the gateway is fixed, and the next outage raises it again.

Registered only on a worker with ``GOWA__ENABLED`` (``worker.app``). It keeps no state:
each run reports what it sees.
"""

from __future__ import annotations

from typing import Final

import httpx
import procrastinate
import sentry_sdk
import structlog

from personal_organizer.providers.channel.gowa.status import (
    GatewayHealth,
    Unhealthy,
    check_status,
)
from personal_organizer.settings import GowaSettings
from personal_organizer.worker.queues import Queue

log = structlog.get_logger(__name__)

GOWA_HEALTH_TASK: Final = "system:gowa_health"
GOWA_HEALTH_CRON: Final = "*/5 * * * *"
#: The Sentry grouping key: every report, whatever its reason, lands in one issue.
SENTRY_FINGERPRINT: Final = ("gowa.unhealthy",)
#: Far inside the five-minute period, so a hung gateway cannot make runs overlap.
GOWA_HEALTH_TIMEOUT: Final = httpx.Timeout(10.0, connect=5.0)


def _report(reason: Unhealthy, health: GatewayHealth) -> None:
    with sentry_sdk.new_scope() as scope:
        scope.fingerprint = list(SENTRY_FINGERPRINT)
        scope.set_tag("reason", reason.value)
        if health.status_code is not None:
            scope.set_tag("status_code", str(health.status_code))
        sentry_sdk.capture_message("gowa.unhealthy", level="error")


async def run_health_check(gowa: GowaSettings, client: httpx.AsyncClient) -> GatewayHealth:
    """Ask once; log, and report to Sentry when unhealthy. Returns what it found."""
    health = await check_status(client, gowa)
    reason = health.reason
    if reason is None:
        log.debug("gowa.healthy")
        return health
    log.error(
        "gowa.unhealthy",
        reason=reason.value,
        status_code=health.status_code,
        error_type=health.error_type,
    )
    _report(reason, health)
    return health


def register(
    app: procrastinate.App,
    gowa: GowaSettings,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> None:
    """Attach the periodic check. ``transport`` exists for tests (``httpx.MockTransport``)."""

    @app.periodic(cron=GOWA_HEALTH_CRON)
    @app.task(
        queue=Queue.MAINTENANCE.value,
        name=GOWA_HEALTH_TASK,
        queueing_lock=GOWA_HEALTH_TASK,
    )
    async def gowa_health(
        timestamp: int,  # noqa: ARG001 - periodic tasks receive their schedule time
    ) -> None:
        async with httpx.AsyncClient(timeout=GOWA_HEALTH_TIMEOUT, transport=transport) as client:
            await run_health_check(gowa, client)


__all__ = [
    "GOWA_HEALTH_CRON",
    "GOWA_HEALTH_TASK",
    "GOWA_HEALTH_TIMEOUT",
    "SENTRY_FINGERPRINT",
    "register",
    "run_health_check",
]
```

- [ ] **Step 4: Run the tests: registration still fails**

Run: `uv run pytest tests/worker/test_gowa_health.py -q`
Expected: `TestTheTask`, `TestUnhealthy` and `TestHealthy` pass. In `TestRegistration`, `test_registered_on_a_worker_with_the_gateway`, `test_it_runs_on_a_queue_…` and `test_runs_never_pile_up` FAIL with `KeyError: 'system:gowa_health'`, because nothing registers the task yet.

- [ ] **Step 5: Register it from `build_procrastinate_app`**

In `src/personal_organizer/worker/app.py`, change the import

```python
from personal_organizer.worker.tasks import REGISTRARS
```

to

```python
from personal_organizer.worker.tasks import REGISTRARS, gowa_health
```

and replace

```python
    for register in REGISTRARS:
        register(app)
    return app
```

with

```python
    for register in REGISTRARS:
        register(app)
    # Settings-dependent, so not in REGISTRARS: without a gateway the check could only ever
    # alarm. The api builds this app too, and registers it there as well when GOWA is on;
    # harmless, since only a worker runs periodic tasks.
    if settings.gowa.enabled:
        gowa_health.register(app, settings.gowa)
    return app
```

Do not change the `REGISTRARS` list or its type: PR 4 appended `onboarding.register` to it.

- [ ] **Step 6: Run the worker and gateway tests and the static checks**

Run: `uv run pytest tests/worker tests/unit/test_gowa_status.py tests/unit/test_gowa_cli.py tests/api -q && uv run ruff check src tests && uv run ruff format --check src tests && uv run mypy`
Expected: all pass. `tests/worker/test_app.py` is unchanged and still green; its `settings` fixture has GOWA off.

- [ ] **Step 7: Document the alarm in the gateway runbook**

In `docs/runbook-whatsapp-gateway.md`, after Phase C step 7 (the paragraph that ends "`inbound.handled`."), insert:

````markdown
8. **Prove the alarm.** Every five minutes the worker asks the gateway what `po-gowa status`
   asks (`system:gowa_health`). While the answer is bad it logs `gowa.unhealthy` at error
   level and sends Sentry one event; all of them group into a single issue named
   `gowa.unhealthy`, tagged with the `reason`. To see it work: in Railway, `gowa` → its
   deployment's ⋮ → **Restart** is too quick, so instead set the worker's `GOWA__BASE_URL` to
   `http://gowa.railway.internal:3999` (a port nothing listens on) and redeploy the worker.
   Within five minutes Sentry shows `gowa.unhealthy` with `reason: unreachable`. Put the URL
   back, redeploy, and **resolve the issue** in Sentry. A resolved issue reopens (a
   regression) the next time, and alerts again. An unresolved one just collects events.
   Check that the Sentry project's alert rules email you on a new issue and on a regression.
````

In the "When replies stop" table, add these rows directly under the header row (before `api logs gowa.signature_rejected`):

```markdown
| Sentry issue `gowa.unhealthy`, `reason: not_logged_in` | the linked device was logged out | re-link (below), then resolve the issue |
| `gowa.unhealthy`, `reason: unreachable` | the gateway is down, or not reachable at `GOWA__BASE_URL` | check the `gowa` service; see C5 |
| `gowa.unhealthy`, `reason: bad_response`, `status_code: 401` | the worker's `GOWA__BASIC_AUTH_*` differs from the gateway's `APP_BASIC_AUTH` | make them equal |
| `gowa.unhealthy`, `reason: not_connected`, once | the gateway was reconnecting to WhatsApp when it was asked | nothing, if it does not recur; recurring means the gateway's network |
```

At the end of the "Re-linking" section, add:

```markdown
Then resolve the `gowa.unhealthy` issue in Sentry, so the next logout raises it again.
```

- [ ] **Step 8: Commit**

```bash
git add src/personal_organizer/worker/tasks/gowa_health.py src/personal_organizer/worker/app.py tests/worker/test_gowa_health.py docs/runbook-whatsapp-gateway.md
git commit -m "$(cat <<'EOF'
feat: system:gowa_health -- a logged-out gateway raises a Sentry issue

Every five minutes, on workers with GOWA enabled, ask the gateway's status
endpoint. Unreachable, logged out, reconnecting or answering nonsense logs
gowa.unhealthy at error level and sends one Sentry event under a fixed
fingerprint: init_sentry keeps log lines out of Sentry, so the capture is
explicit, and the one issue it groups into is the alert.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: ADR 0005 — tenant resolution and the connect flow

**Files:**
- Create: `docs/adr/0005-tenant-resolution-and-the-connect-flow.md`

**Interfaces:**
- Consumes: the spec's D1, D4, D5 and D12; the contract's names (`app_definer`, `resolve_tenant`, `create_tenant`, `DatabaseRole.DEFINER`, `/connect/{token}`, `/connect/callback`, `onboarding_links.used_at`, `tenant_identities (network, external_id)`, `NETWORK_WHATSAPP`).
- Produces: the ADR that the runbook (Task 4) and the README (Task 5) link to, with a "Day-1 results" place in its Consequences, where the runbook says to record the Composio checks.

- [ ] **Step 1: Check the names against the code on this branch**

Run:

```bash
grep -n "DEFINER\|app_definer" src/personal_organizer/db/roles.py alembic/bootstrap.sql
grep -n "resolve_tenant\|create_tenant\|SECURITY DEFINER\|search_path" alembic/versions/0004_tenants.py
grep -rn "Referrer-Policy\|no-store\|default-src\|/connect" src/personal_organizer/api/routers/connect.py src/personal_organizer/observability/
grep -n "user_id\|auth_config_id\|ACTIVE" src/personal_organizer/api/routers/connect.py
```

Expected: every name the ADR text below uses appears. If PR 2 or PR 4 named something differently (for example the status constant PR 4 compares against), use the code's name in the ADR. The ADR describes what was built.

- [ ] **Step 2: Write the ADR**

Create `docs/adr/0005-tenant-resolution-and-the-connect-flow.md`:

````markdown
# ADR 0005 — Tenants are resolved through a narrow definer door, and the connect flow trusts only what it signed or fetched

**Status:** accepted (Iteration 03)

## Context

From Iteration 03 every table that holds a person's data is a tenant table: RLS enabled and
*forced*, with one policy reading `NULLIF(current_setting('app.tenant_id', true), '')::uuid`.
The api and worker connect as `app_user`, which owns nothing and cannot bypass RLS, and
`Database.tenant_session(tid)` sets `app.tenant_id` on every transaction. That is the point:
a missing `WHERE tenant_id = …` returns nothing rather than someone else's rows.

It leaves four problems, and each one is easy to solve in a way that quietly undoes the
point:

1. **The worker must find the tenant before it knows the tenant.** An inbound message carries
   a sender key (`tel:+…`, or `uid:…` from Meta), not a tenant id. Mapping one to the other
   means reading `tenant_identities`, which RLS hides until `app.tenant_id` is set, and it
   cannot be set yet. Creating a tenant for a new invitee has the same problem.
2. **One person can reach us on two gateways.** The GOWA gateway and Meta's Cloud API are two
   channels (`gowa`, `whatsapp`) for one network. ADR 0004 already decided that a person has
   one key across them.
3. **WhatsApp opens links before people do.** To build a preview, it fetches every URL in a
   message, from the sender's phone or from WhatsApp's servers. A connect link that does
   something on GET is used up, or acted on, by a robot.
4. **Composio's callback arrives as a browser redirect.** Its query string (connected account
   id, status) passes through the user's browser, so anyone can type it. A callback that
   believed it would let a person bind someone else's Google account to their tenant, or
   bind a failed connection.

## Decision

1. **Tenant resolution goes through a narrow `SECURITY DEFINER` door (D1).**
   - Bootstrap creates `app_definer`: `NOLOGIN`, `BYPASSRLS`, granted to `app_owner` so
     that migrations can hand it functions, and **not** granted to `app_user`.
   - It owns exactly two functions, each with `SET search_path = public, pg_temp`, `EXECUTE`
     revoked from `PUBLIC` and granted to `app_user` only:
     - `resolve_tenant(network, external_id) → uuid | NULL`
     - `create_tenant(network, external_id, phone, language) → uuid`, atomic and idempotent
       on the identity key: a concurrent second call returns the first call's tenant.
   - They return an id and nothing else. Everything after that runs in
     `tenant_session(tid)`, under RLS like the rest of the code.
   - `DatabaseRole.DEFINER` exists so that code can name the role, and it has no DSN: asking
     for one raises. `tests/db/test_roles.py` pins three things: `app_user` cannot become
     `app_definer`, `app_definer` cannot log in, and the definer-owned function set is
     exactly these two.
2. **Identities are keyed by network, not by gateway (D12).** `tenant_identities.network` is
   `whatsapp` for both the `gowa` and the `whatsapp` channel, with
   `UNIQUE (network, external_id)`. `external_id` is the `SenderRef.key`: `tel:+…`, or `uid:…`
   for a Meta user known only by a BSUID. `resolve_tenant` tries `uid:` first, then `tel:`.
   When a BSUID later arrives for a phone-keyed tenant, the worker adds the `uid:` row to that
   tenant rather than creating a second one. A future Telegram channel is the network
   `telegram`.
3. **The connect link is consumed on POST, never on GET (D4).**
   - `GET /connect/{token}` validates nothing that matters and changes nothing. It renders a
     page with one button.
   - `POST /connect/{token}` verifies the signature (itsdangerous, `ONBOARDING__LINK_SECRET`,
     max age `ONBOARDING__LINK_TTL_S`), then marks the link used with one atomic
     `UPDATE … SET used_at = now WHERE used_at IS NULL AND expires_at > now`, then answers
     303 to Composio. The signature proves we issued the link; the `onboarding_links` row
     makes it single-use.
   - The page sends `Referrer-Policy: no-referrer`, `Cache-Control: no-store` and
     `Content-Security-Policy: default-src 'none'`. It loads no external asset and runs no
     script.
   - Sentry's URL scrubbing treats `/connect/*` paths as secret, because the token is in the
     path.
4. **The callback trusts Composio's API, not its query string (D5).** `GET /connect/callback`
   first verifies our own signed `state` (tenant id + link nonce). It then fetches the
   connected account from Composio by id and requires three things:
   `user_id == str(tenant_id)`, `auth_config_id ==` our configured one, and status `ACTIVE`.
   Only then does it bind the connection and activate the tenant, in one tenant transaction,
   and defer `onboarding:connected`, which sends "You're all set" once (idempotency key
   `connected:<connection_id>`). Composio's `user_id` is always the tenant UUID, never a phone
   number.

## Alternatives rejected

- **A `BYPASSRLS` runtime role, or a worker connection as `app_owner`, for the lookup.** The
  worker would hold a key to every tenant's data for the sake of one query, and a bug
  anywhere in it would use that key. `app_owner` could also `ALTER` or `DISABLE` the policies.
- **Leaving `tenant_identities` without RLS.** The lookup becomes a plain query, but the table
  of who-is-which-phone becomes readable by every query, by any code path.
- **A policy escape hatch, such as `OR current_setting('app.resolving') = 'on'`.** `app_user`
  can set any custom GUC, so the hatch is open to every query.
- **Resolving the tenant at ingress, in the api.** ADR 0002 keeps ingress tenant-free: one
  transaction that stores and defers, before anything is known about the sender.
- **Identities keyed by channel (`gowa` / `whatsapp`).** The same person writing to both
  numbers would become two tenants with two calendars, and merging tenants later is far
  harder than never splitting them.
- **Keyed by phone number alone.** Meta's BSUID users may have no phone number in the
  payload.
- **Consuming the link on GET**, or a GET page that auto-submits. A preview fetch would use up
  the link, and CSP `default-src 'none'` forbids the script an auto-submit needs anyway.
- **A long-lived link with no single-use row.** A forwarded or screenshotted link could be
  replayed until it expired.
- **Trusting the callback's `connected_account_id` and `status` parameters.** They are
  forgeable by anyone who can type a URL.
- **Composio webhooks instead of a fetch.** That is another endpoint and another secret, and
  it is asynchronous, so the page cannot say "connected". The fetch is one call, at the moment
  the user is looking.

## Consequences

- **Bootstrap must run before migration 0004.** The migration hands the functions to
  `app_definer`, which only bootstrap creates. On Railway the api's pre-deploy command
  already runs `po-db bootstrap && alembic upgrade head`; by hand, run them in that order
  (`docs/runbook-iteration-03.md`, section 1).
- **A third definer function is an ADR-level change.** The test that pins the set is the
  tripwire. Prefer a function that returns an id, as these two do, over one that returns
  rows.
- `resolve_tenant` is an oracle for "is this sender key a tenant?" to anyone who can run SQL
  as `app_user`. That role already reads `channel_inbox`'s sender keys, so this exposes
  nothing new.
- Composio never sees a phone number: its `user_id` is our tenant UUID. Deleting a tenant
  (a later iteration) will have to delete its Composio connected accounts by that id.
- Rotating `ONBOARDING__LINK_SECRET` invalidates every outstanding link and in-flight
  callback. That costs a user one fresh link, and nothing else.
- A reconnect binds a new `calendar_connections` row and marks the old `active` row
  `revoked`, so a tenant has at most one active connection (a partial unique index).
  Whether Composio itself refuses a second account for the same user under the same auth
  config is a day-1 question, recorded below.
- Merging two tenants (a person on WhatsApp and, later, on Telegram) is not supported. It
  will need its own linking flow, not a change to the identity key.

### Day-1 results (Composio with our own Google client)

Recorded from `docs/runbook-iteration-03.md` section 6 once they are run: whether the
connect link passes Google's unverified-app screen and returns `ACTIVE`; the names of the
callback's query parameters; what a reconnect does; and whether the SDK logs request bodies
or sends telemetry.
````

- [ ] **Step 3: Check that the links in the ADR resolve**

Run: `ls docs/adr/0002-atomic-ingress-in-one-transaction.md docs/adr/0004-whatsapp-via-a-qr-code-gateway.md tests/db/test_roles.py`
Expected: all three exist. (`docs/runbook-iteration-03.md` is created in Task 4.)

- [ ] **Step 4: Commit**

```bash
git add docs/adr/0005-tenant-resolution-and-the-connect-flow.md
git commit -m "$(cat <<'EOF'
docs: ADR 0005 -- tenant resolution and the connect flow

D1 (two SECURITY DEFINER functions owned by a NOLOGIN app_definer), D12
(identities keyed by network), D4 (the link is consumed on POST) and D5 (the
callback trusts Composio's API, not its query string), with the alternatives
each one rules out.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 4: `docs/runbook-iteration-03.md`

**Files:**
- Create: `docs/runbook-iteration-03.md`

**Interfaces:**
- Consumes: `Settings` validation messages (`settings.py`: `_composio_is_complete`, `_enforce_deployed_invariants`, `AppSettings._origin_only`, `ComposioSettings._looks_like_an_auth_config`); the api pre-deploy command (`docs/runbook-iteration-01.md` section 1); Task 2's `gowa.unhealthy` and Phase C step 8; ADR 0004 and ADR 0005 (Task 3).
- Produces: the runbook that `.env.example` (line 81) and the README (Task 5) already point to.

- [ ] **Step 1: Collect the names the runbook depends on**

Run:

```bash
grep -n "Invalid Composio settings\|must be an origin\|must be https\|real secret when deployed\|must be an auth config" src/personal_organizer/settings.py
grep -rn "app_definer" alembic/bootstrap.sql alembic/versions/0004_tenants.py | head
grep -rnE "log\.(debug|info|warning|error)\(\s*\"" src/personal_organizer/onboarding src/personal_organizer/api/routers/connect.py src/personal_organizer/worker/tasks/onboarding.py src/personal_organizer/providers/calendar src/personal_organizer/messaging | grep -v "inbound.handled\|outbox\." 
grep -rn "queue=" src/personal_organizer/worker/tasks/onboarding.py
```

Expected: the four settings messages exist as quoted below. `app_definer` appears in both bootstrap and the migration. The third command lists the log events PR 3 and PR 4 emit (for example a connect or callback rejection with a `reason`). Keep that list for Step 3.

- [ ] **Step 2: Write the runbook**

Create `docs/runbook-iteration-03.md`:

````markdown
# Runbook — Iteration 03: tenants, onboarding and the Google connect

Iteration 03 is done when **an invited person writes to the bot, confirms their time zone
with a numbered reply, taps one link, connects Google Calendar and is told they are set up,
with their data under RLS from the first message**. Isolation and the choice parsing are
mechanised in the test suite (last section). What remains is outside the code: a Google
OAuth app, a Composio auth config, and one run through on staging from a real phone.

The code merges and deploys **with Composio switched off** (`COMPOSIO__ENABLED` unset). In
that state `/connect/*` is not mounted, no new variable is required, and invited senders
keep Iteration 02's acknowledgement. Only the migration runs. Nothing here has to happen
before merging.

| Step | Needs | Proves |
|---|---|---|
| 1. The migration | the api's pre-deploy command | `app_definer`, the tenant tables and RLS on staging |
| 2. Google OAuth app | a Google Cloud project | a consent screen whose refresh tokens last |
| 3. Composio auth config | Composio's staging project | Composio connects with *our* Google client |
| 4. Variables | 1–3 | onboarding switched on |
| 5. End to end | the GOWA SIM linked; your phone | the Done-When |
| 6. Day 1 and day 8 | 5 | what the next iterations build on |

Staging only. Production stays paused until the first invitee (the ops checklist, below).

---

## 1. The migration: bootstrap first

> **`po-db bootstrap` must run before `alembic upgrade head`, on every database.** Migration
> 0004 hands its two `SECURITY DEFINER` functions to the role `app_definer`, and only
> bootstrap creates that role and grants it to `app_owner`. Run the migration on a database
> last bootstrapped by an older checkout and it stops with `role "app_definer" does not
> exist`, or `must be member of role "app_definer"`.

**On Railway** this is already the order: the api's pre-deploy command is
`sh -c "po-db bootstrap && alembic upgrade head"` (runbook 01, section 1). Before merging,
open staging's `personal-organizer` → Settings → Deploy and confirm it still says exactly
that, `sh -c` included. Merge, then read the pre-deploy log: `db.bootstrap.ok`, then Alembic
running `0003_channel_ledgers -> 0004_tenants`.

**Locally**, and anywhere you migrate by hand:

```sh
uv run po-db bootstrap        # creates app_definer (NOLOGIN, BYPASSRLS), grants it to app_owner
uv run alembic upgrade head   # 0004_tenants
uv run po-db check            # app_user is still correctly constrained
```

Bootstrap is idempotent. Run it again whenever you pull a branch that touches
`alembic/bootstrap.sql`.

**Check it on staging.** Run `railway connect Postgres --environment staging`, which connects
as the superuser and bypasses RLS, so read only:

```sql
SELECT rolname, rolcanlogin, rolbypassrls FROM pg_roles WHERE rolname = 'app_definer';
-- app_definer | f | t
SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class
WHERE relname IN ('tenants', 'tenant_identities', 'calendar_connections',
                  'onboarding_links', 'messages');
-- five rows, t | t
```

## 2. The Google OAuth app: "In production", unverified

One Google Cloud project and one OAuth client. Staging's Composio project uses it now, and
production's later.

1. console.cloud.google.com → create a project (e.g. `personal-organizer`).
2. APIs & Services → Library → **Google Calendar API** → Enable.
3. Google Auth Platform → **Branding**: app name, your support email, developer contact. Add
   no logo: a logo triggers brand verification.
4. **Audience**: user type **External**, then **Publish app**. The publishing status must read
   **In production**. Do not submit it for verification.
5. **Data Access** → Add or remove scopes → `https://www.googleapis.com/auth/calendar`, and
   nothing else. It is a *sensitive* scope, not a restricted one, which is what makes an
   unverified production app possible.
6. **Clients** → Create client → **Web application**. Authorised redirect URI: Composio's
   callback, exactly as its auth config form shows it (step 3.2). At the time of writing that
   is `https://backend.composio.dev/api/v3/toolkits/auth/callback`. Keep the client id and
   secret for step 3.

**Why not "Testing":** in Testing, Google expires refresh tokens after about **7 days**, so
every user would be silently disconnected once a week. Testing also admits only listed test
users.

**What unverified production costs**, all acceptable for an invite-only circle:

- **Google's warning screen.** Users see "Google hasn't verified this app" and must tap
  **Advanced → Go to <app name> (unsafe)**. Tell invitees before they tap the link.
- **A 100-user cap** over the project's lifetime, not concurrently. That is twice the circle
  this is for. Verification and the cutover campaign were dropped for good on 2026-10-07.

## 3. Composio: a custom auth config with that client

Use Composio's staging project first. Production later gets its own project and its own auth
config with the same Google client.

1. platform.composio.dev → the **staging** project → Settings → API keys. That key is
   staging's `COMPOSIO__API_KEY`, and only staging's.
2. Auth Configs → Create auth config → **Google Calendar** → OAuth2 → **your own credentials**
   (not Composio's managed app):
   - the client id and secret from 2.6;
   - scopes: `https://www.googleapis.com/auth/calendar`. Remove any default scope that Data
     Access (2.5) does not list, or add it there too, so the consent screen asks for exactly
     what the app declares;
   - the **redirect URL** the form shows: put it in the Google client (2.6) if it differs.
3. Save. The auth config id (`ac_…`) is `COMPOSIO__CALENDAR_AUTH_CONFIG_ID`. The setting
   refuses anything else. A connected account id (`ca_…`) pasted by mistake is the usual
   culprit.

Every `calendar_connections` row records the auth config that made it, so changing the
setting later affects new connections only.

## 4. The variables

Set these on staging's **api and worker** (`personal-organizer` and `worker`), the same set on
both:

| Variable | Value | Used by |
|---|---|---|
| `COMPOSIO__ENABLED` | `true` | api: mounts `/connect/*`. worker: turns onboarding on (off, invited senders keep the Iteration 02 ack) |
| `COMPOSIO__API_KEY` | the staging project's key (3.1) | api: creates the connect link, fetches the account on callback |
| `COMPOSIO__CALENDAR_AUTH_CONFIG_ID` | `ac_…` (3.3) | api: links are made under it, and the callback requires it |
| `COMPOSIO__REQUEST_TIMEOUT_S` | leave unset (15) | api |
| `APP__PUBLIC_BASE_URL` | `https://<staging api domain>`: an origin, no path | worker: builds the link. api: Composio's callback URL |
| `ONBOARDING__LINK_SECRET` | `uv run python -c "import secrets; print(secrets.token_hex(32))"` | worker signs links; api verifies them and the callback's state |
| `ONBOARDING__LINK_TTL_S` | leave unset (900); 60–3600 | both |

- **`ONBOARDING__LINK_SECRET` must be identical on both services.** The worker signs and the
  api verifies, so a mismatch makes every link "invalid" and nothing else fails. Use one
  Railway shared variable, or paste the same value twice.
- With `COMPOSIO__ENABLED=true`, a service refuses to boot unless the API key, the auth config
  id, `APP__PUBLIC_BASE_URL` and `ONBOARDING__LINK_SECRET` are all set, and the error names
  every missing one. That is why the full set goes on both services, even where one does not
  read a value.
- When deployed, `APP__PUBLIC_BASE_URL` must be `https://`, and the link secret must not be
  `.env.example`'s placeholder.
- The staging domain is Railway's generated one; no product domain is needed. Like the
  webhook, it must target port **8080** (runbook 01, the PORT trap).
- Unchanged and still needed: `WHATSAPP__ALLOWED_PHONES` (the invite list, for every
  channel) and the `GOWA__*` set (`docs/runbook-whatsapp-gateway.md`).

## 5. The end to end, on staging

**Before you start:** `po-gowa status` against staging's gateway prints
`connected=True logged_in=True`, and Sentry has no open `gowa.unhealthy` issue. Your number is
on `WHATSAPP__ALLOWED_PHONES`, and you have not written to the bot since the migration (if
you have, see "Starting over" below). Read logs with the CLI, since the dashboard shows JSON
lines blank: `railway logs --service worker --environment staging --json`, and
`--service personal-organizer` for the api.

1. **Write "hi"** to the SIM's number. Expect a welcome and a numbered time-zone choice,
   "Your time zone is Asia/Jerusalem?" with `1` Correct and `2` Change. That is for a +972
   number; a number with no zone guess is asked for its city. In the worker log:
   `inbound.handled` with `disposition: onboarding`.
2. **Reply `1`.** Expect one message with one link: `https://<staging>/connect/<token>`.
   From a second invited phone, try the other path. Write the first message in Hebrew, then
   `2`, then a city (`London`, or `תל אביב`). That proves the Hebrew strings (D11), the city
   match, and that both paths reach the same link step.
3. **Do not tap yet.** WhatsApp has already fetched the link for its preview. The api log has
   an `http.request` with `method: GET` and `route: /connect/{token}`. The link must still
   work, because GET never consumes it (ADR 0005).
4. **Tap the link.** You get a page with one button. Tap it: the api answers 303 to
   Composio, which sends you to Google.
5. **At Google:** choose the account. At the unverified-app warning, tap **Advanced → Go to …
   (unsafe)**, then allow Calendar access.
6. **Back on our page** (`/connect/callback`), it says you are connected. Within seconds
   WhatsApp says "You're all set". In the worker log: the `onboarding:connected` job, then
   `outbox.accepted` with `channel: gowa`. **Write today's date down: the day-8 check counts
   from it.**
7. **Check the state** as the superuser (`railway connect Postgres --environment staging`):

   ```sql
   SELECT id, status, onboarding_step, language, timezone FROM tenants;
   SELECT tenant_id, network FROM tenant_identities;
   SELECT tenant_id, connected_account_id, auth_config_id, status FROM calendar_connections;
   SELECT tenant_id, used_at IS NOT NULL AS used FROM onboarding_links;
   SELECT tenant_id, direction, channel, message_type FROM messages;
   ```

   Expect:
   - the tenant `active`, with a NULL step and your zone;
   - one `whatsapp` identity;
   - one `active` connection under the `ac_…` from step 3;
   - the link used;
   - your inbound messages in `messages`, with `channel_inbox.body` nulled for them.

   In Composio → Connected accounts, the account is **ACTIVE** and its user id is the
   tenant's UUID, never a phone number.
8. **Replay the link**: tap it again, or open it in another browser. The page says the link
   was already used, and nothing changes.
9. **Write again.** An active tenant gets the fixed acknowledgement (`disposition: allowed`)
   until the agent arrives.

### Starting over with the same phone

There is no tenant deletion yet. To run onboarding again from scratch, as the superuser:

```sql
DELETE FROM tenants WHERE id = '<uuid>';  -- identities, links, connections, messages cascade
```

Then delete the account in Composio → Connected accounts, so the next connect does not meet
the old one.

### Reconnecting one user

There is no reconnect flow yet (it arrives with calendar reads). To send one user a fresh
link, as the superuser:

```sql
UPDATE tenants SET status = 'onboarding', onboarding_step = 'connect' WHERE id = '<uuid>';
```

Their next message gets a new link. The new connection marks the old one `revoked`.

## 6. Day-1 and day-8 checks

None of these block the iteration. They decide later work, so write each answer down where
the next plan will read it.

### Day 1: the GOWA SIM (record in ADR 0004, Consequences)

1. **Buttons and lists.** Look in the gateway's API docs (its UI, or `docs/openapi.yaml` in
   the GOWA repository at the deployed tag, `v9.6.0`) for an endpoint that sends buttons or a
   list. If there is one, send each to an iPhone and to an Android phone and note what each
   shows. If there is none, that is the answer.
2. **Polls.** Send one to your own number (check the field names against the same API docs
   if this answers 400):

   ```sh
   curl -u "<user>:<password>" -X POST https://<gowa domain>/send/poll \
     -H 'content-type: application/json' \
     -d '{"phone": "<your number, digits only>@s.whatsapp.net", "question": "Test?", "options": ["One", "Two"], "max_answer": 1}'
   ```

   Does it render on both phones? Then vote. Read the api log: an `ingress.recorded` with
   `message_count: 1` means the vote arrived and our parser kept it; `skipped_count: 1`
   means it arrived and the parser dropped it; no line at all means GOWA did not forward it,
   or `WHATSAPP_WEBHOOK_EVENTS` filtered it out.
3. **When our webhook fails.** First read the answer in GOWA's source at `v9.6.0`: find
   where it posts webhooks and note its timeout, number of attempts and backoff. Then
   confirm with three short probes. In each, send one message from your phone during the
   failure, restore, and watch whether that message arrives later, how often, and for how
   long the gateway logs retries:
   - **a 4xx:** set a different `GOWA__WEBHOOK_SECRET` on the api only, and redeploy it (we
     answer 401);
   - **a timeout:** set the gateway's `WHATSAPP_WEBHOOK` to
     `http://10.255.255.1/webhooks/gowa`, which is unroutable, so every attempt times out.
     The volume keeps the session across the gateway's redeploy;
   - **a 5xx:** `personal-organizer` → ⋮ → **Restart**, and send while it restarts (Railway's
     edge answers 502). If the restart is too quick to catch, the timeout probe stands in.

   A message lost on a 4xx matters most: unlike Meta, which redelivers for seven days, a
   secret mismatch may then drop messages for good.

Record the results as one bullet at the end of ADR 0004's Consequences, in this shape:

```markdown
- **Day-1 checks (<date>, GOWA v9.6.0).** Buttons: … Lists: … Polls: render on iOS …, on
  Android …; votes … Webhook failures: on 401 …; on a timeout …; on a 502 … (attempts,
  interval, how long before it gives up).
```

### Day 1: Composio with our own client (record in ADR 0005, "Day-1 results")

- **The warning screen.** Did the connect pass Google's unverified-app screen and return
  `ACTIVE`? Step 5 of the end to end answers this.
- **The callback's query parameters.** At step 6, note the parameter *names* in the browser's
  address bar, not their values.
- **A reconnect.** Use "Reconnecting one user" above with the same Google account. Does
  Composio return a second `ACTIVE` account (the old row becomes `revoked`), or refuse with a
  multiple-accounts error? If it refuses, the old account has to be deleted in Composio
  first; record which.
- **The SDK's own output.** During the end to end, look for any log line from a `composio`
  logger at INFO or above that carries more than ids, and check whether the SDK sends
  telemetry. Either is a leak to fix before inviting anyone.

### Day 8: the refresh token survived

Eight days after step 6 (Testing mode's expiry is about seven), confirm that the connection
still works. The app has no calendar reads yet, so ask Composio directly, with staging's key
and the connection's `connected_account_id` from step 7:

```sh
COMPOSIO_API_KEY=<staging key> uv run python - <<'EOF'
from composio import Composio

result = Composio().tools.execute(
    "GOOGLECALENDAR_LIST_CALENDARS",
    {},
    connected_account_id="ca_...",
    dangerously_skip_version_check=True,
)
print(result["successful"], result["error"])
EOF
```

Expect `True None`. The dashboard does the same: Connected accounts → the account shows
**ACTIVE** → run a read-only Calendar tool on it.

An auth error, or an account that is no longer ACTIVE, means the refresh token died. Either
the app was still in Testing when the user connected, or the user revoked access in their
Google account. Publish the app (2.4), then reconnect them (section 5). Record the result in
the ops checklist below.

## When it does not work

### Deploying

| Symptom | Cause | Fix |
|---|---|---|
| pre-deploy fails: `role "app_definer" does not exist` | the migration ran without bootstrap first | restore the pre-deploy command to `sh -c "po-db bootstrap && alembic upgrade head"`; by hand, run bootstrap, then upgrade |
| pre-deploy fails: `must be member of role "app_definer"` | bootstrap ran from an older checkout, so `GRANT app_definer TO app_owner` is missing | run this branch's `po-db bootstrap`, then migrate |
| boot fails: `Invalid Composio settings: … required when COMPOSIO__ENABLED is true` | one of the four is missing on that service | set the full set on both services (section 4) |
| boot fails: `APP__PUBLIC_BASE_URL must be an origin …` or `… must be https:// when deployed` | a path, a trailing route, or `http://` | the bare `https://` origin |
| boot fails: `ONBOARDING__LINK_SECRET must be set to a real secret when deployed` | `.env.example`'s placeholder | generate one (section 4) |
| boot fails: `COMPOSIO__CALENDAR_AUTH_CONFIG_ID must be an auth config id (ac_...)` | a `ca_…` or a toolkit slug | the `ac_…` id from 3.3 |

### Onboarding

| Symptom | Cause | Fix |
|---|---|---|
| an invited person still gets "Got it — I'm not smart yet" on their first message | `COMPOSIO__ENABLED` is not `true` on the **worker**: onboarding is gated on it | set it there too, with the full set |
| an invited person gets the invite-only line | their number is not on the list in the form it arrives | compare `po-gowa hash +<number>` with `sender_hash` on `inbound.handled` |
| no reply at all | the gateway | Sentry `gowa.unhealthy`; `docs/runbook-whatsapp-gateway.md`, "When replies stop" |
| every link page says the link is invalid | `ONBOARDING__LINK_SECRET` differs between api and worker | make them equal and redeploy both; then any message gets a fresh link |
| the link page says it expired, or was used, on the first tap | older than `ONBOARDING__LINK_TTL_S` (15 min), or tapped before (in another browser, or by someone it was forwarded to) | write any message to the bot: it re-sends the latest usable link, or a fresh one |
| api answers 404 on `/connect/…` | `COMPOSIO__ENABLED` is not `true` on the api | set it |

### Google and Composio

| Symptom | Cause | Fix |
|---|---|---|
| Google: `Error 400: redirect_uri_mismatch` | the Google client lacks Composio's callback URL | add it exactly as the auth config shows it (2.6) |
| Google: "Access blocked: … has not completed the Google verification process" | the app is still in **Testing**, and the user is not a test user | publish it (2.4) |
| Google: "This app is blocked" | a restricted scope was requested | Calendar only (2.5, 3.2) |
| Google: "Google hasn't verified this app" | expected | Advanced → Go to … (unsafe) |
| our callback page shows an error, and the api log has `http.request route: /connect/callback` with a 4xx | the `state` expired (consent took longer than the TTL), or the account failed D5's checks: a different `user_id`, an auth config other than `COMPOSIO__CALENDAR_AUTH_CONFIG_ID` (e.g. the API key and the config id from different Composio projects), or not `ACTIVE` | the same project for both; then a fresh link |
| connected, but no "You're all set" | the `onboarding:connected` job has not run, or the send failed | the worker log for that job and its `outbox.*` line; the worker's queues include the job's queue (`WORKER__QUEUES`) |
| the calendar stops working about a week after connecting | the app was in Testing when the user connected | publish, then "Reconnecting one user" |

### The gateway alarm

| Symptom | Cause | Fix |
|---|---|---|
| Sentry issue `gowa.unhealthy` | the gateway cannot carry messages; the `reason` tag says why | `docs/runbook-whatsapp-gateway.md`, "When replies stop"; then resolve the issue |
| worker logs `Task was not found` for `system:gowa_health`, once | GOWA was switched off on the worker while a check was queued | nothing: it is registered only with GOWA on |

## Ops checklist

- [ ] Pause the production services (`personal-organizer`, `worker`) until the first
      invitee: each deployment's ⋮ → **Remove**. Keep Postgres. Run on staging only. The
      next promotion to `production` deploys them again.
- [ ] When production comes back: **Postgres backups enabled before any real data.**
- [ ] Google OAuth app published "In production" (section 2). **Day 8 after connecting:
      confirm the calendar still works** (section 6), which proves the refresh token
      survived. Result: ____
- [ ] Day-1 GOWA checks (section 6), recorded in ADR 0004's Consequences.
- [ ] Day-1 Composio checks (section 6), recorded in ADR 0005's Day-1 results.
- [ ] The gateway alarm proved on staging (`docs/runbook-whatsapp-gateway.md`, Phase C
      step 8), and Sentry alerts on a new issue and on a regression.
- [ ] Before the agent iteration: read Anthropic's model deprecations page and the current
      model ids. v7's date for Haiku 4.5 is Oct 15, 2026, and `MODELS__FALLBACK_MODEL` is
      `claude-haiku-4-5` in both environments.

## Where the Done-When is proved

| Criterion | Where |
|---|---|
| An invited number onboards from WhatsApp and connects Google Calendar | section 5, on staging; and `tests/db/test_onboarding.py`, `tests/api/test_connect.py` with a fake Composio |
| Isolation holds with app-level tenant filters disabled | `tests/db/test_isolation.py` (`-m rls`), run once normally and once with `scoped()` patched to a no-op |
| A choice works on every channel | `tests/unit/test_choices.py`: numbered, label and alias replies, Meta button `reply_id`, GOWA `selection.selected_id` |
| A logged-out gateway is never silent | `tests/worker/test_gowa_health.py`; on staging, `docs/runbook-whatsapp-gateway.md` Phase C step 8 |

## Deliberately not in this iteration

- Calendar reads, Composio usage metering, and reconnecting on auth errors: the
  calendar-read iteration.
- `pending_actions` and `scheduled_jobs`; token and message budgets.
- Native buttons and polls (`send_choice`): chosen by the day-1 checks.
- Tenant deletion, data export, and DB-backed invites: before the circle grows past a
  handful of people.
````

- [ ] **Step 3: Reconcile the runbook with PR 3 and PR 4 as built**

Using the output of Step 1:
- For each log event PR 3 or PR 4 emits on a failure path (for example a callback rejection with a `reason`), add its event name, and its `reason` value if it has one, to the Symptom cell of the matching row in "Onboarding" or "Google and Composio". A Symptom cell reads like `api logs <event> reason: <value>`.
- If `onboarding.py`'s task has a `queue=` other than one in the default `WORKER__QUEUES` (`default`, `webhooks`, `maintenance`), name that queue in the "connected, but no You're all set" row.
- If PR 3's zone prompt or PR 4's page wording differs from the paraphrases in section 5 steps 1, 6 and 8, change the paraphrase to the real text (`messaging/onboarding_text.py`, the jinja templates).
- Check that `tests/db/test_onboarding.py`, `tests/api/test_connect.py`, `tests/db/test_isolation.py` and `tests/unit/test_choices.py` exist: `ls` them. If PR 3 or PR 4 put one under another name, use that name in "Where the Done-When is proved".

- [ ] **Step 4: Check every path and setting the runbook names**

Run:

```bash
for f in docs/runbook-iteration-01.md docs/runbook-whatsapp-gateway.md docs/adr/0004-whatsapp-via-a-qr-code-gateway.md docs/adr/0005-tenant-resolution-and-the-connect-flow.md tests/worker/test_gowa_health.py; do test -e "$f" || echo "missing $f"; done
grep -c "COMPOSIO__ENABLED\|COMPOSIO__API_KEY\|COMPOSIO__CALENDAR_AUTH_CONFIG_ID\|COMPOSIO__REQUEST_TIMEOUT_S\|APP__PUBLIC_BASE_URL\|ONBOARDING__LINK_SECRET\|ONBOARDING__LINK_TTL_S" docs/runbook-iteration-03.md
grep -n "request_timeout_s\|link_ttl_s\|public_base_url\|link_secret" src/personal_organizer/settings.py
```

Expected: no `missing` lines; a non-zero count; and the settings fields exist with the defaults the table states (15.0 and 900).

- [ ] **Step 5: Commit**

```bash
git add docs/runbook-iteration-03.md
git commit -m "$(cat <<'EOF'
docs: the Iteration 03 runbook

The Google OAuth app published unverified (and why not Testing), Composio's
custom auth config, every new variable on api and worker, bootstrap before
migration 0004, the staging end to end through the GOWA SIM, a failure
table, the day-1 and day-8 checks and the ops checklist.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 5: README, the Phase B step 1 fix, and the stale "managed auth" texts

**Files:**
- Modify: `README.md`
- Modify: `docs/runbook-iteration-02.md` (the banner, and Phase B steps 1–2)
- Modify: `src/personal_organizer/settings.py` (the `ComposioSettings` docstring and the `calendar_auth_config_id` comment)
- Modify: `.env.example` (the comment above `COMPOSIO__CALENDAR_AUTH_CONFIG_ID`)
- Modify: `src/personal_organizer/interfaces/calendar.py` (the module docstring, unless PR 4 already rewrote it)

**Interfaces:**
- Consumes: `docs/runbook-iteration-03.md` (Task 4) and ADR 0005 (Task 3), which the README links to.
- Produces: nothing code depends on. Only docstrings and comments change in `.py` files.

- [ ] **Step 1: README: what exists now**

Replace the opening paragraph's last two sentences

```markdown
WhatsApp. This repository is the implementation of the v7 plan. **Iteration 01 (walking
skeleton)** and **Iteration 02 (WhatsApp webhook, queue and access controls)** exist: an
allowlisted sender gets a fixed acknowledgement, everyone else a one-line "invite-only"
reply. There is no agent yet.
```

with

```markdown
WhatsApp. This repository is the implementation of the v7 plan. **Iteration 01 (walking
skeleton)**, **Iteration 02 (WhatsApp webhook, queue and access controls)** and **Iteration
03 (tenants under RLS, onboarding and the Google Calendar connect)** exist. An invited sender
is onboarded: they confirm a time zone with a numbered reply, then tap one link to connect
Google Calendar. After that they get a fixed acknowledgement. Everyone else gets a one-line
"invite-only" reply. There is no agent yet.
```

- [ ] **Step 2: README: the "What is here" table**

Replace these four rows

```markdown
| `src/personal_organizer/api/` | FastAPI service. `/health` (liveness), `/ready` (readiness, monitoring only), `/webhooks/whatsapp` and `/webhooks/gowa` (each when enabled). |
| `src/personal_organizer/worker/` | Procrastinate worker and task registry. |
| `src/personal_organizer/db/` | Async engines per role, tenant-scoped sessions, bootstrap, `po-db`, ORM models. |
| `src/personal_organizer/messaging/` | Provider-neutral ingress (persist-then-ack), the access-control gate, at-most-once replies. |
```

with

```markdown
| `src/personal_organizer/api/` | FastAPI service. `/health` (liveness), `/ready` (readiness, monitoring only), `/webhooks/whatsapp` and `/webhooks/gowa` (each when enabled), `/connect/*` (when Composio is enabled). |
| `src/personal_organizer/worker/` | Procrastinate worker and task registry, including `system:gowa_health`, the gateway alarm. |
| `src/personal_organizer/db/` | Async engines per role, tenant-scoped sessions, bootstrap, `po-db`, ORM models, and `repositories/`: tenant-scoped queries, each also filtered through `scoped()`. |
| `src/personal_organizer/messaging/` | Provider-neutral ingress (persist-then-ack), the invite and onboarding gate, numbered choices, at-most-once replies. |
| `src/personal_organizer/onboarding/` | Signed, single-use connect links. |
| `src/personal_organizer/providers/calendar/` | Composio: the connect link and the connected-account check. |
```

and replace

```markdown
| `alembic/` | Migrations, `bootstrap.sql`, vendored Procrastinate schema. |
```

with

```markdown
| `alembic/` | Migrations, `bootstrap.sql`, `rls.py` (the one way a tenant table gets its policy), vendored Procrastinate schema. |
```

- [ ] **Step 3: README: onboarding, bootstrap again, roles, and where the variables are**

After the paragraph "To link a real phone, … Both, and staging, are `docs/runbook-whatsapp-gateway.md`." (the last one in "WhatsApp without a Meta app"), add:

```markdown
### Onboarding and the Google connect

Onboarding runs only with `COMPOSIO__ENABLED=true`. Off (the default), invited senders get
the fixed acknowledgement, as in Iteration 02. On, a first message from an invited number
creates its tenant and starts two steps: the time zone, as a numbered choice, then a signed,
single-use link to connect Google Calendar through Composio. The link is consumed on POST,
never on GET, because WhatsApp fetches links to build previews. Composio's callback is
believed only after a fetch of the account from Composio's API (docs/adr/0005). The Google
OAuth app, the Composio auth config and every variable are in
`docs/runbook-iteration-03.md`.
```

In "Getting started", replace

```sh
uv run po-db bootstrap        # extension, roles, ownership, default privileges
```

with

```sh
uv run po-db bootstrap        # extension, roles, ownership, default privileges; re-run after pulls
```

Replace the "**Database roles.**" paragraph with:

```markdown
**Database roles.** There are four, not two: a superuser for bootstrap only; `app_owner`,
which owns everything and runs Alembic; `app_user`, which the api and worker connect as; and
`app_definer`, which cannot log in and owns exactly two `SECURITY DEFINER` functions, the
only way to map a sender to a tenant before RLS knows the tenant (docs/adr/0005).
`app_owner` is deliberately *not* a superuser, because superusers bypass RLS and would make
`FORCE ROW LEVEL SECURITY` inert. Bootstrap creates the roles, so run it before migrating.
See `src/personal_organizer/db/roles.py`.
```

At the end of "Deployment", after the paragraph ending "connecting the gateway is `docs/runbook-whatsapp-gateway.md`.", add:

```markdown
Onboarding and the Google connect are off unless `COMPOSIO__ENABLED=true`, and then
`COMPOSIO__API_KEY`, `COMPOSIO__CALENDAR_AUTH_CONFIG_ID`, `APP__PUBLIC_BASE_URL` and
`ONBOARDING__LINK_SECRET` are required on both services. The Google OAuth app, the Composio
auth config, those variables and the staging end to end are `docs/runbook-iteration-03.md`.
```

- [ ] **Step 4: Phase B step 1 in `docs/runbook-iteration-02.md`**

In the banner, delete the last sentence, so that

```markdown
> GOWA gateway instead: `docs/runbook-whatsapp-gateway.md`, and docs/adr/0004. Phase B step 1
> is also out of date: Meta now creates the app from the "Connect with customers through
> WhatsApp" use case, not app type "Business" plus an added product.
```

becomes

```markdown
> GOWA gateway instead: `docs/runbook-whatsapp-gateway.md`, and docs/adr/0004.
```

Replace Phase B steps 1 and 2

```markdown
1. **Create the app.** developers.facebook.com → My Apps → Create app → type **Business** →
   add the **WhatsApp** product. Meta creates a test WhatsApp Business Account with a free
   **test number**.
2. **API Setup** (WhatsApp → API Setup):
   - note the **Phone number ID** of the test number
   - under *To*, add your own phone as a **recipient** and confirm the code WhatsApp sends.
     The test number can only message recipients listed here (up to five).
```

with

```markdown
1. **Create the app.** developers.facebook.com → My Apps → Create app → choose the use case
   **Connect with customers through WhatsApp**, then the business portfolio. Meta creates a
   test WhatsApp Business Account with a free **test number**. There is no longer an app
   type to pick or a product to add.
2. **API Setup** (WhatsApp → API Setup, also reached from the use case's **Customize**):
   - note the **Phone number ID** of the test number
   - under *To*, add your own phone as a **recipient** and confirm the code WhatsApp sends.
     The test number can only message recipients listed here (up to five). Nothing goes in
     App Roles → Test Users: that list is for people who log in to the app, not for whom it
     messages.
```

- [ ] **Step 5: The `ComposioSettings` docstring**

In `src/personal_organizer/settings.py`, replace

```python
    """Composio, which holds users' Google tokens so that we never do (v7 Section 3).

    Off by default and all-or-nothing when on, like WhatsApp. Enabled, the api serves the
```

with

```python
    """Composio, which holds users' Google tokens so that we never do (v7 Section 3).

    The tokens are issued to *our own* Google OAuth app: published "In production" and
    unverified, which shows users Google's warning screen and caps the app at 100 users,
    plugged into a Composio custom auth config. There is no cutover to come;
    docs/runbook-iteration-03.md sets both up.

    Off by default and all-or-nothing when on, like WhatsApp. Enabled, the api serves the
```

and replace

```python
    #: The auth config *new* connections are made under: Composio's managed Google Calendar
    #: config until the Section 3.3 cutover, ours after it. Every connection row records the
    #: config that made it, so changing this never breaks an existing connection.
```

with

```python
    #: The auth config new connections are made under: our custom Google Calendar config,
    #: one per Composio project. Every connection row records the config that made it, so
    #: changing this never breaks an existing connection.
```

- [ ] **Step 6: `.env.example` and the calendar interface docstring**

In `.env.example`, replace

```text
# The managed Google Calendar auth config, from the Composio dashboard: ac_...
```

with

```text
# Our custom Google Calendar auth config (our own OAuth client), from the Composio dashboard: ac_...
```

Run: `grep -n "managed auth\|cutover" src/personal_organizer/interfaces/calendar.py`
If it prints the original lines 3–4, replace

```text
Behind this sits Composio managed auth today and our own Google OAuth app from Iteration 08,
so the cutover is an adapter swap.
```

with

```text
Behind this sits Composio, holding tokens issued to our own unverified Google OAuth app
through a custom auth config (docs/adr/0005). There is no cutover to come.
```

If it prints nothing, PR 4 already rewrote the docstring. Leave it.

- [ ] **Step 7: Check that nothing stale remains, and that the suite and static checks still pass**

Run:

```bash
grep -rn "Section 3.3\|managed Google\|managed auth\|type \*\*Business\*\*" README.md docs/runbook-*.md src .env.example
uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest -q
```

Expected: the grep prints nothing. ruff, format and mypy pass. pytest passes; the database tests skip if no local Postgres is running. With `docker compose up -d` and `uv run po-db bootstrap && uv run alembic upgrade head`, they pass too, and `uv run pytest -m rls` passes.

- [ ] **Step 8: Commit**

```bash
git add README.md docs/runbook-iteration-02.md src/personal_organizer/settings.py .env.example src/personal_organizer/interfaces/calendar.py
git commit -m "$(cat <<'EOF'
docs: README for Iteration 03; Meta's use-case app creation; no cutover

The README says what Iteration 03 added and where, and points at the new
runbook for the Composio variables. Phase B step 1 follows Meta's
"Connect with customers through WhatsApp" use case, and test users are not
needed. ComposioSettings, .env.example and the calendar interface no
longer describe managed auth and a cutover: it is our own unverified
Google OAuth app in a custom auth config.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
)"
```

---

## Self-review

- **Spec coverage.**
  - PR 5's health check: Tasks 1–2. "on workers with GOWA enabled" is `test_not_registered_without_the_gateway`; "logs at error level" is `test_logs_at_error_with_the_reason`; "Sentry groups those into one issue" is `test_reaches_sentry_as_one_issue`; "never silent" is the staging proof in the gateway runbook.
  - Runbook items: Google publishing status (§2), Composio custom auth config (§3), env vars (§4), staging end to end (§5), failure table, day-1 and day-8 (§6), ops checklist. The prerequisites map to §2, §5 "Before you start", §4 and §6.
  - ADR 0005 covers D1, D4, D5 and D12 (Task 3).
  - Fixes: the README, Phase B step 1, and the `ComposioSettings` docstring (Task 5).
  - Every ops checklist item from the spec is in the runbook's checklist.
- **Placeholders.** None in the plan's own instructions. The `____` and `…` inside the runbook's checklist and the ADR 0004 bullet template are blanks the *operator* fills in after running the checks, by design.
- **Type consistency.**
  - `check_status(client, gowa)` and `check_status_sync(client, gowa)`: argument order and names are the same in Tasks 1 and 2.
  - `GatewayHealth.reason`, `status_code` and `error_type` are used the same way in the CLI, the task and the tests.
  - `GOWA_HEALTH_TIMEOUT` is exported and tested.
  - The prototype of Tasks 1–2 was run against this branch's base: ruff, `ruff format --check`, `mypy --strict` and `pytest tests/unit tests/worker tests/api` all pass.
