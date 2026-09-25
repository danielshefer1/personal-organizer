"""Health and readiness.

``/health`` is **liveness**: no I/O, no dependencies. It answers "is this process alive",
and it is what Railway's healthcheck points at.

``/ready`` is **readiness**, and per the spec it is *monitoring only* -- deliberately not
wired to the platform healthcheck. If it were, a five-second Postgres blip during a routine
database restart would fail the healthcheck and roll back a perfectly good deploy, or kill a
running one. Monitoring should see that blip; the deployment pipeline should not react to it.
"""

from __future__ import annotations

from typing import Any

import structlog
from fastapi import APIRouter, Response
from starlette.status import HTTP_503_SERVICE_UNAVAILABLE

from personal_organizer import __version__
from personal_organizer.api.deps import DbDep, SettingsDep

log = structlog.get_logger(__name__)

router = APIRouter(tags=["health"])


@router.get("/health", summary="Liveness. No dependencies.")
async def health(settings: SettingsDep) -> dict[str, Any]:
    return {
        "status": "ok",
        "version": __version__,
        "env": settings.app.env,
        "release": settings.app.release,
    }


@router.get("/ready", summary="Readiness. Monitoring only -- not the platform healthcheck.")
async def ready(db: DbDep, response: Response) -> dict[str, Any]:
    checks: dict[str, str] = {}
    try:
        await db.check()
        checks["database"] = "ok"
    except Exception as exc:
        checks["database"] = "error"
        log.warning("ready.database_unavailable", error_type=type(exc).__name__)

    ok = all(value == "ok" for value in checks.values())
    if not ok:
        response.status_code = HTTP_503_SERVICE_UNAVAILABLE
    return {"status": "ok" if ok else "degraded", "checks": checks}


__all__ = ["router"]
