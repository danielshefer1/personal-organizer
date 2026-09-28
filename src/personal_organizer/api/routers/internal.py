"""Non-production endpoints.

``/internal/ping`` defers the system ping task, which is how the walking skeleton is proved
end to end: api -> queue -> worker -> database.

The router is not mounted in production, but it *is* mounted on staging, where it sits on a
public URL and enqueues a job plus a database round trip per call. So it is gated on a shared
secret as well as on the environment, and ``Settings`` refuses to boot a staging service that
has no token configured. A wrong or missing token answers **404**, not 403: a 403 confirms the
route exists, which is free reconnaissance.
"""

from __future__ import annotations

import secrets
from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request
from starlette.status import HTTP_404_NOT_FOUND

from personal_organizer.api.deps import ProcrastinateDep, get_app_settings
from personal_organizer.worker.tasks.system import PING_TASK

log = structlog.get_logger(__name__)

#: Header carrying the shared secret. Not ``Authorization``: this is not a user credential, and
#: keeping it distinct stops it being picked up by anything that special-cases auth headers.
INTERNAL_TOKEN_HEADER = "x-internal-token"  # noqa: S105 - a header name, not a credential


def require_internal_token(request: Request) -> None:
    """Reject anything without the shared secret.

    When no token is configured the endpoint stays open, but only outside a deployed
    environment -- the settings validator makes that combination unreachable on staging, so
    this is the second guard rather than the only one.

    Note what that leans on: ``is_deployed``, which is ``APP__ENV``. A deployed service with
    that variable unset used to satisfy *both* guards at once -- the router mounted because
    the env was not ``"production"``, and this check waved it through because the env was not
    deployed either. ``Settings`` now refuses to construct in that state, which is what makes
    the pair of guards independent rather than two readings of the same variable.
    """
    settings = get_app_settings(request)
    expected = settings.app.internal_token
    if expected is None:
        if settings.is_deployed:
            raise HTTPException(status_code=HTTP_404_NOT_FOUND)
        return
    supplied = request.headers.get(INTERNAL_TOKEN_HEADER, "")
    # compare_digest, not ==, so a rejection takes the same time whatever the prefix.
    if not secrets.compare_digest(supplied, expected.get_secret_value()):
        log.warning("internal.token_rejected", path=request.url.path)
        raise HTTPException(status_code=HTTP_404_NOT_FOUND)


router = APIRouter(
    prefix="/internal",
    tags=["internal"],
    dependencies=[Depends(require_internal_token)],
)


@router.post("/ping", summary="Defer the system ping task (non-production only).")
async def ping(procrastinate: ProcrastinateDep) -> dict[str, Any]:
    job_id = await procrastinate.configure_task(PING_TASK).defer_async()
    log.info("internal.ping.deferred", job_id=job_id, task_name=PING_TASK)
    return {"deferred": True, "job_id": job_id}


__all__ = ["INTERNAL_TOKEN_HEADER", "require_internal_token", "router"]
