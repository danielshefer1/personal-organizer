"""Onboarding links: the row that makes a signed connect link single-use (D4, D10).

The token's signature proves we issued it; ``used_at`` proves it has not been spent.
:func:`consume_link` is one conditional ``UPDATE``, so two POSTs racing on the same link
cannot both win: the second finds ``used_at`` already set and changes nothing.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from personal_organizer.db.models.tenant import OnboardingLink
from personal_organizer.db.repositories.scope import scoped


async def create_link(
    session: AsyncSession, tenant_id: UUID, *, nonce: str, expires_at: datetime
) -> OnboardingLink:
    link = OnboardingLink(tenant_id=tenant_id, nonce=nonce, expires_at=expires_at)
    session.add(link)
    await session.flush()
    await session.refresh(link)
    return link


async def latest_usable_link(
    session: AsyncSession, tenant_id: UUID, *, now: datetime
) -> OnboardingLink | None:
    """The newest link that is unused and unexpired at ``now``, for a re-send (D10)."""
    link: OnboardingLink | None = await session.scalar(
        scoped(select(OnboardingLink), OnboardingLink, tenant_id)
        .where(OnboardingLink.used_at.is_(None), OnboardingLink.expires_at > now)
        .order_by(OnboardingLink.created_at.desc(), OnboardingLink.expires_at.desc())
        .limit(1)
    )
    return link


async def consume_link(
    session: AsyncSession, tenant_id: UUID, *, nonce: str, now: datetime
) -> bool:
    """Spend the link. ``True`` exactly once per link, and never once it has expired."""
    result = await session.execute(
        scoped(update(OnboardingLink), OnboardingLink, tenant_id)
        .where(
            OnboardingLink.nonce == nonce,
            OnboardingLink.used_at.is_(None),
            OnboardingLink.expires_at > now,
        )
        .values(used_at=now)
        .returning(OnboardingLink.id)
    )
    return result.first() is not None


__all__ = ["consume_link", "create_link", "latest_usable_link"]
