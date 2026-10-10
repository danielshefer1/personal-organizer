"""Invites: every statement on the ``invites`` table (ADR 0006).

Not a tenant table, so nothing here goes through ``scoped()``; callers use
``Database.system_session``. Every function takes the caller's session, so a change commits
with the caller's transaction: ``enrol`` marks an invite used in the one that creates the
tenant.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import and_, exists, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from personal_organizer.db.models.invite import Invite

_OPEN = and_(Invite.used_at.is_(None), Invite.revoked_at.is_(None))


async def has_open_invite(session: AsyncSession, phone: str) -> bool:
    found = await session.scalar(select(exists().where(Invite.phone == phone, _OPEN)))
    return bool(found)


async def has_used_invite(session: AsyncSession, phone: str) -> bool:
    found = await session.scalar(
        select(exists().where(Invite.phone == phone, Invite.used_at.is_not(None)))
    )
    return bool(found)


async def create_invite(
    session: AsyncSession,
    phone: str,
    *,
    note: str | None = None,
    invited_by_tenant_id: UUID | None = None,
) -> bool:
    """Open an invite for ``phone``. ``False`` if one is already open: the partial unique
    index decides, so two concurrent calls leave one invite and raise nothing."""
    created = await session.scalar(
        insert(Invite)
        .values(phone=phone, note=note, invited_by_tenant_id=invited_by_tenant_id)
        .on_conflict_do_nothing(index_elements=[Invite.phone], index_where=_OPEN)
        .returning(Invite.id)
    )
    return created is not None


async def _close_open(session: AsyncSession, phone: str, **values: object) -> bool:
    closed = await session.scalar(
        update(Invite).where(Invite.phone == phone, _OPEN).values(**values).returning(Invite.id)
    )
    return closed is not None


async def revoke_invite(session: AsyncSession, phone: str) -> bool:
    """Cancel the open invite. A used one is left alone: that person is a member, and
    cutting a member off is ``tenants.status`` (``po-admin suspend``), not this table."""
    return await _close_open(session, phone, revoked_at=func.now())


async def mark_used(session: AsyncSession, phone: str) -> bool:
    """Record that the open invite brought its tenant in. A re-run, a racing first message,
    or an env-listed number with no invite finds nothing open and changes nothing."""
    return await _close_open(session, phone, used_at=func.now())


async def list_invites(session: AsyncSession) -> list[Invite]:
    return list(await session.scalars(select(Invite).order_by(Invite.created_at.desc(), Invite.id)))


__all__ = [
    "create_invite",
    "has_open_invite",
    "has_used_invite",
    "list_invites",
    "mark_used",
    "revoke_invite",
]
