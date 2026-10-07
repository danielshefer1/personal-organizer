"""``tenants.create_tenant`` refuses a NULL answer from the SQL function."""

from __future__ import annotations

from typing import Any, cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from personal_organizer.db.repositories import tenants


class _NullResult:
    def scalar_one(self) -> None:
        return None


class _Session:
    async def execute(self, _stmt: object) -> _NullResult:
        return _NullResult()


async def test_create_tenant_raises_when_the_function_returns_null() -> None:
    session = cast("AsyncSession", cast("Any", _Session()))
    with pytest.raises(RuntimeError, match="no tenant id") as caught:
        await tenants.create_tenant(
            session, network="whatsapp", external_id="tel:+972501234567", phone=None, language="he"
        )
    assert "972" not in str(caught.value)
