"""Idempotent database bootstrap and post-deploy verification.

Uses asyncpg directly rather than SQLAlchemy: ``bootstrap.sql`` is a multi-statement script,
and asyncpg's simple query protocol (used when ``execute`` is called with no arguments)
runs those in one round trip. SQLAlchemy's asyncpg dialect prepares statements, which
rejects multi-statement scripts.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import asyncpg
import structlog

from personal_organizer.core.errors import BootstrapError
from personal_organizer.db.dsn import normalise
from personal_organizer.db.roles import DatabaseRole
from personal_organizer.settings import Settings

log = structlog.get_logger(__name__)

BOOTSTRAP_SQL = Path(__file__).resolve().parents[3] / "alembic" / "bootstrap.sql"

MIN_PGVECTOR = (0, 5, 0)


def _credentials(dsn: str) -> tuple[str, str]:
    parts = urlsplit(dsn)
    if not parts.username or not parts.password:
        msg = "DSN must carry a username and password so bootstrap can create the role"
        raise BootstrapError(msg)
    return parts.username, parts.password


async def _connect(settings: Settings, role: DatabaseRole) -> Any:
    url, connect_args = normalise(settings.dsn_for(role), "asyncpg")
    return await asyncpg.connect(
        url.replace("postgresql+asyncpg://", "postgresql://", 1),
        ssl=connect_args.get("ssl"),
        timeout=connect_args.get("timeout", settings.database.connect_timeout),
    )


async def run_bootstrap(settings: Settings, *, sql_path: Path | None = None) -> None:
    """Create the extension, roles, ownership and default privileges. Safe to re-run."""
    owner_role, owner_password = _credentials(settings.dsn_for(DatabaseRole.OWNER))
    app_role, app_password = _credentials(settings.dsn_for(DatabaseRole.APP))
    script = (sql_path or BOOTSTRAP_SQL).read_text()

    conn = await _connect(settings, DatabaseRole.BOOTSTRAP)
    try:
        async with conn.transaction():
            # Transaction-local GUCs, so the password never appears in a statement string
            # that could be logged by the server.
            for name, value in (
                ("po.owner_role", owner_role),
                ("po.owner_password", owner_password),
                ("po.app_role", app_role),
                ("po.app_password", app_password),
            ):
                await conn.execute("SELECT set_config($1, $2, true)", name, value)
            await conn.execute(script)
    finally:
        await conn.close()

    log.info("db.bootstrap.ok", owner_role=owner_role, app_role=app_role)


async def check_database(settings: Settings) -> list[str]:
    """Assert the end state bootstrap is supposed to produce. Returns a list of problems."""
    problems: list[str] = []
    conn = await _connect(settings, DatabaseRole.APP)
    try:
        version: str | None = await conn.fetchval(
            "SELECT extversion FROM pg_extension WHERE extname = 'vector'"
        )
        if version is None:
            problems.append("pgvector is not installed")
        elif tuple(int(p) for p in version.split(".")) < MIN_PGVECTOR:
            problems.append(f"pgvector {version} < 0.5, HNSW indexes unavailable")

        row = await conn.fetchrow(
            "SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user"
        )
        if row is None:
            problems.append("current_user has no pg_roles entry")
        else:
            if row["rolsuper"]:
                problems.append("runtime role is a superuser, which bypasses RLS entirely")
            if row["rolbypassrls"]:
                problems.append("runtime role has BYPASSRLS")

        owned: int = await conn.fetchval(
            "SELECT count(*) FROM pg_class c "
            "JOIN pg_roles r ON c.relowner = r.oid "
            "WHERE r.rolname = current_user AND c.relkind IN ('r', 'p')"
        )
        if owned:
            problems.append(f"runtime role owns {owned} table(s); it must own none")

        can_create: bool = await conn.fetchval(
            "SELECT has_schema_privilege(current_user, 'public', 'CREATE')"
        )
        if can_create:
            problems.append("runtime role can CREATE in schema public")
    finally:
        await conn.close()

    return problems


__all__ = ["BOOTSTRAP_SQL", "MIN_PGVECTOR", "check_database", "run_bootstrap"]
