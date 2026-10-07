from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from personal_organizer.core.types import TenantId
from personal_organizer.db.engine import Database
from personal_organizer.db.repositories.links import consume_link
from personal_organizer.onboarding.links import issue_or_reuse_link
from personal_organizer.onboarding.tokens import TokenPayload
from personal_organizer.settings import Settings
from tests.fixtures.tenants import BASE_URL, IL_PHONE, link_payload, links_of, new_tenant

pytestmark = [pytest.mark.db, pytest.mark.usefixtures("clean_tenant_tables")]

NOW = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)


@pytest.fixture
async def db(onboarding_settings: Settings, owner_conn: Any) -> AsyncIterator[Database]:
    database = Database(onboarding_settings)
    try:
        yield database
    finally:
        await database.dispose()


async def test_issues_a_signed_single_use_link(db: Database, onboarding_settings: Settings) -> None:
    tenant_id = await new_tenant(db)
    url = await issue_or_reuse_link(db, tenant_id, settings=onboarding_settings, now=NOW)

    assert url.startswith(f"{BASE_URL}/connect/")
    [link] = await links_of(db, tenant_id)
    assert link_payload(url) == TokenPayload(tenant_id=tenant_id, nonce=link.nonce)
    ttl = timedelta(seconds=onboarding_settings.onboarding.link_ttl_s)
    assert link.expires_at == NOW + ttl
    assert link.used_at is None


async def test_reuses_the_latest_usable_link(db: Database, onboarding_settings: Settings) -> None:
    tenant_id = await new_tenant(db)
    first = await issue_or_reuse_link(db, tenant_id, settings=onboarding_settings, now=NOW)
    second = await issue_or_reuse_link(db, tenant_id, settings=onboarding_settings, now=NOW)
    assert link_payload(first).nonce == link_payload(second).nonce
    assert len(await links_of(db, tenant_id)) == 1


async def test_a_used_link_is_replaced(db: Database, onboarding_settings: Settings) -> None:
    tenant_id = await new_tenant(db)
    first = await issue_or_reuse_link(db, tenant_id, settings=onboarding_settings, now=NOW)
    async with db.tenant_session(TenantId(tenant_id)) as session:
        assert await consume_link(session, tenant_id, nonce=link_payload(first).nonce, now=NOW)

    second = await issue_or_reuse_link(db, tenant_id, settings=onboarding_settings, now=NOW)
    assert link_payload(second).nonce != link_payload(first).nonce
    assert len(await links_of(db, tenant_id)) == 2


@pytest.mark.parametrize(("elapsed", "reused"), [(0.4, True), (0.6, False), (1.5, False)])
async def test_a_link_past_half_its_life_is_replaced(
    db: Database, onboarding_settings: Settings, elapsed: float, reused: bool
) -> None:
    """Review Focus 2: a reused link must leave the user time to switch apps and sign in."""
    tenant_id = await new_tenant(db)
    ttl = onboarding_settings.onboarding.link_ttl_s
    first = await issue_or_reuse_link(db, tenant_id, settings=onboarding_settings, now=NOW)
    later = NOW + timedelta(seconds=ttl * elapsed)
    second = await issue_or_reuse_link(db, tenant_id, settings=onboarding_settings, now=later)
    assert (link_payload(first).nonce == link_payload(second).nonce) is reused


async def test_links_are_per_tenant(db: Database, onboarding_settings: Settings) -> None:
    mine, theirs = await new_tenant(db), await new_tenant(db, phone=IL_PHONE)
    await issue_or_reuse_link(db, mine, settings=onboarding_settings, now=NOW)
    url = await issue_or_reuse_link(db, theirs, settings=onboarding_settings, now=NOW)
    assert link_payload(url).tenant_id == theirs
    assert len(await links_of(db, theirs)) == 1


async def test_needs_the_public_url_and_the_secret(db: Database, db_settings: Settings) -> None:
    tenant_id = await new_tenant(db)
    bare = db_settings.model_copy(
        update={"app": db_settings.app.model_copy(update={"public_base_url": None})}
    )
    with pytest.raises(RuntimeError, match="APP__PUBLIC_BASE_URL"):
        await issue_or_reuse_link(db, tenant_id, settings=bare, now=NOW)
