"""The onboarding conversation: the ``zone`` and ``connect`` steps (D10).

The tenant gate calls this for every message from an ``onboarding`` tenant. It decides from
four things only: the tenant's stored step, whether this is their first recorded message,
the guess from their number, and the message itself. None of those change until the gate
commits, so a re-run of the job reaches the same decision.

**Send first, return the change.** The step sends through :func:`send_once` under one kind
per decision, and *returns* an :class:`Advance` rather than writing it. The gate commits the
advance together with the finished inbox row and its copy into ``messages`` (D7), in one
transaction. Consider a job that dies after a send and before that commit. It re-runs into a
send that is already claimed and an advance that has not happened, and the step moves once.
The reverse order, writing the step first, would make the re-run treat the same message as
a ``connect``-step message and send the link a second time.

**No choice is stored (D9).** While the step is ``zone`` and the number's country gives a
guess, the Correct/Change choice is open, rebuilt from the guess on every message. There is
no separate "waiting for a city" state either. A city is accepted whenever it is sent, so
"Change" only has to ask for one. One consequence: "1" after "Change" still means Correct.
That is harmless, because the user is shown the zone they are keeping.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Final

import structlog

from personal_organizer.db.engine import Database
from personal_organizer.interfaces.channel import OutboundChannel
from personal_organizer.messaging.choices import Choice, Option, render_text, resolve
from personal_organizer.messaging.inbox import InboxRow
from personal_organizer.messaging.onboarding_text import t
from personal_organizer.messaging.outbox import send_once
from personal_organizer.messaging.tenancy import TenantState, phone_of
from personal_organizer.messaging.timezones import guess_zone, match_zone
from personal_organizer.onboarding.links import issue_or_reuse_link
from personal_organizer.settings import Settings

log = structlog.get_logger(__name__)

WELCOME_ZONE: Final = "onboarding:welcome_zone"
ZONE_ASK_CITY: Final = "onboarding:zone_ask_city"
ZONE_RETRY: Final = "onboarding:zone_retry"
CONNECT: Final = "onboarding:connect"
CONNECT_RESEND: Final = "onboarding:connect_resend"

ZONE_CORRECT: Final = "zone:correct"
ZONE_CHANGE: Final = "zone:change"

ZONE_STEP: Final = "zone"
CONNECT_STEP: Final = "connect"

#: Both languages, whatever the tenant's: people answer in the language they think in.
_CORRECT_ALIASES: Final = ("yes", "y", "ok", "correct", "right", "כן", "נכון")
_CHANGE_ALIASES: Final = ("no", "n", "change", "wrong", "לא", "לשנות", "שנה")


@dataclass(frozen=True, slots=True)
class Advance:
    """The state change a step decided, for the gate to commit with the finished row.
    ``None`` leaves a field as it is."""

    timezone: str | None = None
    next_step: str | None = None


def zone_choice(zone: str, language: str) -> Choice:
    """Correct first: ``zone_retry_keep`` tells the user to reply 1 to keep the guess."""
    return Choice(
        prompt=t("zone_confirm", language, zone=zone),
        options=(
            Option(ZONE_CORRECT, t("option_correct", language), _CORRECT_ALIASES),
            Option(ZONE_CHANGE, t("option_change", language), _CHANGE_ALIASES),
        ),
    )


def welcome_text(language: str, guess: str | None) -> str:
    ask = (
        render_text(zone_choice(guess, language), language)
        if guess
        else t("zone_ask_city", language)
    )
    return f"{t('welcome', language)}\n\n{ask}"


def retry_text(language: str, guess: str | None) -> str:
    retry = t("zone_retry", language)
    return f"{retry}\n{t('zone_retry_keep', language, zone=guess)}" if guess else retry


def connect_text(language: str, zone: str, url: str) -> str:
    return f"{t('zone_set', language, zone=zone)}\n\n{t('connect_link', language, url=url)}"


async def onboarding_step(
    db: Database,
    channel: OutboundChannel,
    row: InboxRow,
    tenant: TenantState,
    *,
    settings: Settings,
    now: datetime,
) -> Advance:
    """Answer ``row`` for an onboarding ``tenant``. Sends; writes no tenant state."""
    address = row.sender_phone or await phone_of(db, tenant.id)
    if address is None:
        # A BSUID-only sender whose tenant has no number on file. Whether Graph accepts a
        # BSUID as ``to`` is unconfirmed, so there is no reply, exactly as for strangers.
        log.info("onboarding.unaddressable", inbox_id=str(row.id), tenant_id=str(tenant.id))
        return Advance()
    to: str = address
    language = tenant.language
    guess = guess_zone(to)

    async def send(kind: str, text: str) -> None:
        await send_once(
            db,
            channel,
            inbox_id=row.id,
            kind=kind,
            recipient_key=row.sender_key,
            to=to,
            text=text,
        )

    async def confirm(zone: str) -> Advance:
        url = await issue_or_reuse_link(db, tenant.id, settings=settings, now=now)
        await send(CONNECT, connect_text(language, zone, url))
        return Advance(timezone=zone, next_step=CONNECT_STEP)

    if tenant.step != ZONE_STEP:
        # ``connect``, and defensively anything else: connecting is the only way out.
        url = await issue_or_reuse_link(db, tenant.id, settings=settings, now=now)
        await send(CONNECT_RESEND, t("connect_link", language, url=url))
        return Advance()
    if tenant.first_message:
        await send(WELCOME_ZONE, welcome_text(language, guess))
        return Advance()
    if guess is not None:
        picked = resolve(zone_choice(guess, language), reply_id=row.reply_id, text=row.body)
        if picked is not None and picked.id == ZONE_CORRECT:
            return await confirm(guess)
        if picked is not None:
            await send(ZONE_ASK_CITY, t("zone_ask_city", language))
            return Advance()
    zone = match_zone(row.body) if row.body else None
    if zone is not None:
        return await confirm(zone)
    log.info("onboarding.zone_unmatched", inbox_id=str(row.id), tenant_id=str(tenant.id))
    await send(ZONE_RETRY, retry_text(language, guess))
    return Advance()


__all__ = [
    "CONNECT",
    "CONNECT_RESEND",
    "CONNECT_STEP",
    "WELCOME_ZONE",
    "ZONE_ASK_CITY",
    "ZONE_CHANGE",
    "ZONE_CORRECT",
    "ZONE_RETRY",
    "ZONE_STEP",
    "Advance",
    "connect_text",
    "onboarding_step",
    "retry_text",
    "welcome_text",
    "zone_choice",
]
