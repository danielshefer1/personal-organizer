"""Handling one stored inbound message: the access-control gate in front of the agent.

Iteration 02 has no agent, so an allowlisted sender gets a fixed acknowledgement. What this
module guarantees is the part that must hold before there *is* one: a sender who is not on
the allowlist never reaches ``on_allowed`` -- the seam Iteration 04 turns into "defer an agent
turn" -- and so can never spend an LLM call or a token of budget. They get one fixed line,
at most once a day, and their message content is deleted.

Everything is idempotent under a re-run of the job: ``processed_at`` ends a re-run early,
and replies go through :func:`send_once`.

Replies go out on the channel the message came in on. Several channels can be live at once,
so the handler is given a resolver and looks up the row's ``channel`` by name. The
invite-only mute, though, is per *person* (``recipient_key``), not per channel: a stranger
who writes to two of our numbers is told once.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final
from uuid import UUID

import structlog
from sqlalchemy import exists, func, select, update

from personal_organizer.core.errors import ChannelError
from personal_organizer.db.engine import Database
from personal_organizer.db.models.channel import ChannelInbox, ChannelOutbox
from personal_organizer.interfaces.channel import OutboundChannel
from personal_organizer.messaging.outbox import send_once
from personal_organizer.messaging.replies import (
    ACK_TEXT,
    INVITE_ONLY_MUTE,
    INVITE_ONLY_TEXT,
    SERVICE_WINDOW,
    SERVICE_WINDOW_MARGIN,
)

log = structlog.get_logger(__name__)

ACK: Final = "ack"
INVITE_ONLY: Final = "invite_only"

#: Outbox states that count as "we already told this stranger". A reply that is still
#: pending or that Meta refused does not mute the next one.
_MUTING_STATUSES: Final = ("sending", "unknown", "accepted", "sent", "delivered", "read")


@dataclass(frozen=True, slots=True)
class InboxRow:
    """A snapshot of the row, read once at the start of the job."""

    id: UUID
    channel: str
    provider_message_id: str
    sender_key: str
    sender_phone: str | None
    message_type: str
    body: str | None
    sent_at: datetime
    processed_at: datetime | None
    disposition: str | None

    @classmethod
    def of(cls, row: ChannelInbox) -> InboxRow:
        return cls(
            id=row.id,
            channel=row.channel,
            provider_message_id=row.provider_message_id,
            sender_key=row.sender_key,
            sender_phone=row.sender_phone,
            message_type=row.message_type,
            body=row.body,
            sent_at=row.sent_at,
            processed_at=row.processed_at,
            disposition=row.disposition,
        )


#: The handoff for an allowed sender, given the channel to answer on.
OnAllowed = Callable[[InboxRow, OutboundChannel], Awaitable[None]]

#: Looks up an outbound channel by name; raises ``ChannelNotConfiguredError`` if it is absent.
ChannelResolver = Callable[[str], OutboundChannel]


def _utcnow() -> datetime:
    return datetime.now(UTC)


async def _load(db: Database, inbox_id: UUID) -> InboxRow | None:
    async with db.system_session() as session:
        row = await session.get(ChannelInbox, inbox_id)
        return InboxRow.of(row) if row is not None else None


async def _already_told(db: Database, row: InboxRow, now: datetime) -> bool:
    """Has this sender had the invite-only reply, for another message, within the mute?"""
    async with db.system_session() as session:
        told = await session.scalar(
            select(
                exists().where(
                    ChannelOutbox.recipient_key == row.sender_key,
                    ChannelOutbox.kind == INVITE_ONLY,
                    ChannelOutbox.status.in_(_MUTING_STATUSES),
                    ChannelOutbox.created_at > now - INVITE_ONLY_MUTE,
                    ChannelOutbox.inbox_id.is_distinct_from(row.id),
                )
            )
        )
    return bool(told)


async def _finish(db: Database, row: InboxRow, disposition: str, *, purge: bool) -> None:
    values: dict[str, object] = {"processed_at": func.now(), "disposition": disposition}
    if purge:
        # A stranger's words have no reason to be kept, and every reason not to be.
        values |= {"body": None, "raw": None, "media_id": None, "media_mime_type": None}
    async with db.system_session() as session:
        await session.execute(update(ChannelInbox).where(ChannelInbox.id == row.id).values(values))


async def acknowledge(row: InboxRow, channel: OutboundChannel, *, db: Database) -> None:
    """Iteration 02's ``on_allowed``: the fixed acknowledgement. Iteration 04 replaces it."""
    if row.sender_phone is None:  # pragma: no cover - allowlisting requires a phone
        return
    await send_once(
        db,
        channel,
        inbox_id=row.id,
        kind=ACK,
        recipient_key=row.sender_key,
        to=row.sender_phone,
        text=ACK_TEXT,
    )


async def _turn_away(db: Database, channel: OutboundChannel, row: InboxRow, now: datetime) -> str:
    if row.sender_phone is None:
        # A BSUID-only sender. Whether Graph accepts a BSUID as ``to`` is unconfirmed, so
        # there is no reply; Iteration 03's tenant identities settle it.
        log.info("inbound.reply_unaddressable", inbox_id=str(row.id), sender=row.sender_key)
        return "stranger"
    if await _already_told(db, row, now):
        return "stranger_muted"
    await send_once(
        db,
        channel,
        inbox_id=row.id,
        kind=INVITE_ONLY,
        recipient_key=row.sender_key,
        to=row.sender_phone,
        text=INVITE_ONLY_TEXT,
    )
    return "stranger"


async def handle_inbound(
    inbox_id: UUID,
    *,
    db: Database,
    channels: ChannelResolver,
    allowlist: frozenset[str],
    on_allowed: OnAllowed,
    now: Callable[[], datetime] = _utcnow,
) -> str | None:
    """Process one inbox row. Returns its disposition, or ``None`` if the row is gone."""
    row = await _load(db, inbox_id)
    if row is None:
        log.warning("inbound.missing", inbox_id=str(inbox_id))
        return None
    if row.processed_at is not None:
        return row.disposition
    # Before anything is claimed or sent: a row whose channel this worker does not serve
    # (switched off with jobs still queued) fails here and stays unprocessed for a re-run.
    channel = channels(row.channel)

    allowed = row.sender_phone is not None and row.sender_phone in allowlist
    current = now()
    if current - row.sent_at >= SERVICE_WINDOW - SERVICE_WINDOW_MARGIN:
        # Redelivered after an outage. The reply window has closed, so any send would be
        # refused -- and replying to a day-old message out of the blue is worse than silence.
        disposition = "stale"
    elif allowed:
        try:
            await channel.mark_read(row.provider_message_id)
        except ChannelError as exc:
            # Blue ticks are cosmetic; failing the job over them would delay the reply.
            log.warning(
                "inbound.mark_read_failed", inbox_id=str(row.id), error_type=type(exc).__name__
            )
        await on_allowed(row, channel)
        disposition = "allowed"
    else:
        disposition = await _turn_away(db, channel, row, current)

    await _finish(db, row, disposition, purge=not allowed)
    log.info(
        "inbound.handled",
        inbox_id=str(row.id),
        channel=row.channel,
        message_type=row.message_type,
        disposition=disposition,
        sender=row.sender_key,
    )
    return disposition


__all__ = [
    "ACK",
    "INVITE_ONLY",
    "ChannelResolver",
    "InboxRow",
    "OnAllowed",
    "acknowledge",
    "handle_inbound",
]
