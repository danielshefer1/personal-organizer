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
    HTTP_409_CONFLICT,
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
    ACCOUNT_CONFLICT,
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
        if outcome.reason == ACCOUNT_CONFLICT:
            log.error("connect.account_conflict", tenant_id=str(outcome.tenant_id))
            return pages.message_page("failed", status_code=HTTP_409_CONFLICT)
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
async def start(token: str, db: DbDep, linker: ConnectLinkerDep, settings: SettingsDep) -> Response:
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
