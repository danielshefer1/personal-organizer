"""Resolving a sender to a tenant through network-keyed identities (D12)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from uuid import uuid4

import pytest

from personal_organizer.core.types import TenantId
from personal_organizer.db.engine import Database
from personal_organizer.db.models.tenant import NETWORK_WHATSAPP
from personal_organizer.db.repositories.messages import record_inbound
from personal_organizer.db.repositories.tenants import resolve_tenant
from personal_organizer.messaging.inbox import InboxRow, load_row
from personal_organizer.messaging.tenancy import (
    TenantState,
    enrol,
    load_state,
    phone_of,
    resolve_sender,
)
from personal_organizer.settings import Settings
from tests.fixtures.channels import insert_inbox
from tests.fixtures.tenants import NL_PHONE

pytestmark = [
    pytest.mark.db,
    pytest.mark.usefixtures("clean_channel_tables", "clean_tenant_tables"),
]


@pytest.fixture
async def db(db_settings: Settings, owner_conn: Any) -> AsyncIterator[Database]:
    database = Database(db_settings)
    try:
        yield database
    finally:
        await database.dispose()


async def _row(
    db: Database,
    conn: Any,
    *,
    user_id: str | None = None,
    phone: str | None = NL_PHONE,
    channel: str = "gowa",
) -> InboxRow:
    row = await load_row(
        db, await insert_inbox(conn, user_id=user_id, phone=phone, channel=channel)
    )
    assert row is not None
    return row


async def _resolve(db: Database, key: str) -> Any:
    async with db.system_session() as session:
        return await resolve_tenant(session, network=NETWORK_WHATSAPP, external_id=key)


class TestIdentities:
    async def test_an_unknown_sender_resolves_to_nothing(
        self, db: Database, owner_conn: Any
    ) -> None:
        assert await resolve_sender(db, await _row(db, owner_conn)) is None

    async def test_enrolled_by_phone_resolves_on_either_channel(
        self, db: Database, owner_conn: Any
    ) -> None:
        tenant_id = await enrol(db, await _row(db, owner_conn, channel="gowa"), language="en")
        via_meta = await _row(db, owner_conn, channel="whatsapp")
        assert await resolve_sender(db, via_meta) == tenant_id

    async def test_enrolled_with_a_bsuid_and_a_phone_records_both(
        self, db: Database, owner_conn: Any
    ) -> None:
        row = await _row(db, owner_conn, user_id="US.1", channel="whatsapp")
        tenant_id = await enrol(db, row, language="en")
        assert await _resolve(db, "uid:US.1") == tenant_id
        assert await _resolve(db, f"tel:{NL_PHONE}") == tenant_id

    async def test_a_bsuid_for_a_phone_keyed_tenant_is_added_not_a_second_tenant(
        self, db: Database, owner_conn: Any
    ) -> None:
        """Review Focus 3: first seen on the gateway by number, later on Meta with a BSUID."""
        tenant_id = await enrol(db, await _row(db, owner_conn), language="en")
        with_bsuid = await _row(db, owner_conn, user_id="US.1", channel="whatsapp")
        assert await resolve_sender(db, with_bsuid) == tenant_id
        assert await _resolve(db, "uid:US.1") == tenant_id
        hidden_number = await _row(db, owner_conn, user_id="US.1", phone=None, channel="whatsapp")
        assert await resolve_sender(db, hidden_number) == tenant_id

    async def test_enrol_is_idempotent(self, db: Database, owner_conn: Any) -> None:
        row = await _row(db, owner_conn, user_id="US.1", channel="whatsapp")
        assert await enrol(db, row, language="en") == await enrol(db, row, language="he")


class TestState:
    async def test_a_new_tenant_is_onboarding_its_first_message(
        self, db: Database, owner_conn: Any
    ) -> None:
        row = await _row(db, owner_conn)
        tenant_id = await enrol(db, row, language="he")
        assert await load_state(db, tenant_id) == TenantState(
            id=tenant_id, status="onboarding", step="zone", language="he", first_message=True
        )
        async with db.tenant_session(TenantId(tenant_id)) as session:
            await record_inbound(
                session,
                tenant_id,
                inbox_id=row.id,
                channel=row.channel,
                message_type=row.message_type,
                body=row.body,
                sent_at=row.sent_at,
            )
        state = await load_state(db, tenant_id)
        assert state is not None
        assert state.first_message is False

    async def test_an_unknown_tenant_has_no_state(self, db: Database) -> None:
        assert await load_state(db, uuid4()) is None

    async def test_phone_of(self, db: Database, owner_conn: Any) -> None:
        tenant_id = await enrol(db, await _row(db, owner_conn), language="en")
        assert await phone_of(db, tenant_id) == NL_PHONE
