"""The connect flow against Postgres, with a fake Composio and a fake queue."""

from __future__ import annotations

import asyncio
import html
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from personal_organizer.core.errors import CalendarProviderUnavailableError
from personal_organizer.core.types import TenantId
from personal_organizer.db.engine import Database
from personal_organizer.db.models.tenant import OnboardingLink, Tenant
from personal_organizer.onboarding.page_text import page_text
from personal_organizer.onboarding.tokens import LINK_SALT, STATE_SALT, verify
from personal_organizer.settings import Settings
from tests.api.conftest import FakeProcrastinate
from tests.fixtures.connect import (
    AUTH_CONFIG_ID,
    BASE_URL,
    LINK_SECRET,
    REDIRECT_URL,
    TRUNCATE_CONNECT_TABLES,
    FakeConnectLinker,
    assert_hardened,
    assert_page,
    connect_app,
    make_active,
    seed_link,
    seed_tenant,
    with_connect,
)

pytestmark = [pytest.mark.db]


@pytest.fixture
def connect_settings(db_settings: Settings) -> Settings:
    return with_connect(db_settings)


@pytest.fixture
async def db(connect_settings: Settings, owner_conn: Any) -> AsyncIterator[Database]:
    await owner_conn.execute(TRUNCATE_CONNECT_TABLES)
    database = Database(connect_settings)
    try:
        yield database
    finally:
        await database.dispose()
        await owner_conn.execute(TRUNCATE_CONNECT_TABLES)


@pytest.fixture
def linker() -> FakeConnectLinker:
    return FakeConnectLinker()


@pytest.fixture
def queue() -> FakeProcrastinate:
    return FakeProcrastinate()


