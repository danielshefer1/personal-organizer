"""``po-db``'s connection -- the pre-deploy command's first contact with the database.

It runs in a fresh container on Railway, so it meets the same cold private network the api
does at startup, and a single attempt failed every production deploy. These pin the same
distinction ``test_startup_wait`` pins for the api: wait out a network that is not up yet,
report a misconfiguration at once.

No database is needed: ``asyncpg.connect`` is the seam.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import asyncpg
import pytest

from personal_organizer.db import bootstrap
from personal_organizer.db.roles import DatabaseRole
from personal_organizer.settings import Settings


class _Connect:
    """Stands in for ``asyncpg.connect``: fails ``failures`` times, then returns a sentinel."""

    def __init__(self, error: BaseException, *, failures: int) -> None:
        self.error = error
        self.failures = failures
        self.calls: list[dict[str, Any]] = []
        self.connection = object()

    async def __call__(self, dsn: str, **kwargs: Any) -> object:
        self.calls.append({"dsn": dsn, **kwargs})
        if len(self.calls) <= self.failures:
            raise self.error
        return self.connection


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    async def sleep(_: float) -> None:
        return None

    monkeypatch.setattr("personal_organizer.db.engine.asyncio.sleep", sleep)


def _patch(monkeypatch: pytest.MonkeyPatch, connect: _Connect) -> None:
    monkeypatch.setattr("personal_organizer.db.bootstrap.asyncpg.connect", connect)


async def test_waits_out_a_network_that_is_not_up_yet(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    connect = _Connect(TimeoutError(), failures=2)
    _patch(monkeypatch, connect)

    conn = await bootstrap._connect(settings, DatabaseRole.BOOTSTRAP)

    assert conn is connect.connection
    assert len(connect.calls) == 3


async def test_gives_up_after_the_budget(
    settings_factory: Callable[..., Settings], monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = settings_factory(DATABASE__STARTUP_TIMEOUT="0")
    connect = _Connect(TimeoutError(), failures=1_000)
    _patch(monkeypatch, connect)

    with pytest.raises(TimeoutError):
        await bootstrap._connect(settings, DatabaseRole.BOOTSTRAP)
    assert len(connect.calls) == 1


async def test_a_wrong_password_is_raised_on_the_first_attempt(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    connect = _Connect(asyncpg.InvalidPasswordError("nope"), failures=1_000)
    _patch(monkeypatch, connect)

    with pytest.raises(asyncpg.InvalidPasswordError):
        await bootstrap._connect(settings, DatabaseRole.BOOTSTRAP)
    assert len(connect.calls) == 1


async def test_one_attempt_cannot_spend_the_whole_budget(
    settings_factory: Callable[..., Settings], monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = settings_factory(DATABASE__CONNECT_TIMEOUT="60")
    connect = _Connect(TimeoutError(), failures=0)
    _patch(monkeypatch, connect)

    await bootstrap._connect(settings, DatabaseRole.BOOTSTRAP)

    assert connect.calls[0]["timeout"] == bootstrap.ATTEMPT_TIMEOUT_S
    assert connect.calls[0]["dsn"].startswith("postgresql://postgres:")
