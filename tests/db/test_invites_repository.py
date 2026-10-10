"""The ``invites`` table: one open invite per number, history kept, no DELETE for the app."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import asyncpg
import pytest

from personal_organizer.db.engine import Database
from personal_organizer.db.repositories.invites import (
    create_invite,
    has_open_invite,
    has_used_invite,
    list_invites,
    mark_used,
    revoke_invite,
)
from personal_organizer.settings import Settings

pytestmark = [pytest.mark.db, pytest.mark.usefixtures("clean_channel_tables")]

PHONE = "+972501234567"
OTHER = "+12025550123"


@pytest.fixture
async def db(db_settings: Settings, owner_conn: Any) -> AsyncIterator[Database]:
    database = Database(db_settings)
    try:
        yield database
    finally:
        await database.dispose()


async def _create(db: Database, phone: str = PHONE, note: str | None = None) -> bool:
    async with db.system_session() as session:
        return await create_invite(session, phone, note=note)


async def _open(db: Database, phone: str = PHONE) -> bool:
    async with db.system_session() as session:
        return await has_open_invite(session, phone)


class TestCreate:
    async def test_a_new_invite_is_open(self, db: Database) -> None:
        assert await _create(db, note="Mom") is True
        assert await _open(db) is True
        assert await _open(db, OTHER) is False

    async def test_creating_twice_keeps_one_open_invite(
        self, db: Database, owner_conn: Any
    ) -> None:
        assert await _create(db) is True
        assert await _create(db) is False
        assert await owner_conn.fetchval("SELECT count(*) FROM invites") == 1

    async def test_concurrent_creates_keep_one_open_invite(
        self, db: Database, owner_conn: Any
    ) -> None:
        """Review Focus 3: two terminals, one number, no IntegrityError."""
        results = await asyncio.gather(*(_create(db) for _ in range(5)))
        assert sorted(results) == [False, False, False, False, True]
        assert await owner_conn.fetchval("SELECT count(*) FROM invites") == 1

    async def test_a_new_invite_after_a_revoke_is_allowed(
        self, db: Database, owner_conn: Any
    ) -> None:
        await _create(db)
        async with db.system_session() as session:
            assert await revoke_invite(session, PHONE) is True
        assert await _create(db) is True
        assert await owner_conn.fetchval("SELECT count(*) FROM invites") == 2


class TestRevokeAndUse:
    async def test_revoke_closes_the_open_invite_once(self, db: Database) -> None:
        await _create(db)
        async with db.system_session() as session:
            assert await revoke_invite(session, PHONE) is True
            assert await revoke_invite(session, PHONE) is False
        assert await _open(db) is False

    async def test_mark_used_closes_it_once_and_records_it(self, db: Database) -> None:
        await _create(db)
        async with db.system_session() as session:
            assert await mark_used(session, PHONE) is True
            assert await mark_used(session, PHONE) is False
            assert await has_used_invite(session, PHONE) is True
            assert await has_used_invite(session, OTHER) is False
        assert await _open(db) is False

    async def test_a_revoked_invite_cannot_be_used(self, db: Database) -> None:
        await _create(db)
        async with db.system_session() as session:
            await revoke_invite(session, PHONE)
            assert await mark_used(session, PHONE) is False
            assert await has_used_invite(session, PHONE) is False

    async def test_nothing_to_mark_is_not_an_error(self, db: Database) -> None:
        async with db.system_session() as session:
            assert await mark_used(session, PHONE) is False


class TestList:
    async def test_newest_first_with_history(self, db: Database) -> None:
        await _create(db, PHONE, note="Mom")
        async with db.system_session() as session:
            await mark_used(session, PHONE)
        await _create(db, OTHER)
        async with db.system_session() as session:
            invites = await list_invites(session)
        assert [(i.phone, i.note, i.used_at is not None) for i in invites] == [
            (OTHER, None, False),
            (PHONE, "Mom", True),
        ]


class TestTheTable:
    async def test_app_user_cannot_delete_invites(self, app_conn: Any) -> None:
        """History: a used or revoked invite is what ``po-admin list`` shows."""
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await app_conn.execute("DELETE FROM invites")

    async def test_an_invite_cannot_be_both_used_and_revoked(self, owner_conn: Any) -> None:
        with pytest.raises(asyncpg.CheckViolationError):
            await owner_conn.execute(
                "INSERT INTO invites (phone, used_at, revoked_at) VALUES ($1, now(), now())",
                PHONE,
            )
