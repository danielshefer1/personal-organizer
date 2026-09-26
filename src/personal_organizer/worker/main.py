"""``po-worker`` -- the Procrastinate worker entrypoint.

Deliberately not the ``procrastinate`` CLI: that would give unstructured stdlib logs and no
Sentry, whereas this initialises logging, Sentry and settings exactly as the api does. Since
:func:`configure_logging` installs the redaction chain on the root logger, Procrastinate's
own logging -- which records job kwargs, and from Iteration 02 those carry message bodies --
is redacted too.
"""

from __future__ import annotations

import asyncio
import sys
from contextlib import AsyncExitStack

import structlog

from personal_organizer.db.engine import Database, set_database
from personal_organizer.observability.langfuse import flush_langfuse, init_langfuse
from personal_organizer.observability.logging import configure_logging
from personal_organizer.observability.sentry import init_sentry
from personal_organizer.settings import Settings, get_settings
from personal_organizer.worker.app import build_procrastinate_app

log = structlog.get_logger(__name__)


async def run(settings: Settings) -> None:
    async with AsyncExitStack() as stack:
        database = Database(settings)
        stack.push_async_callback(database.dispose)
        await database.wait_ready()
        # Tasks get no dependency injection, so the instance is registered process-wide.
        set_database(database)

        init_langfuse(settings)
        stack.callback(flush_langfuse)

        app = build_procrastinate_app(settings)
        await stack.enter_async_context(app.open_async())

        log.info(
            "worker.started",
            env=settings.app.env,
            release=settings.app.release,
        )
        await app.run_worker_async(
            queues=settings.worker.queues,
            concurrency=settings.worker.concurrency,
            install_signal_handlers=True,
            shutdown_graceful_timeout=settings.worker.shutdown_graceful_timeout_s,
        )


def main() -> int:
    settings = get_settings()
    configure_logging(settings)
    init_sentry(settings)
    try:
        asyncio.run(run(settings))
    except KeyboardInterrupt:  # pragma: no cover
        log.info("worker.interrupted")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
