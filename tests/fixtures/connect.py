"""Shared pieces for the connect tests: settings, a fake Composio, tokens and seeders.

``FakeConnectLinker`` stands in for ``ComposioConnector`` behind the ``ConnectLinker``
protocol. ``tests/unit/test_composio_connector.py`` runs the real SDK. Every other connect
test uses this.
"""

from __future__ import annotations

import html
import secrets
from datetime import UTC, datetime, timedelta
from typing import Final
from uuid import UUID, uuid4

import httpx
from fastapi import FastAPI
from pydantic import SecretStr
from sqlalchemy import insert, update

from personal_organizer.api.app import create_app
from personal_organizer.api.pages import SECURITY_HEADERS
from personal_organizer.core.errors import CalendarProviderError, CalendarProviderRejectedError
from personal_organizer.core.types import TenantId
from personal_organizer.db.engine import Database
from personal_organizer.db.models.channel import ChannelInbox
from personal_organizer.db.models.tenant import NETWORK_WHATSAPP, Tenant
from personal_organizer.db.repositories.links import create_link
from personal_organizer.db.repositories.messages import record_inbound
from personal_organizer.db.repositories.tenants import (
    activate,
    create_tenant,
    set_onboarding_step,
)
from personal_organizer.interfaces.calendar import ACCOUNT_ACTIVE, ConnectedAccount
from personal_organizer.onboarding.page_text import page_text
from personal_organizer.onboarding.tokens import LINK_SALT, STATE_SALT, TokenPayload, sign
from personal_organizer.settings import ComposioSettings, OnboardingSettings, Settings

BASE_URL: Final = "https://po.test"
AUTH_CONFIG_ID: Final = "ac_test"
LINK_SECRET: Final = "connect-test-link-secret-0123456789abcdef"
OTHER_SECRET: Final = "someone-elses-link-secret-0123456789abcdef"
REDIRECT_URL: Final = "https://connect.composio.test/link/ln_test"
ACCOUNT_ID: Final = "ca_test1"
PHONE: Final = "+972501234567"

#: Everything Settings requires once COMPOSIO__ENABLED is true.
CONNECT_ENV: Final[dict[str, str]] = {
    "COMPOSIO__ENABLED": "true",
    "COMPOSIO__API_KEY": "composio-test-key",
    "COMPOSIO__CALENDAR_AUTH_CONFIG_ID": AUTH_CONFIG_ID,
    "APP__PUBLIC_BASE_URL": BASE_URL,
    "ONBOARDING__LINK_SECRET": LINK_SECRET,
}

#: Every table a connect test writes. Tenants cascade to identities, links, connections and
#: messages. Run as the owner: TRUNCATE is not subject to RLS.
TRUNCATE_CONNECT_TABLES: Final = (
    "TRUNCATE tenants, channel_outbox, channel_inbox, procrastinate_jobs CASCADE"
)


def with_connect(settings: Settings) -> Settings:
    """``settings`` (the real database's) with Composio and onboarding links switched on."""
    return settings.model_copy(
        update={
            "app": settings.app.model_copy(update={"public_base_url": BASE_URL}),
            "composio": ComposioSettings(
                enabled=True,
                api_key=SecretStr("composio-test-key"),
                calendar_auth_config_id=AUTH_CONFIG_ID,
            ),
            "onboarding": OnboardingSettings(link_secret=SecretStr(LINK_SECRET)),
        }
    )


class FakeConnectLinker:
    """Records every call. ``accounts`` is what Composio's API says about each account;
    ``failure``, while set, is raised by both methods."""

    def __init__(self) -> None:
        self.links: list[dict[str, str]] = []
        self.lookups: list[str] = []
        self.accounts: dict[str, ConnectedAccount] = {}
        self.failure: CalendarProviderError | None = None

    async def link(self, *, user_id: str, auth_config_id: str, callback_url: str) -> str:
        self.links.append(
            {"user_id": user_id, "auth_config_id": auth_config_id, "callback_url": callback_url}
        )
        if self.failure is not None:
            raise self.failure
        return REDIRECT_URL

    async def get_account(self, connected_account_id: str) -> ConnectedAccount:
        self.lookups.append(connected_account_id)
        if self.failure is not None:
            raise self.failure
        try:
            return self.accounts[connected_account_id]
        except KeyError:
            raise CalendarProviderRejectedError(404) from None


