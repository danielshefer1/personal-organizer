"""``po-admin`` against Postgres: invite, revoke and list (Task 3); suspend and unsuspend
(Task 4). Operator output is stdout; logs carry no number."""

from __future__ import annotations

import asyncio
import io
from collections.abc import AsyncIterator
from typing import Any

import pytest

from personal_organizer.admin import cli
from personal_organizer.db.engine import Database
from personal_organizer.observability.logging import configure_logging
from personal_organizer.settings import Settings
from tests.fixtures.tenants import IL_PHONE, US_PHONE, new_tenant

pytestmark = [
    pytest.mark.db,
    pytest.mark.usefixtures("clean_channel_tables", "clean_tenant_tables"),
]

BOT_PHONE = "+972531112222"
OWNER_PHONE = "+31612345678"


def _with(
    settings: Settings, *, bot_phone: str | None = BOT_PHONE, allowlist: str = OWNER_PHONE
) -> Settings:
    return settings.model_copy(
        update={
            "whatsapp": settings.whatsapp.model_copy(update={"allowed_phones": allowlist}),
            "onboarding": settings.onboarding.model_copy(update={"bot_phone": bot_phone}),
        }
    )


@pytest.fixture
def admin_settings(db_settings: Settings) -> Settings:
    return _with(db_settings)


@pytest.fixture
async def db(admin_settings: Settings, owner_conn: Any) -> AsyncIterator[Database]:
    database = Database(admin_settings)
    try:
        yield database
    finally:
        await database.dispose()


async def _invites(conn: Any) -> list[tuple[str, str | None, bool, bool]]:
    rows = await conn.fetch(
        "SELECT phone, note, used_at IS NOT NULL AS used, revoked_at IS NOT NULL AS revoked "
        "FROM invites ORDER BY created_at"
    )
    return [(r["phone"], r["note"], r["used"], r["revoked"]) for r in rows]


