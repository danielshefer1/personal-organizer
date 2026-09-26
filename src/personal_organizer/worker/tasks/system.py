"""System tasks.

``ping`` is the walking skeleton's proof of life: the api defers it, the worker picks it up
and touches Postgres. That round trip exercises api -> queue -> worker -> database, which is
the point of Iteration 01.

Tasks are registered through a function rather than a module-level ``Blueprint`` because
``App.add_tasks_from`` mutates the blueprint in place, prefixing each task name with the
namespace. Registering the same blueprint with a second App therefore yields
``system:system:ping``. A registration function keeps no shared mutable state, so building
an app twice in one process (tests, a future management command) is safe.

Task arguments carry identifiers only, never user content -- see docs/adr/0001.
"""

from __future__ import annotations

import procrastinate
import structlog
from sqlalchemy import text

from personal_organizer.db.engine import get_database
from personal_organizer.worker.queues import Queue

log = structlog.get_logger(__name__)

PING_TASK = "system:ping"


def register(app: procrastinate.App) -> None:
    @app.task(queue=Queue.MAINTENANCE.value, name=PING_TASK, pass_context=True)
    async def ping(context: procrastinate.JobContext) -> None:
        """Touch the database and log. Idempotent, so it is safe as a periodic task."""
        async with get_database().system_session() as session:
            await session.execute(text("SELECT 1"))
        log.info("ping.ok", job_id=context.job.id, queue=Queue.MAINTENANCE.value)


__all__ = ["PING_TASK", "register"]
