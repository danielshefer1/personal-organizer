"""Migration chain health."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory

pytestmark = pytest.mark.db

ROOT = Path(__file__).resolve().parents[2]


def _scripts() -> ScriptDirectory:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    return ScriptDirectory.from_config(config)


def test_there_is_exactly_one_head() -> None:
    """A second head means two migrations were written in parallel and never merged;
    `alembic upgrade head` would then fail in pre-deploy, taking the deploy with it."""
    assert len(_scripts().get_heads()) == 1


async def test_migrations_have_been_applied(app_conn: Any) -> None:
    applied = await app_conn.fetchval("SELECT version_num FROM alembic_version")
    assert applied == _scripts().get_current_head()


async def test_procrastinate_tables_exist(app_conn: Any) -> None:
    exists = await app_conn.fetchval("SELECT to_regclass('public.procrastinate_jobs') IS NOT NULL")
    assert exists is True


async def test_procrastinate_tables_are_owned_by_the_owner_role(app_conn: Any) -> None:
    owner = await app_conn.fetchval(
        "SELECT r.rolname FROM pg_class c JOIN pg_roles r ON c.relowner = r.oid "
        "WHERE c.relname = 'procrastinate_jobs'"
    )
    assert owner == "app_owner"