class TestNumbers:
    """Review Focus 1: numbers as people type them."""

    @pytest.mark.parametrize("typed", ["+972 50-123-4567", "00972501234567", "+972501234567"])
    async def test_international_forms_are_normalised(
        self, db: Database, admin_settings: Settings, owner_conn: Any, typed: str
    ) -> None:
        assert await cli.invite(db, admin_settings, typed, note=None) == 0
        assert await _invites(owner_conn) == [(IL_PHONE, None, False, False)]

    async def test_a_local_number_is_refused_and_not_echoed(
        self,
        db: Database,
        admin_settings: Settings,
        owner_conn: Any,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        assert await cli.invite(db, admin_settings, "0501234567", note=None) == 1
        out = capsys.readouterr().out
        assert "not an E.164 number" in out
        assert "0501234567" not in out
        assert await _invites(owner_conn) == []


class TestInvite:
    async def test_it_records_the_invite_and_prints_the_link(
        self,
        db: Database,
        admin_settings: Settings,
        owner_conn: Any,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        assert await cli.invite(db, admin_settings, IL_PHONE, note="Mom") == 0
        assert await _invites(owner_conn) == [(IL_PHONE, "Mom", False, False)]
        assert "https://wa.me/972531112222" in capsys.readouterr().out

    async def test_inviting_twice_prints_the_link_again(
        self,
        db: Database,
        admin_settings: Settings,
        owner_conn: Any,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        await cli.invite(db, admin_settings, IL_PHONE, note=None)
        capsys.readouterr()
        assert await cli.invite(db, admin_settings, IL_PHONE, note=None) == 0
        out = capsys.readouterr().out
        assert "already has an open invite" in out
        assert "https://wa.me/972531112222" in out
        assert len(await _invites(owner_conn)) == 1

    @pytest.mark.parametrize(("status", "hint"), [("active", ""), ("suspended", "unsuspend")])
    async def test_a_member_is_not_invited_again(
        self,
        db: Database,
        admin_settings: Settings,
        owner_conn: Any,
        capsys: pytest.CaptureFixture[str],
        status: str,
        hint: str,
    ) -> None:
        """Review Focus 2: the gate never reads an invite for someone with a tenant."""
        await new_tenant(db, phone=IL_PHONE, status=status, step=None)
        assert await cli.invite(db, admin_settings, IL_PHONE, note=None) == 1
        out = capsys.readouterr().out
        assert f"already a member ({status})" in out
        assert hint in out
        assert await _invites(owner_conn) == []

    async def test_without_a_bot_phone_it_still_records(
        self,
        db: Database,
        db_settings: Settings,
        owner_conn: Any,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        settings = _with(db_settings, bot_phone=None)
        assert await cli.invite(db, settings, IL_PHONE, note=None) == 0
        out = capsys.readouterr().out
        assert "no ONBOARDING__BOT_PHONE" in out
        assert "wa.me" not in out
        assert len(await _invites(owner_conn)) == 1


class TestRevoke:
    async def test_it_cancels_an_open_invite(
        self, db: Database, admin_settings: Settings, owner_conn: Any
    ) -> None:
        await cli.invite(db, admin_settings, IL_PHONE, note=None)
        assert await cli.revoke(db, admin_settings, IL_PHONE) == 0
        assert await _invites(owner_conn) == [(IL_PHONE, None, False, True)]

    async def test_a_used_invite_points_at_suspend(
        self,
        db: Database,
        admin_settings: Settings,
        owner_conn: Any,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        await cli.invite(db, admin_settings, IL_PHONE, note=None)
        await owner_conn.execute("UPDATE invites SET used_at = now()")
        capsys.readouterr()
        assert await cli.revoke(db, admin_settings, IL_PHONE) == 1
        assert "invite already used; use suspend" in capsys.readouterr().out

    async def test_nothing_to_revoke(
        self, db: Database, admin_settings: Settings, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert await cli.revoke(db, admin_settings, IL_PHONE) == 1
        assert "no open invite" in capsys.readouterr().out


class TestList:
    async def test_it_shows_each_state_and_the_member_status(
        self,
        db: Database,
        admin_settings: Settings,
        owner_conn: Any,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        await cli.invite(db, admin_settings, IL_PHONE, note="Mom")
        await owner_conn.execute("UPDATE invites SET used_at = now() WHERE phone = $1", IL_PHONE)
        await new_tenant(db, phone=IL_PHONE, status="active", step=None)
        await cli.invite(db, admin_settings, US_PHONE, note=None)
        capsys.readouterr()

        assert await cli.list_invites_command(db, admin_settings) == 0
        lines = capsys.readouterr().out.strip().splitlines()
        assert len(lines) == 2
        assert lines[0].startswith(US_PHONE)
        assert "open" in lines[0]
        assert lines[1].startswith(IL_PHONE)
        assert "used" in lines[1]
        assert "active" in lines[1]
        assert lines[1].endswith("Mom")

    async def test_an_empty_list(
        self, db: Database, admin_settings: Settings, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert await cli.list_invites_command(db, admin_settings) == 0
        assert "no invites" in capsys.readouterr().out


class TestMain:
    async def test_main_runs_a_command(self, owner_conn: Any) -> None:
        """Through argparse and the real environment's settings, as Railway would run it.
        ``main`` calls ``asyncio.run``, so it runs in a thread with its own event loop."""
        assert await asyncio.to_thread(cli.main, ["invite", IL_PHONE, "--note", "Mom"]) == 0
        assert await asyncio.to_thread(cli.main, ["list"]) == 0
        assert await _invites(owner_conn) == [(IL_PHONE, "Mom", False, False)]


class TestLogs:
    async def test_invite_and_revoke_log_no_number_note_or_link(
        self, db: Database, admin_settings: Settings
    ) -> None:
        stream = io.StringIO()
        configure_logging(admin_settings, stream=stream)
        await cli.invite(db, admin_settings, IL_PHONE, note="Mom")
        await cli.revoke(db, admin_settings, IL_PHONE)
        captured = stream.getvalue()
        assert "admin.invite.created" in captured
        assert "admin.invite.revoked" in captured
        assert "sender_hash" in captured
        for leaked in (IL_PHONE, IL_PHONE.removeprefix("+"), "Mom", "wa.me"):
            assert leaked not in captured
