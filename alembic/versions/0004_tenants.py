"""Tenants under RLS, the tenant-resolution door, and the outbox idempotency key.

Iteration 03 (docs/plan-iteration-03.md, "Schema"):

* Five tenant tables -- ``tenants``, ``tenant_identities``, ``calendar_connections``,
  ``onboarding_links``, ``messages`` -- each with RLS enabled, forced, and the one
  ``tenant_isolation`` policy from ``alembic/rls.py`` (D3). ``tenants`` is keyed on ``id``.
* ``resolve_tenant`` and ``create_tenant``: ``SECURITY DEFINER``, owned by ``app_definer``
  (``NOLOGIN BYPASSRLS``, created by ``bootstrap.sql``), callable by ``app_user`` and nobody
  else (D1). They are the only way to reach a tenant row without that tenant's GUC, and they
  return an id and nothing more.
* ``channel_outbox.idempotency_key`` (D6) and the ``onboarding`` disposition.

Three PostgreSQL details shape the function DDL:

1. A new function is executable by ``PUBLIC``. ``REVOKE ... FROM PUBLIC`` is what makes
   "callable by app_user only" true; app_user's own EXECUTE comes from the default
   privileges bootstrap installs for objects app_owner creates.
2. Privileges are set *before* the owner changes. ``ALTER ... OWNER TO`` carries the ACL
   across, rewriting the grantor, so the order leaves nothing to re-grant as the new owner.
3. On PostgreSQL 16 ``ALTER FUNCTION ... OWNER TO app_definer`` needs app_owner to be able
   to ``SET ROLE app_definer`` and app_definer to hold ``CREATE`` on ``public``. Bootstrap
   grants both; this revision fails loudly on a database bootstrapped before Iteration 03.

``app_definer`` gets ``SELECT, INSERT`` on the two tables the functions touch: the default
privileges cover app_user only. Hand-written to match ``personal_organizer.db.models``;
``tests/db/test_models_match_migrations.py`` fails if the two drift.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from rls import disable_tenant_rls, enable_tenant_rls

from personal_organizer.db.roles import DEFINER_ROLE_NAME

revision: str = "0004_tenants"
down_revision: str | None = "0003_channel_ledgers"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TZ = sa.DateTime(timezone=True)
_GEN_UUID = sa.text("gen_random_uuid()")

#: Child tables first, so the downgrade can drop in this order.
_CHILD_TABLES = ("messages", "onboarding_links", "calendar_connections", "tenant_identities")

_DISPOSITIONS_0003 = "disposition IN ('allowed', 'stranger', 'stranger_muted', 'stale')"
_DISPOSITIONS_0004 = (
    "disposition IN ('allowed', 'stranger', 'stranger_muted', 'stale', 'onboarding')"
)

_RESOLVE_TENANT = """
CREATE FUNCTION resolve_tenant(p_network text, p_external_id text)
RETURNS uuid
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
    SELECT tenant_id FROM tenant_identities
    WHERE network = p_network AND external_id = p_external_id
$$
"""

# The fast path answers a known sender without a subtransaction. The slow path inserts the
# tenant and its identity inside one exception block, so that losing a race to a concurrent
# call for the same identity rolls back *both* rows -- no orphan tenant -- and the
# winner's id is read once its row is committed (READ COMMITTED: each statement sees it).
_CREATE_TENANT = """
CREATE FUNCTION create_tenant(
    p_network text, p_external_id text, p_phone text, p_language text
)
RETURNS uuid
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
    v_tenant uuid;
BEGIN
    SELECT tenant_id INTO v_tenant FROM tenant_identities
    WHERE network = p_network AND external_id = p_external_id;
    IF FOUND THEN
        RETURN v_tenant;
    END IF;
    BEGIN
        INSERT INTO tenants (language) VALUES (p_language) RETURNING id INTO v_tenant;
        INSERT INTO tenant_identities (tenant_id, network, external_id, phone)
        VALUES (v_tenant, p_network, p_external_id, p_phone);
        RETURN v_tenant;
    EXCEPTION WHEN unique_violation THEN
        SELECT tenant_id INTO v_tenant FROM tenant_identities
        WHERE network = p_network AND external_id = p_external_id;
        -- Never hand back NULL. Under READ COMMITTED the winner's committed row is always
        -- visible here, so this guard is unreachable and no test exercises it; it exists for
        -- REPEATABLE READ/SERIALIZABLE callers, or a winner deleted in between, who would
        -- otherwise get a NULL tenant id that silently sets no GUC downstream.
        IF v_tenant IS NULL THEN
            RAISE EXCEPTION 'create_tenant: identity vanished'
                USING ERRCODE = 'serialization_failure';
        END IF;
        RETURN v_tenant;
    END;
