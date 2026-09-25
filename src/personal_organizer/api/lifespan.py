"""Application lifespan.

Resources are acquired in dependency order and released in reverse via an ``AsyncExitStack``.

The eager ``db.check()`` is deliberate: a bad DSN or a missing role then fails the deploy
immediately, rather than surfacing on the first real request minutes later.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, AsyncExitStack, asynccontextmanager

import structlog
from fastapi import FastAPI

from personal_organizer.db.engine import Database, set_database
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
            await database.check()
            set_database(database)
            app.state.db = database

            init_langfuse(settings)
            stack.callback(flush_langfuse)

            # Opened here, not in Iteration 02, so that adding the webhook router later is
            # a routing change rather than a plumbing change.
            procrastinate_app = build_procrastinate_app(settings)
            await stack.enter_async_context(procrastinate_app.open_async())
            app.state.procrastinate = procrastinate_app

            log.info("api.started", env=settings.app.env, release=settings.app.release)
            yield
            log.info("api.stopping")

    return lifespan


__all__ = ["make_lifespan"]
