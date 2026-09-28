"""Channel ledgers: ``channel_inbox`` and ``channel_outbox``.

The first domain tables, and deliberately not tenant tables: the tenant is not known at
ingress, so neither carries ``tenant_id`` and neither gets RLS. The RLS suite derives its
expectations from ``TenantMixin``, so it asserts exactly that. See docs/adr/0002.

Hand-written to match ``personal_organizer.db.models.channel``;
``tests/db/test_models_match_migrations.py`` fails if the two drift. ``app_user`` needs no
grant here -- the default privileges bootstrap installs cover tables ``app_owner`` creates.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003_channel_ledgers"
down_revision: str | None = "0002_procrastinate_schema"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TZ = sa.DateTime(timezone=True)
_GEN_UUID = sa.text("gen_random_uuid()")


def upgrade() -> None:
    op.create_table(
        "channel_inbox",
        sa.Column("id", sa.Uuid(), server_default=_GEN_UUID, nullable=False),
        sa.Column("channel", sa.Text(), nullable=False),
        sa.Column("provider_message_id", sa.Text(), nullable=False),
        sa.Column("sender_key", sa.Text(), nullable=False),
        sa.Column("sender_user_id", sa.Text(), nullable=True),
        sa.Column("sender_phone", sa.Text(), nullable=True),
        sa.Column("message_type", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), nullable=True),
        sa.Column("media_id", sa.Text(), nullable=True),
        sa.Column("media_mime_type", sa.Text(), nullable=True),
        sa.Column("reply_id", sa.Text(), nullable=True),
        sa.Column("context_message_id", sa.Text(), nullable=True),
        sa.Column("raw", postgresql.JSONB(), nullable=True),
        sa.Column("sent_at", _TZ, nullable=False),
        sa.Column("received_at", _TZ, server_default=sa.func.now(), nullable=False),
        sa.Column("processed_at", _TZ, nullable=True),
        sa.Column("disposition", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_channel_inbox"),
        sa.UniqueConstraint(
            "channel", "provider_message_id", name="uq_channel_inbox_channel_provider_message_id"
        ),
        sa.CheckConstraint(
            "sender_user_id IS NOT NULL OR sender_phone IS NOT NULL",
            name="ck_channel_inbox_sender",
        ),
        sa.CheckConstraint(
            "disposition IN ('allowed', 'stranger', 'stranger_muted', 'stale')",
            name="ck_channel_inbox_disposition",
        ),
    )
    op.create_index("ix_channel_inbox_sender_key", "channel_inbox", ["sender_key"])

    op.create_table(
        "channel_outbox",
        sa.Column("id", sa.Uuid(), server_default=_GEN_UUID, nullable=False),
        sa.Column("channel", sa.Text(), nullable=False),
        sa.Column("inbox_id", sa.Uuid(), nullable=True),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("recipient_key", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), server_default=sa.text("'pending'"), nullable=False),
        sa.Column("provider_message_id", sa.Text(), nullable=True),
        sa.Column("error_code", sa.Integer(), nullable=True),
        sa.Column("status_at", _TZ, nullable=True),
        sa.Column("created_at", _TZ, server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", _TZ, server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_channel_outbox"),
        sa.ForeignKeyConstraint(
            ["inbox_id"],
            ["channel_inbox.id"],
            name="fk_channel_outbox_inbox_id_channel_inbox",
            ondelete="SET NULL",
        ),
        sa.UniqueConstraint("inbox_id", "kind", name="uq_channel_outbox_inbox_id_kind"),
        sa.UniqueConstraint(
            "channel", "provider_message_id", name="uq_channel_outbox_channel_provider_message_id"
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'sending', 'unknown', 'accepted', 'sent', 'delivered', "
            "'read', 'failed')",
            name="ck_channel_outbox_status",
        ),
    )
    op.create_index(
        "ix_channel_outbox_recipient_key_kind_created_at",
        "channel_outbox",
        ["recipient_key", "kind", "created_at"],
    )


def downgrade() -> None:
    # Outbox first: it holds the foreign key into the inbox.
    op.drop_table("channel_outbox")
    op.drop_table("channel_inbox")
