"""Development-only endpoints.

``/internal/ping`` defers the system ping task, which is how the walking skeleton is proved
end to end: api -> queue -> worker -> database. The router is not mounted in production.
"""

from __future__ import annotations

from typing import Any

import structlog
from fastapi import APIRouter

from personal_organizer.api.deps import ProcrastinateDep
from personal_organizer.worker.tasks.system import PING_TASK

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/internal", tags=["internal"])


@router.post("/ping", summary="Defer the system ping task (non-production only).")
async def ping(procrastinate: ProcrastinateDep) -> dict[str, Any]:
    job_id = await procrastinate.configure_task(PING_TASK).defer_async()
    log.info("internal.ping.deferred", job_id=job_id, task_name=PING_TASK)
    return {"deferred": True, "job_id": job_id}


__all__ = ["router"]
