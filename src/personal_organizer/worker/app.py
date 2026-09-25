"""The Procrastinate application.

Connector choice is forced, not preferred: Procrastinate 3.10 ships no asyncpg connector
(only psycopg3, psycopg2 and aiopg). So this process runs two drivers -- asyncpg for
SQLAlchemy, psycopg3 for the queue.

That turns out to be desirable anyway: Procrastinate holds a dedicated long-lived connection
for ``LISTEN``, which is exactly what you do not want occupying a slot in the application
pool.
"""

from __future__ import annotations

import procrastinate

from personal_organizer.db.dsn import normalise
from personal_organizer.db.roles import DatabaseRole
from personal_organizer.settings import Settings
from personal_organizer.worker.tasks import REGISTRARS


def build_procrastinate_app(settings: Settings) -> procrastinate.App:
    """Build the Procrastinate app, connecting as the runtime role.

    ``app_user`` owns none of the ``procrastinate_*`` tables -- they are created by Alembic
    as ``app_owner`` -- but has DML on them via the default privileges bootstrap installs.
    ``LISTEN``/``NOTIFY`` need no grant at all.
    """
    conninfo, _ = normalise(settings.dsn_for(DatabaseRole.APP), "libpq")
    app = procrastinate.App(
        connector=procrastinate.PsycopgConnector(
            conninfo=conninfo,
            min_size=settings.worker.pool_min_size,
            max_size=settings.worker.pool_max_size,
        ),
    )
    for register in REGISTRARS:
        register(app)
    return app


__all__ = ["build_procrastinate_app"]
