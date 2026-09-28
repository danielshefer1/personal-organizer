"""Database fixtures.

These tests need a real PostgreSQL 16 with pgvector. They run in CI against a service
container and locally against `docker compose up -d`; without one they skip rather than
fail, so `uv run pytest` stays useful before the local database is up.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import asyncpg
import pytest
from pydantic import ValidationError

from personal_organizer.db.dsn import normalise
from personal_organizer.db.roles import DatabaseRole
from personal_organizer.settings import Settings


def _real_settings() -> Settings:
    try:
        return Settings()  # from the real environment, not the synthetic fixture env
    except ValidationError as exc:  # pragma: no cover - configuration-dependent
        pytest.skip(f"database settings not configured: {exc.error_count()} problems")


@pytest.fixture(scope="session")
def db_settings() -> Settings:
    return _real_settings()


async def _connect(settings: Settings, role: DatabaseRole) -> Any:
    url, connect_args = normalise(settings.dsn_for(role), "asyncpg")
    return await asyncpg.connect(
        url.replace("postgresql+asyncpg://", "postgresql://", 1),
        ssl=connect_args.get("ssl"),
        timeout=5,
    )


@pytest.fixture
async def app_conn(db_settings: Settings) -> AsyncIterator[Any]:
    """A connection as `app_user` -- the role RLS actually applies to.

    RLS assertions must never run as the owner: under FORCE ROW LEVEL SECURITY the owner
    behaves differently, and under plain RLS it would bypass policies entirely, so an
    owner-run isolation test proves nothing.
    """
    try:
        conn = await _connect(db_settings, DatabaseRole.APP)
    except (OSError, asyncpg.PostgresError) as exc:
        pytest.skip(f"no database available: {type(exc).__name__}")
    try:
        yield conn
    finally:
        await conn.close()


@pytest.fixture
async def owner_conn(db_settings: Settings) -> AsyncIterator[Any]:
    try:
        conn = await _connect(db_settings, DatabaseRole.OWNER)
    except (OSError, asyncpg.PostgresError) as exc:
        pytest.skip(f"no database available: {type(exc).__name__}")
    try:
        yield conn
    finally:
        await conn.close()


#: Everything a messaging test writes. The queue is included because ingress defers into it.
_CHANNEL_TABLES = "channel_outbox, channel_inbox, procrastinate_jobs"


@pytest.fixture
async def clean_channel_tables(owner_conn: Any) -> AsyncIterator[None]:
    """Empty the messaging ledgers and the queue before and after a test.

    Opt-in rather than autouse: most DB tests touch no data. TRUNCATE is also why these
    tests must never point at a deployed database -- see docker-compose.yml.
    """
    await owner_conn.execute(f"TRUNCATE {_CHANNEL_TABLES} CASCADE")
    try:
        yield
    finally:
        await owner_conn.execute(f"TRUNCATE {_CHANNEL_TABLES} CASCADE")
