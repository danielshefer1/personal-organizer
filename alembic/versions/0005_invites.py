"""Invites in the database: who may start onboarding (ADR 0006).

``invites`` is not a tenant table, for the reason ``channel_inbox`` is not: an invite exists
before its tenant does. No RLS, no ``TenantMixin``; the RLS suite derives its expectations
from the mixins, so it asserts exactly that.

Bootstrap's default privileges give ``app_user`` ``SELECT, INSERT, UPDATE, DELETE`` on every
table ``app_owner`` creates. Invites are history, so ``DELETE`` is taken back. The runtime
role's name comes from its DSN, not a constant, so the revoke is from whichever role holds
the privilege other than the owner.

Hand-written to match ``personal_organizer.db.models.invite``;
``tests/db/test_models_match_migrations.py`` fails if the two drift.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005_invites"
down_revision: str | None = "0004_tenants"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TZ = sa.DateTime(timezone=True)
_GEN_UUID = sa.text("gen_random_uuid()")

_REVOKE_DELETE = """
DO $$
DECLARE
    grantee_name text;
BEGIN
    FOR grantee_name IN
        SELECT DISTINCT r.rolname
        FROM pg_class c, aclexplode(c.relacl) a
        JOIN pg_roles r ON r.oid = a.grantee
        WHERE c.oid = 'public.invites'::regclass
          AND a.privilege_type = 'DELETE'
          AND r.rolname <> current_user
    LOOP
        EXECUTE format('REVOKE DELETE ON invites FROM %I', grantee_name);
    END LOOP;
END
$$
"""


def upgrade() -> None:
    op.create_table(
        "invites",
        sa.Column("id", sa.Uuid(), server_default=_GEN_UUID, nullable=False),
        sa.Column("phone", sa.Text(), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("invited_by_tenant_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", _TZ, server_default=sa.func.now(), nullable=False),
        sa.Column("used_at", _TZ, nullable=True),
        sa.Column("revoked_at", _TZ, nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_invites"),
        sa.ForeignKeyConstraint(
            ["invited_by_tenant_id"],
            ["tenants.id"],
            name="fk_invites_invited_by_tenant_id_tenants",
            ondelete="SET NULL",
        ),
        sa.CheckConstraint(
            "used_at IS NULL OR revoked_at IS NULL", name="ck_invites_used_or_revoked"
        ),
    )
    op.create_index(
        "uq_invites_open_phone",
        "invites",
        ["phone"],
        unique=True,
        postgresql_where=sa.text("used_at IS NULL AND revoked_at IS NULL"),
    )
    op.execute(_REVOKE_DELETE)


def downgrade() -> None:
    op.drop_index("uq_invites_open_phone", table_name="invites")
    op.drop_table("invites")
