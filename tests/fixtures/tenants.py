"""Settings, numbers and helpers for the onboarding tests.

Tenant tables are under FORCE RLS, so even the owner sees nothing without the GUC. These
helpers read through ``Database.tenant_session``, the way the application does, and find a
tenant through the ``resolve_tenant`` definer function.
"""

from __future__ import annotations

import re
from typing import Final
from uuid import UUID

from pydantic import SecretStr
from sqlalchemy import select, update

from personal_organizer.core.types import TenantId
from personal_organizer.db.engine import Database
from personal_organizer.db.models import Message, OnboardingLink, Tenant
from personal_organizer.db.models.tenant import NETWORK_WHATSAPP
from personal_organizer.db.repositories.tenants import (
    activate,
    create_tenant,
    get_tenant,
    resolve_tenant,
    set_onboarding_step,
)
from personal_organizer.onboarding.tokens import LINK_SALT, TokenPayload, verify
from personal_organizer.settings import ComposioSettings, OnboardingSettings, Settings
from tests.fixtures.payloads import SENDER_PHONE

BASE_URL: Final = "https://po.example.test"
LINK_SECRET: Final = "test-link-secret-" + "x" * 32
IL_PHONE: Final = "+972501234567"
US_PHONE: Final = "+12025550123"
#: ``SENDER_PHONE`` is Dutch: its guess is Europe/Amsterdam.
NL_PHONE: Final = SENDER_PHONE

_LINK: Final = re.compile(re.escape(BASE_URL) + r"/connect/(\S+)")


def with_onboarding(settings: Settings) -> Settings:
    """``settings`` with Composio on, plus everything its validator would demand."""
    return settings.model_copy(
        update={
            "app": settings.app.model_copy(update={"public_base_url": BASE_URL}),
            "composio": ComposioSettings(
                enabled=True,
                api_key=SecretStr("test-composio-key"),
                calendar_auth_config_id="ac_test",
            ),
            "onboarding": OnboardingSettings(link_secret=SecretStr(LINK_SECRET)),
        }
    )


async def new_tenant(
    db: Database,
    *,
    phone: str | None = NL_PHONE,
    user_id: str | None = None,
    language: str = "en",
    status: str = "onboarding",
    step: str | None = "zone",
) -> UUID:
    key = f"uid:{user_id}" if user_id else f"tel:{phone}"
    async with db.system_session() as session:
        tenant_id = await create_tenant(
            session, network=NETWORK_WHATSAPP, external_id=key, phone=phone, language=language
        )
    async with db.tenant_session(TenantId(tenant_id)) as session:
        if status == "active":
            await activate(session, tenant_id)
        elif status != "onboarding":
            await session.execute(
                update(Tenant).where(Tenant.id == tenant_id).values(status=status)
            )
        if status != "active" and step != "zone":
            await set_onboarding_step(session, tenant_id, step)
    return tenant_id


async def tenant_by_phone(db: Database, phone: str) -> Tenant | None:
    async with db.system_session() as session:
        tenant_id = await resolve_tenant(
            session, network=NETWORK_WHATSAPP, external_id=f"tel:{phone}"
        )
    if tenant_id is None:
        return None
    async with db.tenant_session(TenantId(tenant_id)) as session:
        return await get_tenant(session, tenant_id)


async def messages_of(db: Database, tenant_id: UUID) -> list[Message]:
    async with db.tenant_session(TenantId(tenant_id)) as session:
        return list(await session.scalars(select(Message).order_by(Message.created_at)))


async def links_of(db: Database, tenant_id: UUID) -> list[OnboardingLink]:
    async with db.tenant_session(TenantId(tenant_id)) as session:
        return list(
            await session.scalars(select(OnboardingLink).order_by(OnboardingLink.created_at))
        )


def link_payload(text: str) -> TokenPayload:
    """The verified payload of the one connect link in ``text``. Fails the test if there is none."""
    found = _LINK.search(text)
    assert found is not None, "no connect link in the message"
    payload = verify(found.group(1), secret=LINK_SECRET, salt=LINK_SALT, max_age_s=3600)
    assert payload is not None, "the link does not verify"
    return payload