END
$$
"""

_FUNCTIONS = (
    ("resolve_tenant(text, text)", _RESOLVE_TENANT),
    ("create_tenant(text, text, text, text)", _CREATE_TENANT),
)


def _create_tables() -> None:
    op.create_table(
        "tenants",
        sa.Column("id", sa.Uuid(), server_default=_GEN_UUID, nullable=False),
        sa.Column("status", sa.Text(), server_default=sa.text("'onboarding'"), nullable=False),
        sa.Column("onboarding_step", sa.Text(), server_default=sa.text("'zone'"), nullable=True),
        sa.Column("language", sa.Text(), nullable=False),
        sa.Column("timezone", sa.Text(), nullable=True),
        sa.Column("created_at", _TZ, server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", _TZ, server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_tenants"),
        sa.CheckConstraint(
            "status IN ('onboarding', 'active', 'suspended')", name="ck_tenants_status"
        ),
        sa.CheckConstraint(
            "onboarding_step IN ('zone', 'connect')", name="ck_tenants_onboarding_step"
        ),
        sa.CheckConstraint("language IN ('he', 'en')", name="ck_tenants_language"),
    )

    op.create_table(
        "tenant_identities",
        sa.Column("id", sa.Uuid(), server_default=_GEN_UUID, nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("network", sa.Text(), nullable=False),
        sa.Column("external_id", sa.Text(), nullable=False),
        sa.Column("phone", sa.Text(), nullable=True),
        sa.Column("created_at", _TZ, server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_tenant_identities"),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_tenant_identities_tenant_id_tenants",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "network", "external_id", name="uq_tenant_identities_network_external_id"
        ),
    )
    op.create_index("ix_tenant_identities_tenant_id", "tenant_identities", ["tenant_id"])

    op.create_table(
        "calendar_connections",
        sa.Column("id", sa.Uuid(), server_default=_GEN_UUID, nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("composio_user_id", sa.Text(), nullable=False),
        sa.Column("connected_account_id", sa.Text(), nullable=False),
        sa.Column("auth_config_id", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), server_default=sa.text("'active'"), nullable=False),
        sa.Column("connected_at", _TZ, server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_calendar_connections"),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_calendar_connections_tenant_id_tenants",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "connected_account_id", name="uq_calendar_connections_connected_account_id"
        ),
        sa.CheckConstraint(
            "status IN ('active', 'revoked', 'failed')", name="ck_calendar_connections_status"
        ),
    )
    op.create_index("ix_calendar_connections_tenant_id", "calendar_connections", ["tenant_id"])
    op.create_index(
        "uq_calendar_connections_tenant_id_active",
        "calendar_connections",
        ["tenant_id"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )

    op.create_table(
        "onboarding_links",
        sa.Column("id", sa.Uuid(), server_default=_GEN_UUID, nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("nonce", sa.Text(), nullable=False),
        sa.Column("expires_at", _TZ, nullable=False),
        sa.Column("used_at", _TZ, nullable=True),
        sa.Column("created_at", _TZ, server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_onboarding_links"),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_onboarding_links_tenant_id_tenants",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("nonce", name="uq_onboarding_links_nonce"),
    )
    op.create_index("ix_onboarding_links_tenant_id", "onboarding_links", ["tenant_id"])

    op.create_table(
        "messages",
        sa.Column("id", sa.Uuid(), server_default=_GEN_UUID, nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("direction", sa.Text(), nullable=False),
        sa.Column("channel", sa.Text(), nullable=False),
        sa.Column("inbox_id", sa.Uuid(), nullable=True),
        sa.Column("message_type", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), nullable=True),
        sa.Column("sent_at", _TZ, nullable=False),
        sa.Column("created_at", _TZ, server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_messages"),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_messages_tenant_id_tenants",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["inbox_id"],
            ["channel_inbox.id"],
            name="fk_messages_inbox_id_channel_inbox",
            ondelete="SET NULL",
        ),
        sa.CheckConstraint("direction IN ('in', 'out')", name="ck_messages_direction"),
    )
    op.create_index("ix_messages_tenant_id", "messages", ["tenant_id"])


def _create_functions() -> None:
    for signature, ddl in _FUNCTIONS:
        op.execute(ddl)
        op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC")
        op.execute(f'ALTER FUNCTION {signature} OWNER TO "{DEFINER_ROLE_NAME}"')


def upgrade() -> None:
    _create_tables()

    enable_tenant_rls("tenants", column="id")
    for table in _CHILD_TABLES:
        enable_tenant_rls(table)

    op.execute(f'GRANT SELECT, INSERT ON tenants, tenant_identities TO "{DEFINER_ROLE_NAME}"')
    _create_functions()

    op.add_column("channel_outbox", sa.Column("idempotency_key", sa.Text(), nullable=True))
    op.create_unique_constraint(
        "uq_channel_outbox_idempotency_key", "channel_outbox", ["idempotency_key"]
    )

    op.drop_constraint("ck_channel_inbox_disposition", "channel_inbox", type_="check")
    op.create_check_constraint("ck_channel_inbox_disposition", "channel_inbox", _DISPOSITIONS_0004)


def downgrade() -> None:
    # 0003's check does not know 'onboarding'. Those senders were invited, which is what
    # 'allowed' meant before tenants existed.
    op.execute("UPDATE channel_inbox SET disposition = 'allowed' WHERE disposition = 'onboarding'")
    op.drop_constraint("ck_channel_inbox_disposition", "channel_inbox", type_="check")
    op.create_check_constraint("ck_channel_inbox_disposition", "channel_inbox", _DISPOSITIONS_0003)

    op.drop_constraint("uq_channel_outbox_idempotency_key", "channel_outbox", type_="unique")
    op.drop_column("channel_outbox", "idempotency_key")

    # app_owner may drop them: it owns schema public, and it is a member of app_definer.
    for signature, _ in reversed(_FUNCTIONS):
        op.execute(f"DROP FUNCTION {signature}")

    for table in _CHILD_TABLES:
        disable_tenant_rls(table)
        op.drop_table(table)
    disable_tenant_rls("tenants")
    op.drop_table("tenants")
