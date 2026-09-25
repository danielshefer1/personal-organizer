"""Baseline.

Iteration 01 ships no domain tables: the walking skeleton proves deployment, migrations,
roles and observability, not the data model. Tenant tables and their RLS policies arrive in
Iteration 03.

This revision exists so the chain has a root and so `alembic upgrade head` is exercised
end to end by CI and by the Railway pre-deploy command from Day 1.
"""

from __future__ import annotations

from collections.abc import Sequence

revision: str = "0001_baseline"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
