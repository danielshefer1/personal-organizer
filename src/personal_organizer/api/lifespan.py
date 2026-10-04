"""Application lifespan.

Resources are acquired in dependency order and released in reverse via an ``AsyncExitStack``.

The eager ``db.wait_ready()`` is deliberate: a bad DSN or a missing role then fails the
deploy immediately, rather than surfacing on the first real request minutes later. It waits
rather than probing once, because the platform's private network needs a moment at container
start and a single attempt makes that indistinguishable from a database that is not there.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, AsyncExitStack, asynccontextmanager

import structlog
from fastapi import FastAPI

from personal_organizer.db.engine import Database, set_database
from personal_organizer.messaging.ingress import PgIngressStore
from personal_organizer.observability.langfuse import flush_langfuse, init_langfuse
from personal_organizer.settings import Settings
from personal_organizer.worker.app import build_procrastinate_app

log = structlog.get_logger(__name__)


def make_lifespan(
    settings: Settings,
) -> Callable[[FastAPI], AbstractAsyncContextManager[None]]:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with AsyncExitStack() as stack:
            database = Database(settings)
            stack.push_async_callback(database.dispose)
            await database.wait_ready()
            set_database(database)
            app.state.db = database

            init_langfuse(settings)
            stack.callback(flush_langfuse)

            # The api defers jobs as well as the worker running them: /internal/ping, and every
            # inbound webhook message.
            procrastinate_app = build_procrastinate_app(settings)
            await stack.enter_async_context(procrastinate_app.open_async())
            app.state.procrastinate = procrastinate_app
            # Borrows the queue's psycopg pool, so the inbox insert and the job it defers
            # commit in one transaction. See docs/adr/0002.
            app.state.ingress_store = PgIngressStore(
                procrastinate_app, pepper=settings.logging.pii_pepper.get_secret_value()
            )

            log.info(
                "api.started",
                env=settings.app.env,
                release=settings.app.release,
                whatsapp_enabled=settings.whatsapp.enabled,
                gowa_enabled=settings.gowa.enabled,
            )
            yield
            log.info("api.stopping")

    return lifespan


__all__ = ["make_lifespan"]
