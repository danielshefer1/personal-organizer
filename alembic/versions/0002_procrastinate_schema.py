"""Procrastinate schema.

The spec leaves this open: migrations run as the owner role, the worker connects as
`app_user`, and nothing may run DDL at app startup. Three options were possible --
`procrastinate schema --apply` in pre-deploy, vendoring its SQL into this chain, or letting
Procrastinate manage itself as the runtime role. The third violates "no table ownership".
The first introduces a second migration mechanism with its own version state and no ordering
guarantee against Alembic.

So: vendored. One chain, one lock, one ordering, objects owned by `app_owner`, and grants
handled by the ALTER DEFAULT PRIVILEGES that bootstrap installs.

The cost is that a Procrastinate upgrade needs the SQL re-vendored by hand. That cost is
made safe by tests/worker/test_procrastinate_schema.py, which fails CI if the installed
package stops matching the vendored copy.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision: str = "0002_procrastinate_schema"
down_revision: str | None = "0001_baseline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

VENDORED_SQL = Path(__file__).resolve().parents[1] / "vendor" / "procrastinate_3.10.sql"


def upgrade() -> None:
    op.execute(sa.text(VENDORED_SQL.read_text()))

    # Belt and braces. ALTER DEFAULT PRIVILEGES in bootstrap already covers objects created
    # after it ran, but an environment bootstrapped before that statement existed would not
    # be covered -- and a worker that cannot touch its own queue fails in a confusing way.
    for statement in (
        "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO app_user",
        "GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO app_user",
        "GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA public TO app_user",
    ):
        op.execute(statement)


def downgrade() -> None:
    op.execute("DROP SCHEMA IF EXISTS procrastinate CASCADE")
    for table in (
        "procrastinate_periodic_defers",
        "procrastinate_events",
        "procrastinate_jobs",
    ):
        op.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
