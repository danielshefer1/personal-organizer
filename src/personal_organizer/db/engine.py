"""Async engines and sessions, one per database role.

A class rather than module-level globals: the test suite constructs ``Database(settings)``
per session with no monkeypatching and no import-order hazards, and the api and worker each
own their instance explicitly.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Final

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from personal_organizer.core.types import TenantId
from personal_organizer.db.dsn import describe, normalise
from personal_organizer.db.roles import DatabaseRole
from personal_organizer.db.session import TENANT_INFO_KEY
from personal_organizer.settings import Settings

log = structlog.get_logger(__name__)

READY_TIMEOUT_S = 2.0

_RETRY_INITIAL_S: Final = 0.5
_RETRY_MAX_S: Final = 4.0

#: SQLSTATEs that no amount of waiting fixes: the password, the role or the database is
#: wrong. :meth:`Database.wait_ready` raises these on the first attempt, which is the
#: fail-fast the eager startup check exists for.
_FATAL_SQLSTATES: Final = frozenset(
    {
        "28P01",  # invalid_password
        "28000",  # invalid_authorization_specification -- includes a role that is absent
        "3D000",  # invalid_catalog_name -- no such database
        "42501",  # insufficient_privilege
    }
)


def _fatal_sqlstate(exc: BaseException) -> str | None:
    """The SQLSTATE of a configuration error anywhere in ``exc``'s chain.

    Walks the chain rather than inspecting ``exc`` itself: SQLAlchemy wraps the asyncpg
    error in a ``DBAPIError`` (under ``orig``) and asyncpg chains its own beneath that, so
    the exception actually raised is almost never the one carrying the SQLSTATE.
    """
    seen: set[int] = set()
    queue: list[BaseException] = [exc]
    while queue:
        current = queue.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        sqlstate = getattr(current, "sqlstate", None)
        if isinstance(sqlstate, str) and sqlstate in _FATAL_SQLSTATES:
            return sqlstate
        for nested in (current.__cause__, current.__context__, getattr(current, "orig", None)):
            if isinstance(nested, BaseException):
                queue.append(nested)
    return None


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

    async def wait_ready(
        self,
        *,
        role: DatabaseRole = DatabaseRole.APP,
        budget: float | None = None,
        initial_backoff: float = _RETRY_INITIAL_S,
    ) -> None:
        """Block until the database answers, or give up after ``budget`` seconds.

        ``budget`` bounds the retrying, not any one attempt: each probe is still capped at
        :data:`READY_TIMEOUT_S` by :meth:`check`.

        :meth:`check` is the right probe for ``/ready``, where a bounded answer matters
        more than a patient one. Startup is the opposite case, and sharing one two-second
        attempt between them is what turned a cold start into a crash loop on Railway,
        whose private network is not routable for the first seconds of a container's life:
        the probe timed out, the process exited, the platform started another container,
        and that one was cold too. Nothing recovers from that on its own, and the traceback
        blames a connection timeout rather than a check that was too eager to wait.

        Wrong credentials, an absent role and an absent database are still raised on the
        first attempt. Retrying those only delays the report, and reporting them
        immediately is what the eager check was for.
        """
        seconds = self._settings.database.startup_timeout if budget is None else budget
        target = describe(self._settings.dsn_for(role))
        where = {
            "db_role": role.value,
            "db_host": target.host,
            "db_port": target.port,
            "db_name": target.database,
        }
        loop = asyncio.get_running_loop()
        deadline = loop.time() + seconds
        started = loop.time()
        backoff = initial_backoff
        attempt = 0

        while True:
            attempt += 1
            try:
                await self.check(role=role)
            except Exception as exc:
                elapsed_ms = int((loop.time() - started) * 1000)
                if (sqlstate := _fatal_sqlstate(exc)) is not None:
                    log.error(
                        "db.startup_rejected",
                        **where,
                        attempt=attempt,
                        error_type=type(exc).__name__,
                        error_code=sqlstate,
                    )
                    raise
                if (remaining := deadline - loop.time()) <= 0:
                    log.error(
                        "db.startup_timeout",
                        **where,
                        attempt=attempt,
                        duration_ms=elapsed_ms,
                        error_type=type(exc).__name__,
                    )
                    raise
                log.warning(
                    "db.startup_retry",
                    **where,
                    attempt=attempt,
                    duration_ms=elapsed_ms,
                    error_type=type(exc).__name__,
                )
                await asyncio.sleep(min(backoff, remaining))
                backoff = min(backoff * 2, _RETRY_MAX_S)
            else:
                if attempt > 1:
                    log.info(
                        "db.ready",
                        **where,
                        attempt=attempt,
                        duration_ms=int((loop.time() - started) * 1000),
                    )
                return

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