@pytest.fixture
async def http(
    connect_settings: Settings, db: Database, linker: FakeConnectLinker, queue: FakeProcrastinate
) -> AsyncIterator[AsyncClient]:
    application = connect_app(connect_settings, db=db, linker=linker, queue=queue)
    transport = ASGITransport(app=application, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


class TestConnectPage:
    async def test_it_speaks_the_tenants_language(self, http: AsyncClient, db: Database) -> None:
        tenant_id = await seed_tenant(db, language="he")
        response = await http.get(f"/connect/{await seed_link(db, tenant_id)}")
        assert response.status_code == 200
        assert '<html lang="he" dir="rtl">' in response.text
        assert page_text("connect", "he")["button"] in html.unescape(response.text)
        assert "4567" in response.text  # the last digits of PHONE
        assert_hardened(response)

    async def test_english_reads_left_to_right(self, http: AsyncClient, db: Database) -> None:
        tenant_id = await seed_tenant(db, language="en")
        response = await http.get(f"/connect/{await seed_link(db, tenant_id)}")
        assert '<html lang="en" dir="ltr">' in response.text
        assert page_text("connect", "en")["button"] in html.unescape(response.text)

    async def test_previews_do_not_spend_the_link(
        self, http: AsyncClient, db: Database, linker: FakeConnectLinker
    ) -> None:
        """WhatsApp fetches a link to build its preview. Only the button's POST may spend it."""
        tenant_id = await seed_tenant(db)
        token = await seed_link(db, tenant_id)
        for _ in range(3):
            await http.get(f"/connect/{token}")
        await http.head(f"/connect/{token}")
        response = await http.post(f"/connect/{token}")
        assert response.status_code == 303
        assert len(linker.links) == 1

    async def test_a_get_writes_nothing(self, http: AsyncClient, db: Database) -> None:
        """The link row is untouched after GETs: not used, and not updated in any column."""
        tenant_id = await seed_tenant(db)
        token = await seed_link(db, tenant_id)

        async def snapshot() -> list[tuple[Any, ...]]:
            async with db.tenant_session(TenantId(tenant_id)) as session:
                rows = await session.execute(
                    select(
                        OnboardingLink.nonce,
                        OnboardingLink.used_at,
                        OnboardingLink.expires_at,
                        Tenant.status,
                        Tenant.onboarding_step,
                        Tenant.updated_at,
                    ).join(Tenant, Tenant.id == OnboardingLink.tenant_id)
                )
                return [tuple(row) for row in rows]

        before = await snapshot()
        for _ in range(3):
            assert (await http.get(f"/connect/{token}")).status_code == 200
        after = await snapshot()
        assert after == before
        assert after[0][1] is None

    async def test_a_tenant_already_connected_is_refused(
        self, http: AsyncClient, db: Database, linker: FakeConnectLinker
    ) -> None:
        tenant_id = await seed_tenant(db)
        token = await seed_link(db, tenant_id)
        await make_active(db, tenant_id)
        assert (await http.get(f"/connect/{token}")).status_code == 410
        assert (await http.post(f"/connect/{token}")).status_code == 410
        assert linker.links == []


class TestStart:
    async def test_the_button_goes_to_composio(
        self, http: AsyncClient, db: Database, linker: FakeConnectLinker
    ) -> None:
        tenant_id = await seed_tenant(db)
        response = await http.post(f"/connect/{await seed_link(db, tenant_id)}")
        assert response.status_code == 303
        assert response.headers["location"] == REDIRECT_URL
        assert_hardened(response)
        [call] = linker.links
        assert call["user_id"] == str(tenant_id)  # the tenant UUID, never a phone number
        assert call["auth_config_id"] == AUTH_CONFIG_ID
        callback = urlsplit(call["callback_url"])
        assert f"{callback.scheme}://{callback.netloc}{callback.path}" == (
            f"{BASE_URL}/connect/callback"
        )
        [state] = parse_qs(callback.query)["state"]
        payload = verify(state, secret=LINK_SECRET, salt=STATE_SALT, max_age_s=60)
        assert payload is not None
        assert payload.tenant_id == tenant_id
        # A state is not a link: the salts keep the two kinds of token apart.
        assert verify(state, secret=LINK_SECRET, salt=LINK_SALT, max_age_s=60) is None

    async def test_a_link_works_once(
        self, http: AsyncClient, db: Database, linker: FakeConnectLinker
    ) -> None:
        tenant_id = await seed_tenant(db)
        url = f"/connect/{await seed_link(db, tenant_id)}"
        first, second = await http.post(url), await http.post(url)
        assert (first.status_code, second.status_code) == (303, 410)
        assert_page(second, "link_unusable", "he", "en")
        assert len(linker.links) == 1

    async def test_two_taps_at_once_make_one_redirect(
        self, http: AsyncClient, db: Database, linker: FakeConnectLinker
    ) -> None:
        tenant_id = await seed_tenant(db)
        url = f"/connect/{await seed_link(db, tenant_id)}"
        responses = await asyncio.gather(http.post(url), http.post(url))
        assert sorted(r.status_code for r in responses) == [303, 410]
        assert len(linker.links) == 1

    async def test_an_expired_link_is_refused(
        self, http: AsyncClient, db: Database, linker: FakeConnectLinker
    ) -> None:
        tenant_id = await seed_tenant(db)
        token = await seed_link(db, tenant_id, expires_at=datetime.now(UTC) - timedelta(minutes=1))
        assert (await http.post(f"/connect/{token}")).status_code == 410
        assert linker.links == []

    async def test_a_composio_outage_spends_the_link(
        self, http: AsyncClient, db: Database, linker: FakeConnectLinker
    ) -> None:
        """Spent before Composio is called, so two taps race to one redirect. The user asks
        the bot again, and the connect step issues a fresh link because none is left unused."""
        tenant_id = await seed_tenant(db)
        url = f"/connect/{await seed_link(db, tenant_id)}"
        linker.failure = CalendarProviderUnavailableError("down")
        first = await http.post(url)
        assert first.status_code == 503
        assert_page(first, "unavailable_new_link", "he", "en")
        assert_hardened(first)
        linker.failure = None
        assert (await http.post(url)).status_code == 410
