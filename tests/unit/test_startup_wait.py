"""``Database.wait_ready`` -- the startup gate.

The behaviour under test is a distinction, not a delay: a connection that is not there
*yet* is worth waiting for, and a password that is wrong never will be. Getting that
backwards in either direction is a production outage. Too impatient and a cold private
network crash-loops the service; too patient and a typo in a DSN takes the whole startup
budget to report, once per restart.

No database is needed. ``check`` is the seam -- it is what talks to Postgres, and these
tests replace it.
"""

from __future__ import annotations

from typing import Any

import asyncpg
import pytest
from sqlalchemy.exc import OperationalError

from personal_organizer.db.engine import Database
from personal_organizer.settings import Settings


class _Probe:
    """Stands in for ``Database.check``: fails ``failures`` times, then succeeds."""

    def __init__(self, error: BaseException, *, failures: int) -> None:
        self.error = error
        self.failures = failures
        self.calls = 0

    async def __call__(self, **_: Any) -> None:
        self.calls += 1
        if self.calls <= self.failures:
            raise self.error


def _database(settings: Settings, probe: _Probe) -> Database:
    database = Database(settings)
    database.check = probe  # type: ignore[method-assign]
    return database


def _wrapped(orig: BaseException) -> OperationalError:
    """An asyncpg error as SQLAlchemy delivers it -- wrapped, with the original under `orig`."""
    return OperationalError("SELECT 1", {}, orig)


async def test_waits_out_a_network_that_is_not_up_yet(settings: Settings) -> None:
    probe = _Probe(TimeoutError(), failures=3)
    await _database(settings, probe).wait_ready(budget=5.0, initial_backoff=0.001)
    assert probe.calls == 4


async def test_gives_up_after_the_budget_and_reraises_the_last_error(
    settings: Settings,
) -> None:
    probe = _Probe(TimeoutError(), failures=1_000)
    with pytest.raises(TimeoutError):
        await _database(settings, probe).wait_ready(budget=0.05, initial_backoff=0.001)
    assert probe.calls > 1, "the budget should cover more than one attempt"


@pytest.mark.parametrize(
    "orig",
    [
        asyncpg.InvalidPasswordError("password authentication failed"),
        asyncpg.InvalidAuthorizationSpecificationError('role "app_user" does not exist'),
        asyncpg.InvalidCatalogNameError('database "railway" does not exist'),
    ],
    ids=["password", "role", "database"],
)
async def test_a_misconfiguration_is_raised_on_the_first_attempt(
    settings: Settings, orig: BaseException
) -> None:
    """Waiting cannot fix any of these, and the eager check exists to say so immediately."""
    probe = _Probe(_wrapped(orig), failures=1_000)
    with pytest.raises(OperationalError):
        await _database(settings, probe).wait_ready(budget=30.0, initial_backoff=0.001)
    assert probe.calls == 1


async def test_a_bare_asyncpg_error_is_classified_too(settings: Settings) -> None:
    """Not everything arrives wrapped -- `po-db` and the tests use asyncpg directly."""
    probe = _Probe(asyncpg.InvalidPasswordError("nope"), failures=1_000)
    with pytest.raises(asyncpg.InvalidPasswordError):
        await _database(settings, probe).wait_ready(budget=30.0, initial_backoff=0.001)
    assert probe.calls == 1


async def test_succeeds_without_waiting_when_the_database_is_already_there(
    settings: Settings,
) -> None:
    probe = _Probe(TimeoutError(), failures=0)
    await _database(settings, probe).wait_ready(budget=0.0, initial_backoff=0.001)
    assert probe.calls == 1
