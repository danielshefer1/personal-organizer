"""Invites: who may start onboarding (ADR 0006).

Not a tenant table, deliberately: an invite exists before its tenant does, so like
``channel_inbox`` it has no ``TenantMixin``, no RLS, and is reached through
``system_session``. ``app_user`` may read, add and update rows but never delete them
(migration 0005): a used or revoked invite is the history ``po-admin list`` shows.

``WHATSAPP__ALLOWED_PHONES`` stays the fallback invite list beside this table, so the owner's
own number is invited even with the table empty (``messaging.invites``).
"""

from __future__ import annotations

from datetime import datetime
from typing import Final
from uuid import UUID

from sqlalchemy import CheckConstraint, ForeignKey, Index, Text, func, text
from sqlalchemy.orm import Mapped, mapped_column

from personal_organizer.db.base import Base

#: An invite nobody has used or revoked yet. At most one per number (the partial index).
OPEN_INVITE: Final = "used_at IS NULL AND revoked_at IS NULL"

_GEN_UUID = text("gen_random_uuid()")


class Invite(Base):
    __tablename__ = "invites"
    __table_args__ = (
        CheckConstraint("used_at IS NULL OR revoked_at IS NULL", name="used_or_revoked"),
        Index("uq_invites_open_phone", "phone", unique=True, postgresql_where=text(OPEN_INVITE)),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=_GEN_UUID)
    #: E.164, normalised by the caller with ``core.phone.normalise_e164``.
    phone: Mapped[str] = mapped_column(Text)
    #: The owner's label, e.g. "Mom". Shown by ``po-admin list``, never logged.
    note: Mapped[str | None] = mapped_column(Text)
    #: ``NULL``: the owner, from ``po-admin``. Invitations from chat will fill it.
    invited_by_tenant_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("tenants.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    #: Set by ``enrol``, in the transaction that creates their tenant.
    used_at: Mapped[datetime | None]
    revoked_at: Mapped[datetime | None]


__all__ = ["OPEN_INVITE", "Invite"]