def account(
    tenant_id: UUID,
    *,
    account_id: str = ACCOUNT_ID,
    user_id: str | None = None,
    auth_config_id: str = AUTH_CONFIG_ID,
    status: str = ACCOUNT_ACTIVE,
) -> ConnectedAccount:
    """An account as Composio would describe it, by default this tenant's and ACTIVE."""
    return ConnectedAccount(
        id=account_id,
        user_id=user_id if user_id is not None else str(tenant_id),
        auth_config_id=auth_config_id,
        status=status,
    )


def link_token(
    tenant_id: UUID | None = None, *, nonce: str = "nonce-1", secret: str = LINK_SECRET
) -> str:
    payload = TokenPayload(tenant_id=tenant_id or uuid4(), nonce=nonce)
    return sign(payload, secret=secret, salt=LINK_SALT)


def state_token(
    tenant_id: UUID | None = None, *, nonce: str = "nonce-1", secret: str = LINK_SECRET
) -> str:
    payload = TokenPayload(tenant_id=tenant_id or uuid4(), nonce=nonce)
    return sign(payload, secret=secret, salt=STATE_SALT)


def connect_app(
    settings: Settings, *, db: object, linker: FakeConnectLinker, queue: object
) -> FastAPI:
    """The api with stubbed state and no lifespan, as ``tests/api/conftest.py`` builds it."""
    application = create_app(settings)
    application.state.db = db
    application.state.procrastinate = queue
    application.state.connect_linker = linker
    return application


def assert_hardened(response: httpx.Response) -> None:
    for name, value in SECURITY_HEADERS.items():
        assert response.headers[name] == value


def assert_page(response: httpx.Response, key: str, *languages: str) -> None:
    """The page ``key`` was rendered, in each of ``languages``."""
    text = html.unescape(response.text)
    for language in languages:
        assert page_text(key, language)["title"] in text


async def seed_tenant(
    db: Database,
    *,
    phone: str | None = PHONE,
    language: str = "he",
    step: str | None = "connect",
    external_id: str | None = None,
) -> UUID:
    """An onboarding tenant at ``step``. Pass ``external_id`` when ``phone`` is None."""
    async with db.system_session() as session:
        tenant_id = await create_tenant(
            session,
            network=NETWORK_WHATSAPP,
            external_id=external_id or f"tel:{phone}",
            phone=phone,
            language=language,
        )
    async with db.tenant_session(TenantId(tenant_id)) as session:
        await set_onboarding_step(session, tenant_id, step)
    return tenant_id


async def make_active(db: Database, tenant_id: UUID) -> None:
    async with db.tenant_session(TenantId(tenant_id)) as session:
        await activate(session, tenant_id)


async def suspend(db: Database, tenant_id: UUID) -> None:
    async with db.tenant_session(TenantId(tenant_id)) as session:
        await session.execute(
            update(Tenant).where(Tenant.id == tenant_id).values(status="suspended")
        )


async def seed_link(db: Database, tenant_id: UUID, *, expires_at: datetime | None = None) -> str:
    """A stored, unused link row and the token that names it."""
    nonce = secrets.token_urlsafe(16)
    async with db.tenant_session(TenantId(tenant_id)) as session:
        await create_link(
            session,
            tenant_id,
            nonce=nonce,
            expires_at=expires_at or datetime.now(UTC) + timedelta(minutes=15),
        )
    return link_token(tenant_id, nonce=nonce)


async def seed_inbound(
    db: Database, tenant_id: UUID, *, channel: str, sent_at: datetime, phone: str = PHONE
) -> None:
    """An inbound message from the tenant on ``channel``: the inbox row and its D7 copy."""
    async with db.system_session() as session:
        inbox_id = await session.scalar(
            insert(ChannelInbox)
            .values(
                channel=channel,
                provider_message_id=f"msg.{uuid4().hex}",
                sender_key=f"tel:{phone}",
                sender_phone=phone,
                message_type="text",
                body=None,
                sent_at=sent_at,
            )
            .returning(ChannelInbox.id)
        )
    assert inbox_id is not None
    async with db.tenant_session(TenantId(tenant_id)) as session:
        await record_inbound(
            session,
            tenant_id,
            inbox_id=inbox_id,
            channel=channel,
            message_type="text",
            body="hello",
            sent_at=sent_at,
        )
