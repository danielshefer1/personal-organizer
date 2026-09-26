"""Async engines and sessions, one per database role.

A class rather than module-level globals: the test suite constructs ``Database(settings)``
per session with no monkeypatching and no import-order hazards, and the api and worker each
own their instance explicitly.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from personal_organizer.core.types import TenantId
from personal_organizer.db.dsn import normalise
from personal_organizer.db.roles import DatabaseRole
from personal_organizer.db.session import TENANT_INFO_KEY
from personal_organizer.settings import Settings

log = structlog.get_logger(__name__)

READY_TIMEOUT_S = 2.0


class Database:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._engines: dict[DatabaseRole, AsyncEngine] = {}
        self._makers: dict[DatabaseRole, async_sessionmaker[AsyncSession]] = {}

    def engine(self, role: DatabaseRole = DatabaseRole.APP) -> AsyncEngine:
        if role not in self._engines:
            self._engines[role] = self._build_engine(role)
            self._makers[role] = async_sessionmaker(
                self._engines[role], expire_on_commit=False, autoflush=False
            )
        return self._engines[role]

    def _build_engine(self, role: DatabaseRole) -> AsyncEngine:
        db = self._settings.database
        url, connect_args = normalise(self._settings.dsn_for(role), "asyncpg")
        connect_args = {
            **connect_args,
            "timeout": db.connect_timeout,
            "server_settings": {
                "application_name": f"po-{self._settings.app.component}-{role.value}",
                "statement_timeout": str(db.statement_timeout_ms),
                # A leaked open transaction is a pooled connection stuck with a tenant GUC
                # still set, so this bound is part of the RLS design, not just hygiene.
                "idle_in_transaction_session_timeout": str(db.idle_in_transaction_timeout_ms),
                "jit": "off",
            },
        }
        return create_async_engine(
            url,
            connect_args=connect_args,
            pool_size=db.pool_size,
            max_overflow=db.max_overflow,
            pool_timeout=db.pool_timeout,
            pool_recycle=db.pool_recycle_seconds,
            pool_pre_ping=True,
            echo=db.echo,
        )

    def _maker(self, role: DatabaseRole) -> async_sessionmaker[AsyncSession]:
        self.engine(role)
        return self._makers[role]

    @asynccontextmanager
    async def tenant_session(
        self, tenant_id: TenantId, *, role: DatabaseRole = DatabaseRole.APP
    ) -> AsyncIterator[AsyncSession]:
        """An RLS-scoped session: every statement runs with ``app.tenant_id`` set."""
        maker = self._maker(role)
        async with maker(info={TENANT_INFO_KEY: tenant_id}) as session:
            structlog.contextvars.bind_contextvars(tenant_id=str(tenant_id))
            try:
                async with session.begin():
                    yield session
            finally:
                structlog.contextvars.unbind_contextvars("tenant_id")

    @asynccontextmanager
    async def system_session(
        self, *, role: DatabaseRole = DatabaseRole.APP
    ) -> AsyncIterator[AsyncSession]:
        """A session with no tenant GUC.

        Only for tables that are not tenant-scoped -- ``procrastinate_*`` and the webhook
        event log. Using this for a tenant table means RLS returns nothing, loudly.
        """
        maker = self._maker(role)
        async with maker() as session, session.begin():
            yield session

    async def check(self, *, role: DatabaseRole = DatabaseRole.APP) -> None:
        """Bounded connectivity probe for ``/ready``.

        Runs as the runtime role so it also proves ``app_user``'s credentials work, not
        merely that Postgres is up.
        """
        async with asyncio.timeout(READY_TIMEOUT_S), self.engine(role).connect() as conn:
            await conn.execute(text("SELECT 1"))

    async def dispose(self) -> None:
        for engine in self._engines.values():
            await engine.dispose()
        self._engines.clear()
        self._makers.clear()


_database: Database | None = None


def set_database(database: Database) -> None:
    """Register the process-wide instance (used by worker tasks, which get no DI)."""
    global _database
    _database = database


def get_database() -> Database:
    if _database is None:
        msg = "Database has not been initialised"
        raise RuntimeError(msg)
    return _database


__all__ = ["READY_TIMEOUT_S", "Database", "get_database", "set_database"]
