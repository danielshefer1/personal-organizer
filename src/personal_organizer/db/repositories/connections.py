"""Calendar connections: which Composio account a tenant's calendar is reached through.

At most one ``active`` row per tenant (a partial unique index). Reconnecting revokes the old
row and inserts the new one in the caller's transaction, so there is never a moment with two
active rows or, for anyone reading through RLS, none.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from personal_organizer.db.models.tenant import CalendarConnection
from personal_organizer.db.repositories.scope import scoped


async def _by_account(
    session: AsyncSession, tenant_id: UUID, connected_account_id: str
) -> CalendarConnection | None:
    row: CalendarConnection | None = await session.scalar(
        scoped(select(CalendarConnection), CalendarConnection, tenant_id).where(
            CalendarConnection.connected_account_id == connected_account_id
        )
    )
    return row


async def bind_connection(
    session: AsyncSession, tenant_id: UUID, *, connected_account_id: str, auth_config_id: str
) -> CalendarConnection:
    """Make ``connected_account_id`` the tenant's active connection.

    Idempotent on ``connected_account_id``: a callback delivered twice finds its row and
    returns it unchanged, whatever its status, without revoking anything. A *new* account
    revokes the previous active one first. ``composio_user_id`` is the tenant id, never a
    phone number. ``ON CONFLICT DO NOTHING`` covers the race of two callbacks for the same
    account: the loser waits for the winner's commit and then reads its row.

    Raises :class:`LookupError` when the account is already bound to *another* tenant. The
    callback's ``user_id`` check (D5) makes that unreachable in practice; if it ever happens,
    the caller's transaction rolls back and nothing of either tenant changes.
    """
    existing = await _by_account(session, tenant_id, connected_account_id)
    if existing is not None:
        return existing
    await session.execute(
        scoped(update(CalendarConnection), CalendarConnection, tenant_id)
        .where(CalendarConnection.status == "active")
        .values(status="revoked")
    )
    await session.execute(
        insert(CalendarConnection)
        .values(
            tenant_id=tenant_id,
            composio_user_id=str(tenant_id),
            connected_account_id=connected_account_id,
            auth_config_id=auth_config_id,
        )
        .on_conflict_do_nothing(index_elements=["connected_account_id"])
    )
    bound = await _by_account(session, tenant_id, connected_account_id)
    if bound is None:
        # The insert conflicted with a row RLS will not show us: another tenant's.
        msg = "connected account is bound to another tenant"
        raise LookupError(msg)
    return bound


__all__ = ["bind_connection"]
