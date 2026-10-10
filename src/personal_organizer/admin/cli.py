"""``po-admin`` -- who is in the circle (ADR 0006).

``invite <phone> [--note TEXT]``
    Open an invite and print the ``wa.me`` link to forward. The bot never writes first: an
    unsolicited first message is what gets a QR-gateway number banned.
``revoke <phone>``
    Cancel an invite nobody has used. A used one belongs to a member: ``suspend`` them.
``suspend <phone>`` / ``unsuspend <phone>``
    Turn a member away, and back. Numbers on ``WHATSAPP__ALLOWED_PHONES`` cannot be
    suspended, so the owner cannot lock themselves out by a typo.
``list``
    Every invite, newest first, with the member's status for used ones.

Runs as ``app_user``, like the worker. On Railway: ``railway ssh --service worker
--environment staging``, then ``po-admin ...``. Operator output -- numbers, notes, links --
goes to stdout and never to structlog; log lines carry ``sender`` (emitted as
``sender_hash``) and ``tenant_id`` only.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from typing import Final

import structlog

from personal_organizer.core.phone import normalise_e164
from personal_organizer.core.types import TenantId
from personal_organizer.db.engine import Database
from personal_organizer.db.models.tenant import NETWORK_WHATSAPP, Tenant
from personal_organizer.db.repositories.invites import (
    create_invite,
    has_open_invite,
    has_used_invite,
    list_invites,
    revoke_invite,
)
from personal_organizer.db.repositories.tenants import get_tenant, resolve_tenant, set_status
from personal_organizer.observability.logging import configure_logging
from personal_organizer.settings import Settings, get_settings

log = structlog.get_logger(__name__)

OK: Final = 0
REFUSED: Final = 1

#: Israel, the circle's country: a national number never starts with 0 after +972, but people
#: write the trunk 0 anyway (``+972 050-...``). Italy keeps its 0, so this is not a blanket rule.
_NO_TRUNK_ZERO: Final = ("+9720",)


def _out(line: str) -> None:
    sys.stdout.write(line + "\n")


def wa_link(bot_phone: str) -> str:
    """No ``?text=``: the tenant's language comes from their first words (D11)."""
    return f"https://wa.me/{bot_phone.removeprefix('+')}"


def parse_phone(raw: str) -> str | None:
    """The normalised number, or ``None`` after printing why. The input is never echoed.

    A trunk 0 kept after the country code -- ``+44 (0)20 ...``, ``+972 050-...`` -- is refused
    rather than stored: WhatsApp delivers the number without it, so that invite would never
    match and the invitee would be told "invite-only".
    """
    phone = normalise_e164(raw)
    if phone is None:
        _out("not an E.164 number, e.g. +972501234567")
        return None
    if "(0)" in raw or phone.startswith(_NO_TRUNK_ZERO):
        _out("drop the 0 after the country code, e.g. +972501234567")
        return None
    return phone


async def tenant_of(db: Database, phone: str) -> Tenant | None:
    async with db.system_session() as session:
        tenant_id = await resolve_tenant(
            session, network=NETWORK_WHATSAPP, external_id=f"tel:{phone}"
        )
    if tenant_id is None:
        return None
    async with db.tenant_session(TenantId(tenant_id)) as session:
        return await get_tenant(session, tenant_id)


def _print_link(settings: Settings) -> None:
    bot_phone = settings.onboarding.bot_phone
    if bot_phone is None:
        _out("no ONBOARDING__BOT_PHONE; send them the bot's number yourself")
        return
    _out(f"send them: {wa_link(bot_phone)}")


async def invite(db: Database, settings: Settings, raw_phone: str, *, note: str | None) -> int:
    phone = parse_phone(raw_phone)
    if phone is None:
        return REFUSED
    tenant = await tenant_of(db, phone)
    if tenant is not None:
        hint = "; use unsuspend" if tenant.status == "suspended" else ""
        _out(f"already a member ({tenant.status}){hint}")
        return REFUSED
    async with db.system_session() as session:
        created = await create_invite(session, phone, note=note)
    if created:
        log.info("admin.invite.created", sender=f"tel:{phone}")
        _out(f"invited {phone}")
    else:
        _out(f"{phone} already has an open invite")
    _print_link(settings)
    return OK


