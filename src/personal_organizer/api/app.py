"""FastAPI application factory."""

from __future__ import annotations

from typing import Any

import sentry_sdk
import structlog
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.status import HTTP_422_UNPROCESSABLE_CONTENT, HTTP_500_INTERNAL_SERVER_ERROR

from personal_organizer import __version__
from personal_organizer.api.lifespan import make_lifespan
from personal_organizer.api.middleware import RequestContextMiddleware
from personal_organizer.api.routers import health, internal
from personal_organizer.settings import Settings

log = structlog.get_logger(__name__)


def _request_id(request: Request) -> str:
    bound: dict[str, Any] = structlog.contextvars.get_contextvars()
    return str(bound.get("request_id", "")) or request.headers.get("x-request-id", "")


def create_app(settings: Settings) -> FastAPI:
    app = FastAPI(
        title="personal-organizer",
        version=__version__,
        lifespan=make_lifespan(settings),
        # The schema is of no use to WhatsApp and is a free map of the surface area.
        docs_url=None if settings.is_deployed else "/docs",
        openapi_url=None if settings.is_deployed else "/openapi.json",
    )

    # NOTE: do not add GZipMiddleware or anything else that rewrites request bodies above
    # the webhook router -- Iteration 02 verifies an HMAC over the exact received bytes.
    app.add_middleware(RequestContextMiddleware)

    app.include_router(health.router)
    if settings.app.env != "production":
        app.include_router(internal.router)

    @app.exception_handler(RequestValidationError)
    async def _validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        # Log the field locations but never exc.errors()[i]["input"]: Pydantic echoes the
        # offending value, which for this product is a phone number or a message body.
        log.warning(
            "request.invalid",
            error_type="RequestValidationError",
            error_count=len(exc.errors()),
            path=request.url.path,
        )
        return JSONResponse(
            status_code=HTTP_422_UNPROCESSABLE_CONTENT,
            content={"error": "invalid_request", "request_id": _request_id(request)},
        )

    @app.exception_handler(Exception)
    async def _unhandled_handler(request: Request, exc: Exception) -> JSONResponse:
        sentry_sdk.capture_exception(exc)
        # error_type only. Driver exception messages embed the offending row values.
        log.error("request.failed", error_type=type(exc).__name__, exc_info=exc)
        return JSONResponse(
            status_code=HTTP_500_INTERNAL_SERVER_ERROR,
            content={"error": "internal_error", "request_id": _request_id(request)},
        )

    return app


__all__ = ["create_app"]
