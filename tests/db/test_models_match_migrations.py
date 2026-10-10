"""The ORM models and the hand-written migrations describe the same schema.

Migrations are written by hand (reviewable, deliberate), models by hand too (typed access),
and hand-writing the same thing twice is exactly where drift creeps in: a column that is
nullable in one and not the other passes every test that does not touch it. Autogenerate's
comparison against the migrated database is the arbiter.
"""

from __future__ import annotations

from typing import Any

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, pool

from personal_organizer.db import models  # noqa: F401  (registers every table)
from personal_organizer.db.base import Base
from personal_organizer.db.dsn import normalise
from personal_organizer.db.roles import DatabaseRole
from personal_organizer.settings import Settings

pytestmark = pytest.mark.db


def _include(_obj: object, name: str | None, _type: str, _reflected: bool, _other: object) -> bool:
    return not (name or "").startswith("procrastinate_") and name != "alembic_version"


def test_models_match_the_migrated_schema(db_settings: Settings, owner_conn: Any) -> None:
    """``owner_conn`` is taken only for its skip-without-a-database behaviour."""
    url, connect_args = normalise(db_settings.dsn_for(DatabaseRole.OWNER), "psycopg")
    engine = create_engine(url, connect_args=connect_args, poolclass=pool.NullPool)
    try:
        with engine.connect() as connection:
            context = MigrationContext.configure(
                connection,
                opts={
                    "compare_type": True,
                    "compare_server_default": True,
                    "include_object": _include,
                },
            )
            diff = compare_metadata(context, Base.metadata)
    finally:
        engine.dispose()
    assert diff == [], f"models and migrations disagree: {diff}"


async def test_tables_written_before_a_tenant_is_known_have_no_rls(app_conn: Any) -> None:
    """The channel ledgers and ``invites`` exist before their tenant does, so no RLS --
    which also means a stray policy on them would silently hide every row from the worker."""
    rows = await app_conn.fetch(
        "SELECT relname, relrowsecurity FROM pg_class "
        "WHERE relname IN ('channel_inbox', 'channel_outbox', 'invites')"
    )
    assert {row["relname"]: row["relrowsecurity"] for row in rows} == {
        "channel_inbox": False,
        "channel_outbox": False,
        "invites": False,
    }