async def revoke(db: Database, settings: Settings, raw_phone: str) -> int:
    del settings
    phone = parse_phone(raw_phone)
    if phone is None:
        return REFUSED
    async with db.system_session() as session:
        revoked = await revoke_invite(session, phone)
        used = not revoked and await has_used_invite(session, phone)
    if revoked:
        log.info("admin.invite.revoked", sender=f"tel:{phone}")
        _out(f"revoked the invite for {phone}")
        return OK
    _out("invite already used; use suspend" if used else "no open invite")
    return REFUSED


async def suspend(db: Database, settings: Settings, raw_phone: str) -> int:
    phone = parse_phone(raw_phone)
    if phone is None:
        return REFUSED
    if phone in settings.whatsapp.allowlist:
        _out("on WHATSAPP__ALLOWED_PHONES; remove it there first")
        return REFUSED
    tenant = await tenant_of(db, phone)
    if tenant is None:
        # With Composio off an invitee is served with no tenant; revoke is what cuts them off.
        async with db.system_session() as session:
            pending = await has_open_invite(session, phone)
        _out("not a member yet (open invite); use revoke" if pending else "not a member")
        return REFUSED
    if tenant.status == "suspended":
        _out("already suspended")
        return OK
    async with db.tenant_session(TenantId(tenant.id)) as session:
        await set_status(session, tenant.id, "suspended")
    log.info("admin.tenant.suspended", tenant_id=str(tenant.id), sender=f"tel:{phone}")
    _out(f"suspended {phone}; their next message gets the invite-only line")
    return OK


async def unsuspend(db: Database, settings: Settings, raw_phone: str) -> int:
    del settings
    phone = parse_phone(raw_phone)
    if phone is None:
        return REFUSED
    tenant = await tenant_of(db, phone)
    if tenant is None:
        _out("not a member")
        return REFUSED
    if tenant.status != "suspended":
        _out(f"not suspended ({tenant.status})")
        return OK
    # activate() clears the step, so no step means onboarding was finished.
    restored = "active" if tenant.onboarding_step is None else "onboarding"
    async with db.tenant_session(TenantId(tenant.id)) as session:
        await set_status(session, tenant.id, restored)
    log.info("admin.tenant.unsuspended", tenant_id=str(tenant.id), sender=f"tel:{phone}")
    _out(f"{phone} is {restored} again")
    return OK


async def list_invites_command(db: Database, settings: Settings) -> int:
    del settings
    async with db.system_session() as session:
        invites = await list_invites(session)
    if not invites:
        _out("no invites")
        return OK
    for entry in invites:
        if entry.used_at is not None:
            state = "used"
            tenant = await tenant_of(db, entry.phone)
            member = tenant.status if tenant is not None else "gone"
        else:
            state = "revoked" if entry.revoked_at is not None else "open"
            member = "-"
        fields = [entry.phone, f"{state:<7}", f"{entry.created_at:%Y-%m-%d}", f"{member:<10}"]
        _out("  ".join([*fields, entry.note or ""]).rstrip())
    return OK


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="po-admin", description="who is in the circle")
    commands = parser.add_subparsers(dest="command", required=True)
    invited = commands.add_parser("invite", help="open an invite and print the wa.me link")
    invited.add_argument("phone", help="E.164, e.g. +972501234567")
    invited.add_argument("--note", help="your label for them, e.g. Mom")
    revoked = commands.add_parser("revoke", help="cancel an invite nobody has used")
    revoked.add_argument("phone")
    for name, text in (
        ("suspend", "turn a member away; their messages get the invite-only line"),
        ("unsuspend", "let a suspended member back in"),
    ):
        command = commands.add_parser(name, help=text)
        command.add_argument("phone")
    commands.add_parser("list", help="every invite, newest first")
    return parser


async def _run(args: argparse.Namespace, settings: Settings) -> int:
    db = Database(settings)
    try:
        if args.command == "invite":
            return await invite(db, settings, args.phone, note=args.note)
        if args.command == "revoke":
            return await revoke(db, settings, args.phone)
        if args.command == "suspend":
            return await suspend(db, settings, args.phone)
        if args.command == "unsuspend":
            return await unsuspend(db, settings, args.phone)
        return await list_invites_command(db, settings)
    finally:
        await db.dispose()


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    settings = get_settings()
    configure_logging(settings)
    return asyncio.run(_run(args, settings))


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
