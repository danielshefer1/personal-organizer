"""Inbound message processing.

One job per stored inbound message, deferred by the ingress transaction with
``lock=<sender hash>`` so a sender's messages are handled one at a time, in arrival order.
The job carries the inbox row id and nothing else -- the body is read back from the
database, per docs/adr/0001.

The task is registered here so that ingress can defer it by name with
``allow_unknown=False``. Its processing -- the allowlist gate and the reply -- is the next
change; until then a job only records that it ran.
"""

from __future__ import annotations

from typing import Final

import procrastinate
import structlog

from personal_organizer.worker.queues import Queue

log = structlog.get_logger(__name__)

HANDLE_INBOUND_TASK: Final = "channel:handle_inbound"


def register(app: procrastinate.App) -> None:
    @app.task(queue=Queue.WEBHOOKS.value, name=HANDLE_INBOUND_TASK)
    async def handle_inbound(inbox_id: str) -> None:
        log.info("inbound.received", inbox_id=inbox_id)


__all__ = ["HANDLE_INBOUND_TASK", "register"]
