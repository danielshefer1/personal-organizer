"""Alembic environment.

Runs on a **synchronous** psycopg engine rather than async. Migrations gain nothing from
asyncio, ``asyncio.run()`` inside ``env.py`` is a recurring source of event-loop bugs, and
multi-statement DDL is simpler on psycopg. The application stays async on asyncpg; the two
coexist because :func:`personal_organizer.db.dsn.normalise` selects the driver per consumer.

The connection is always the **owner** role. ``app_user`` owns nothing and cannot create in
``public``, so a migration accidentally run as the runtime role fails immediately rather
than creating objects with the wrong owner.
"""

from __future__ import annotations

from alembic import context
from sqlalchemy import create_engine, pool

from personal_organizer.db import models  # noqa: F401  (registers every table)
from personal_organizer.db.base import Base
from personal_organizer.db.dsn import normalise
from personal_organizer.db.roles import DatabaseRole
from personal_organizer.settings import get_settings

target_metadata = Base.metadata


def include_object(
    _obj: object, name: str | None, _type: str, _reflected: bool, _compare_to: object
) -> bool:
    """Keep Procrastinate's tables out of autogenerate.

    They are applied from vendored SQL in revision 0002 and are not described by our
    metadata, so without this every autogenerate would propose dropping them.
    """
    return not (name or "").startswith("procrastinate_")


def run_migrations_offline() -> None:
    url, _ = normalise(get_settings().dsn_for(DatabaseRole.OWNER), "psycopg")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        include_object=include_object,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    url, connect_args = normalise(get_settings().dsn_for(DatabaseRole.OWNER), "psycopg")
    engine = create_engine(url, connect_args=connect_args, poolclass=pool.NullPool)
    try:
        with engine.connect() as connection:
            context.configure(
                connection=connection,
                target_metadata=target_metadata,
                compare_type=True,
                compare_server_default=True,
                include_object=include_object,
                transaction_per_migration=True,
            )
            with context.begin_transaction():
                context.run_migrations()
    finally:
        engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
