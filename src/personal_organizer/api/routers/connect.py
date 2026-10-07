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
