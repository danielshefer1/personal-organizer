"""Inbound message processing.

One job per stored inbound message, deferred by the ingress transaction with
``lock=<sender hash>`` so a sender's messages are handled one at a time, in arrival order.
The job carries the inbox row id and nothing else -- the body is read back from the
database, per docs/adr/0001.

Retries are Procrastinate's, and only for failures that are definitely-not-sent or a lost
database connection. One retry layer keeps every attempt visible in ``procrastinate_jobs``;
an in-task retry loop would hide them and hold a worker slot while it slept. The price is
ordering: a job waiting to retry keeps its lock, so the same sender's next message waits
too -- up to about a minute across the whole backoff.
"""

from __future__ import annotations

import functools
from typing import Final
from uuid import UUID

import procrastinate
import structlog
from sqlalchemy.exc import InterfaceError, OperationalError

from personal_organizer.core.errors import TransientChannelError
from personal_organizer.db.engine import get_database
from personal_organizer.messaging.inbound import acknowledge, handle_inbound
from personal_organizer.messaging.runtime import get_outbound_channel
from personal_organizer.settings import get_settings
from personal_organizer.worker.queues import Queue

log = structlog.get_logger(__name__)

HANDLE_INBOUND_TASK: Final = "channel:handle_inbound"

#: Waits of 2, 4, 8, 16 and 32 seconds between six attempts.
INBOUND_RETRY: Final = procrastinate.RetryStrategy(
    max_attempts=6,
    exponential_wait=2,
    retry_exceptions={TransientChannelError, OperationalError, InterfaceError},
)


def register(app: procrastinate.App) -> None:
    @app.task(queue=Queue.WEBHOOKS.value, name=HANDLE_INBOUND_TASK, retry=INBOUND_RETRY)
    async def handle_inbound_task(inbox_id: str) -> None:
        db = get_database()
        channel = get_outbound_channel()
        await handle_inbound(
            UUID(inbox_id),
            db=db,
            channel=channel,
            allowlist=get_settings().whatsapp.allowlist,
            on_allowed=functools.partial(acknowledge, db=db, channel=channel),
        )


__all__ = ["HANDLE_INBOUND_TASK", "INBOUND_RETRY", "register"]
