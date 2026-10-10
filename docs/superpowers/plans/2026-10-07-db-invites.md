# DB-backed Invites and `po-admin` Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Invite people to the circle from a CLI, with no redeploy: an `invites` table the
gate reads beside `WHATSAPP__ALLOWED_PHONES`, and `po-admin invite / revoke / suspend /
unsuspend / list`.

**Architecture:** `invites` is a plain table outside RLS, like `channel_inbox`, reached
through `system_session` as `app_user`. Both gates ask one function, `is_invited`, which
checks the env list first and the table second. `enrol` marks the invite used in the same
transaction that creates the tenant. `po-admin` is an argparse CLI, like `po-db`, that runs
as `app_user` and prints operator output to stdout, never to structlog.

**Tech Stack:** Python 3.13, SQLAlchemy 2 async (asyncpg), Alembic (hand-written
migrations), pydantic-settings, structlog, pytest + pytest-asyncio against local Postgres 16
(`docker compose up -d`), uv.

**Spec:** `docs/superpowers/specs/2026-10-07-db-invites-design.md` — read it first.

## Global Constraints

- Work on branch `invites/db-invites`, created from `invites/0-spec` (which holds the spec).
- Run everything through `uv run`. Before each commit run `uv run ruff format . && uv run ruff check . && uv run mypy` (format rewrites; the plan's code is not guaranteed to be line-length clean), plus the task's tests.
- pytest runs with `asyncio_mode = "auto"`. A module whose `pytestmark` uses async fixtures (`clean_channel_tables`, …) must contain only `async def` tests; synchronous tests go in `tests/unit/`.
- DB tests need local Postgres: `docker compose up -d && uv run po-db bootstrap && uv run alembic upgrade head`. They skip without one — a skip is **not** a pass; make sure they run.
- Migrations are hand-written and must match the models; `tests/db/test_models_match_migrations.py` is the arbiter.
- No phone number, note, or link in any log line. Log `sender=f"tel:{phone}"` (the redaction processor turns the `sender` key into `sender_hash`) and `tenant_id`; nothing else that identifies a person.
- Operator output goes through a `_out(line)` that writes to `sys.stdout`, exactly as `po-gowa` does.
- Refusals print one line and exit 1. An invalid number prints `not an E.164 number, e.g. +972501234567` and never echoes the input.
- Commit messages end with:
  ```
  Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01BxyAfBrZWfpvo7PGh2yLoS
  ```
- Match the surrounding code: module docstrings that explain *why*, `#:` comments on constants, `__all__` at the bottom, `Final` for constants.

## Review Focus

1. **Numbers as people type them** (`+972 50-123-4567`, `00972501234567`, `0501234567`): the first two must be stored normalised as `+972501234567`; a local number with no country code must be refused, not stored. → Task 3, `TestNumbers`.
2. **Inviting someone who is already a member, including a suspended one**: refuse with their status, and for `suspended` point at `unsuspend` rather than creating an invite the gate would never read. → Task 3, `test_a_member_is_not_invited_again` (parametrised over `active`/`suspended`).
3. **Two `po-admin invite` runs for the same number at once** (two terminals): one open invite, no `IntegrityError` traceback. → Task 1, `test_creating_twice_keeps_one_open_invite` (sequential) and `test_concurrent_creates_keep_one_open_invite`.
4. **An invitee's first message arrives stale** (redelivered after an outage): no tenant, and the invite stays **open** so their next message still onboards them. → Task 2, `test_a_stale_first_message_leaves_the_invite_open`.
5. **An invitee who first writes through Meta's number** (message carries a BSUID *and* the phone): invited by phone, enrolled, invite used. → Task 2, `test_an_invite_works_on_either_channel`.

---

## File Structure

| File | Responsibility |
|---|---|
| Create `src/personal_organizer/db/models/invite.py` | `Invite` ORM model, `OPEN_INVITE` predicate |
| Modify `src/personal_organizer/db/models/__init__.py` | register `Invite` |
| Create `alembic/versions/0005_invites.py` | the table, the partial unique index, `REVOKE DELETE` |
| Create `src/personal_organizer/db/repositories/invites.py` | every SQL statement on `invites` |
| Create `src/personal_organizer/messaging/invites.py` | `is_invited`: env list, then table |
| Modify `src/personal_organizer/messaging/inbound.py` | both gates ask `is_invited` |
| Modify `src/personal_organizer/messaging/tenancy.py` | `enrol` marks the invite used |
| Modify `src/personal_organizer/db/repositories/tenants.py` | `set_status` |
| Modify `src/personal_organizer/settings.py` | `OnboardingSettings.bot_phone` |
| Create `src/personal_organizer/admin/__init__.py`, `src/personal_organizer/admin/cli.py` | `po-admin` |
| Modify `pyproject.toml` | the `po-admin` script |
| Modify `tests/db/conftest.py` | `invites` in the tables cleaned between tests |
| Create `tests/db/test_invites_repository.py`, `tests/db/test_invites_gate.py`, `tests/db/test_admin_cli.py`, `tests/unit/test_admin_cli.py` | tests |
| Modify `tests/db/test_models_match_migrations.py`, `tests/db/test_migrations.py`, `tests/unit/test_settings.py` | tests |
| Create `docs/adr/0006-invites-in-the-database.md`; modify `docs/runbook-iteration-03.md`, `README.md`, `docs/plan-iteration-03.md`, `.env.example` | docs |

---

### Task 1: The `invites` table and its repository

**Files:**
- Create: `src/personal_organizer/db/models/invite.py`
- Modify: `src/personal_organizer/db/models/__init__.py`
- Create: `alembic/versions/0005_invites.py`
- Create: `src/personal_organizer/db/repositories/invites.py`
- Modify: `tests/db/conftest.py:76-77` (`_CHANNEL_TABLES`)
- Modify: `tests/db/test_models_match_migrations.py:51-62`
- Modify: `tests/db/test_migrations.py` (append a test)
- Create: `tests/db/test_invites_repository.py`

**Interfaces:**
- Consumes: `personal_organizer.db.base.Base`, the `tenants` table (FK target).
- Produces:
  - `personal_organizer.db.models.Invite` — columns `id: UUID`, `phone: str`, `note: str | None`, `invited_by_tenant_id: UUID | None`, `created_at: datetime`, `used_at: datetime | None`, `revoked_at: datetime | None`.
  - `personal_organizer.db.models.invite.OPEN_INVITE: Final[str] = "used_at IS NULL AND revoked_at IS NULL"`.
  - In `personal_organizer.db.repositories.invites`, all taking an `AsyncSession` first:
    - `async def has_open_invite(session, phone: str) -> bool`
    - `async def has_used_invite(session, phone: str) -> bool`
    - `async def create_invite(session, phone: str, *, note: str | None = None, invited_by_tenant_id: UUID | None = None) -> bool` — `True` if created, `False` if an open invite already existed.
    - `async def revoke_invite(session, phone: str) -> bool` — `True` if an open invite was revoked.
    - `async def mark_used(session, phone: str) -> bool` — `True` if an open invite was marked used.
    - `async def list_invites(session) -> list[Invite]` — newest first.

- [ ] **Step 1: Create the branch**

```bash
git switch -c invites/db-invites invites/0-spec
```

- [ ] **Step 2: Write the failing repository tests**

Create `tests/db/test_invites_repository.py`:

```python
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

    async def test_creating_twice_keeps_one_open_invite(self, db: Database, owner_conn: Any) -> None:
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
```

- [ ] **Step 3: Add `invites` to the cleaned tables**

In `tests/db/conftest.py`, change:

```python
#: Everything a messaging test writes. The queue is included because ingress defers into it.
_CHANNEL_TABLES = "channel_outbox, channel_inbox, procrastinate_jobs"
```

to:

```python
#: Everything a messaging test writes. The queue is included because ingress defers into it,
#: and ``invites`` because the gate reads it.
_CHANNEL_TABLES = "channel_outbox, channel_inbox, procrastinate_jobs, invites"
```

- [ ] **Step 4: Run the tests to verify they fail**

Run: `uv run pytest tests/db/test_invites_repository.py -v`
Expected: collection error — `ModuleNotFoundError: No module named 'personal_organizer.db.repositories.invites'`.

- [ ] **Step 5: Write the model**

Create `src/personal_organizer/db/models/invite.py`:

```python
"""Invites: who may start onboarding (ADR 0006).

Not a tenant table, deliberately: an invite exists before its tenant does, so like
``channel_inbox`` it has no ``TenantMixin``, no RLS, and is reached through
``system_session``. ``app_user`` may read, add and update rows but never delete them
(migration 0005): a used or revoked invite is the history ``po-admin list`` shows.

``WHATSAPP__ALLOWED_PHONES`` stays the fallback invite list beside this table, so the owner's
own number is invited even with the table empty (``messaging.invites``).
"""

from __future__ import annotations

from datetime import datetime
from typing import Final
from uuid import UUID

from sqlalchemy import CheckConstraint, ForeignKey, Index, Text, func, text
from sqlalchemy.orm import Mapped, mapped_column

from personal_organizer.db.base import Base

#: An invite nobody has used or revoked yet. At most one per number (the partial index).
OPEN_INVITE: Final = "used_at IS NULL AND revoked_at IS NULL"

_GEN_UUID = text("gen_random_uuid()")


class Invite(Base):
    __tablename__ = "invites"
    __table_args__ = (
        CheckConstraint("used_at IS NULL OR revoked_at IS NULL", name="used_or_revoked"),
        Index("uq_invites_open_phone", "phone", unique=True, postgresql_where=text(OPEN_INVITE)),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=_GEN_UUID)
    #: E.164, normalised by the caller with ``core.phone.normalise_e164``.
    phone: Mapped[str] = mapped_column(Text)
    #: The owner's label, e.g. "Mom". Shown by ``po-admin list``, never logged.
    note: Mapped[str | None] = mapped_column(Text)
    #: ``NULL``: the owner, from ``po-admin``. Invitations from chat will fill it.
    invited_by_tenant_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("tenants.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    #: Set by ``enrol``, in the transaction that creates their tenant.
    used_at: Mapped[datetime | None]
    revoked_at: Mapped[datetime | None]


__all__ = ["OPEN_INVITE", "Invite"]
```

In `src/personal_organizer/db/models/__init__.py`, add the import and the export (keep `__all__` sorted):

```python
from personal_organizer.db.models.channel import ChannelInbox, ChannelOutbox
from personal_organizer.db.models.invite import Invite
from personal_organizer.db.models.tenant import (
    CalendarConnection,
    Message,
    OnboardingLink,
    Tenant,
    TenantIdentity,
)

__all__ = [
    "CalendarConnection",
    "ChannelInbox",
    "ChannelOutbox",
    "Invite",
    "Message",
    "OnboardingLink",
    "Tenant",
    "TenantIdentity",
]
```

- [ ] **Step 6: Write the migration**

Create `alembic/versions/0005_invites.py`:

```python
"""Invites in the database: who may start onboarding (ADR 0006).

``invites`` is not a tenant table, for the reason ``channel_inbox`` is not: an invite exists
before its tenant does. No RLS, no ``TenantMixin``; the RLS suite derives its expectations
from the mixins, so it asserts exactly that.

Bootstrap's default privileges give ``app_user`` ``SELECT, INSERT, UPDATE, DELETE`` on every
table ``app_owner`` creates. Invites are history, so ``DELETE`` is taken back. The runtime
role's name comes from its DSN, not a constant, so the revoke is from whichever role holds
the privilege other than the owner.

Hand-written to match ``personal_organizer.db.models.invite``;
``tests/db/test_models_match_migrations.py`` fails if the two drift.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005_invites"
down_revision: str | None = "0004_tenants"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TZ = sa.DateTime(timezone=True)
_GEN_UUID = sa.text("gen_random_uuid()")

_REVOKE_DELETE = """
DO $$
DECLARE
    grantee_name text;
BEGIN
    FOR grantee_name IN
        SELECT DISTINCT r.rolname
        FROM pg_class c, aclexplode(c.relacl) a
        JOIN pg_roles r ON r.oid = a.grantee
        WHERE c.oid = 'public.invites'::regclass
          AND a.privilege_type = 'DELETE'
          AND r.rolname <> current_user
    LOOP
        EXECUTE format('REVOKE DELETE ON invites FROM %I', grantee_name);
    END LOOP;
END
$$
"""


def upgrade() -> None:
    op.create_table(
        "invites",
        sa.Column("id", sa.Uuid(), server_default=_GEN_UUID, nullable=False),
        sa.Column("phone", sa.Text(), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("invited_by_tenant_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", _TZ, server_default=sa.func.now(), nullable=False),
        sa.Column("used_at", _TZ, nullable=True),
        sa.Column("revoked_at", _TZ, nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_invites"),
        sa.ForeignKeyConstraint(
            ["invited_by_tenant_id"],
            ["tenants.id"],
            name="fk_invites_invited_by_tenant_id_tenants",
            ondelete="SET NULL",
        ),
        sa.CheckConstraint(
            "used_at IS NULL OR revoked_at IS NULL", name="ck_invites_used_or_revoked"
        ),
    )
    op.create_index(
        "uq_invites_open_phone",
        "invites",
        ["phone"],
        unique=True,
        postgresql_where=sa.text("used_at IS NULL AND revoked_at IS NULL"),
    )
    op.execute(_REVOKE_DELETE)


def downgrade() -> None:
    op.drop_index("uq_invites_open_phone", table_name="invites")
    op.drop_table("invites")
```

- [ ] **Step 7: Write the repository**

Create `src/personal_organizer/db/repositories/invites.py`:

```python
"""Invites: every statement on the ``invites`` table (ADR 0006).

Not a tenant table, so nothing here goes through ``scoped()``; callers use
``Database.system_session``. Every function takes the caller's session, so a change commits
with the caller's transaction: ``enrol`` marks an invite used in the one that creates the
tenant.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import and_, exists, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from personal_organizer.db.models.invite import Invite

_OPEN = and_(Invite.used_at.is_(None), Invite.revoked_at.is_(None))


async def has_open_invite(session: AsyncSession, phone: str) -> bool:
    found = await session.scalar(select(exists().where(Invite.phone == phone, _OPEN)))
    return bool(found)


async def has_used_invite(session: AsyncSession, phone: str) -> bool:
    found = await session.scalar(
        select(exists().where(Invite.phone == phone, Invite.used_at.is_not(None)))
    )
    return bool(found)


async def create_invite(
    session: AsyncSession,
    phone: str,
    *,
    note: str | None = None,
    invited_by_tenant_id: UUID | None = None,
) -> bool:
    """Open an invite for ``phone``. ``False`` if one is already open: the partial unique
    index decides, so two concurrent calls leave one invite and raise nothing."""
    created = await session.scalar(
        insert(Invite)
        .values(phone=phone, note=note, invited_by_tenant_id=invited_by_tenant_id)
        .on_conflict_do_nothing(index_elements=[Invite.phone], index_where=_OPEN)
        .returning(Invite.id)
    )
    return created is not None


async def _close_open(session: AsyncSession, phone: str, **values: object) -> bool:
    closed = await session.scalar(
        update(Invite).where(Invite.phone == phone, _OPEN).values(**values).returning(Invite.id)
    )
    return closed is not None


async def revoke_invite(session: AsyncSession, phone: str) -> bool:
    """Cancel the open invite. A used one is left alone: that person is a member, and
    cutting a member off is ``tenants.status`` (``po-admin suspend``), not this table."""
    return await _close_open(session, phone, revoked_at=func.now())


async def mark_used(session: AsyncSession, phone: str) -> bool:
    """Record that the open invite brought its tenant in. A re-run, a racing first message,
    or an env-listed number with no invite finds nothing open and changes nothing."""
    return await _close_open(session, phone, used_at=func.now())


async def list_invites(session: AsyncSession) -> list[Invite]:
    return list(await session.scalars(select(Invite).order_by(Invite.created_at.desc(), Invite.id)))


__all__ = [
    "create_invite",
    "has_open_invite",
    "has_used_invite",
    "list_invites",
    "mark_used",
    "revoke_invite",
]
```

- [ ] **Step 8: Migrate the local database and run the repository tests**

Run: `uv run alembic upgrade head && uv run pytest tests/db/test_invites_repository.py -v`
Expected: all PASS.

- [ ] **Step 9: Cover the table in the schema tests**

In `tests/db/test_models_match_migrations.py`, replace `test_channel_ledgers_are_not_tenant_tables` with:

```python
async def test_tables_written_before_a_tenant_is_known_have_no_rls(app_conn: Any) -> None:
    """The channel ledgers and ``invites`` exist before their tenant does, so no RLS --
    which also means a stray policy on them would silently hide every row from the worker."""
    rows = await app_conn.fetch(
        "SELECT relname, relrowsecurity FROM pg_class "
        "WHERE relname IN ('channel_inbox', 'channel_outbox', 'invites')"
    )
    assert {row["relname"]: row["relrowsecurity"] for row in rows} == {
        "channel_inbox": False,
        "channel_outbox": False,
        "invites": False,
    }
```

Append to `tests/db/test_migrations.py`:

```python
_INVITES_SQL = "SELECT to_regclass('public.invites')::text"


async def test_the_invites_downgrade_round_trips(owner_conn: Any) -> None:
    """0005 down and up again; the up must re-revoke DELETE (checked by the repository suite,
    which runs at head)."""
    config = _config()
    try:
        command.downgrade(config, "0004_tenants")
        assert await owner_conn.fetchval(_INVITES_SQL) is None
        command.upgrade(config, "head")
        assert await owner_conn.fetchval(_INVITES_SQL) == "invites"
    finally:
        command.upgrade(config, "head")
```

- [ ] **Step 10: Run the schema tests**

Run: `uv run pytest tests/db/test_models_match_migrations.py tests/db/test_migrations.py tests/db/test_rls.py tests/db/test_invites_repository.py -v`
Expected: all PASS. (`test_the_tenants_downgrade_round_trips` now also runs 0005's downgrade on its way to 0003; it must still pass.)

- [ ] **Step 11: Lint, type-check, commit**

```bash
uv run ruff format . && uv run ruff check . && uv run mypy
git add src/personal_organizer/db/models/invite.py src/personal_organizer/db/models/__init__.py \
  alembic/versions/0005_invites.py src/personal_organizer/db/repositories/invites.py \
  tests/db/conftest.py tests/db/test_invites_repository.py \
  tests/db/test_models_match_migrations.py tests/db/test_migrations.py
git commit -m "feat: the invites table, outside RLS, with no DELETE for the app

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01BxyAfBrZWfpvo7PGh2yLoS"
```

---

### Task 2: The gate reads invites; `enrol` uses them

**Files:**
- Create: `src/personal_organizer/messaging/invites.py`
- Modify: `src/personal_organizer/messaging/inbound.py` (module docstring table; `_allowlist_gate`; `_tenant_gate`)
- Modify: `src/personal_organizer/messaging/tenancy.py` (`enrol`)
- Create: `tests/db/test_invites_gate.py`

**Interfaces:**
- Consumes: `has_open_invite(session, phone) -> bool`, `mark_used(session, phone) -> bool`, `create_invite(...)`, `revoke_invite(...)` from Task 1.
- Produces: `personal_organizer.messaging.invites.is_invited(db: Database, phone: str | None, env_list: frozenset[str]) -> bool`. Log event `invite.used` with `tenant_id`.

- [ ] **Step 1: Write the failing gate tests**

Create `tests/db/test_invites_gate.py`:

```python
"""The gate with DB invites: an open invite onboards, a revoked one does not, and the env
list still works with an empty table (ADR 0006)."""

from __future__ import annotations

import asyncio
import io
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from structlog.testing import capture_logs

from personal_organizer.db.engine import Database
from personal_organizer.db.repositories.invites import create_invite, revoke_invite
from personal_organizer.messaging.inbound import handle_inbound
from personal_organizer.messaging.inbox import load_row
from personal_organizer.messaging.replies import INVITE_ONLY_TEXT
from personal_organizer.messaging.tenancy import enrol
from personal_organizer.observability.logging import configure_logging
from personal_organizer.settings import ComposioSettings, Settings
from tests.fixtures.channels import FakeOutbound, Spy, insert_inbox
from tests.fixtures.tenants import IL_PHONE, tenant_by_phone

pytestmark = [
    pytest.mark.db,
    pytest.mark.usefixtures("clean_channel_tables", "clean_tenant_tables"),
]


@pytest.fixture
async def db(onboarding_settings: Settings, owner_conn: Any) -> AsyncIterator[Database]:
    database = Database(onboarding_settings)
    try:
        yield database
    finally:
        await database.dispose()


async def _invite(db: Database, phone: str = IL_PHONE) -> None:
    async with db.system_session() as session:
        assert await create_invite(session, phone)


async def _invite_state(conn: Any, phone: str = IL_PHONE) -> list[tuple[bool, bool]]:
    """``(used, revoked)`` for every invite of ``phone``, oldest first."""
    rows = await conn.fetch(
        "SELECT used_at IS NOT NULL AS used, revoked_at IS NOT NULL AS revoked "
        "FROM invites WHERE phone = $1 ORDER BY created_at",
        phone,
    )
    return [(row["used"], row["revoked"]) for row in rows]


async def _say(
    db: Database,
    conn: Any,
    settings: Settings,
    *,
    phone: str = IL_PHONE,
    allowlist: frozenset[str] = frozenset(),
    channel: str = "gowa",
    user_id: str | None = None,
    sent_at: datetime | None = None,
) -> tuple[str | None, FakeOutbound, UUID]:
    outbound = FakeOutbound(name=channel)
    inbox_id = await insert_inbox(
        conn, phone=phone, user_id=user_id, body="hello", channel=channel, sent_at=sent_at
    )
    disposition = await handle_inbound(
        inbox_id,
        db=db,
        channels={channel: outbound}.__getitem__,
        allowlist=allowlist,
        on_allowed=Spy(),
        settings=settings,
    )
    return disposition, outbound, inbox_id


class TestTenantGate:
    async def test_an_open_invite_onboards_and_is_used(
        self, db: Database, owner_conn: Any, onboarding_settings: Settings
    ) -> None:
        await _invite(db)
        disposition, _, _ = await _say(db, owner_conn, onboarding_settings)
        assert disposition == "onboarding"
        tenant = await tenant_by_phone(db, IL_PHONE)
        assert tenant is not None
        assert tenant.status == "onboarding"
        assert await _invite_state(owner_conn) == [(True, False)]

    async def test_a_revoked_invite_is_turned_away(
        self, db: Database, owner_conn: Any, onboarding_settings: Settings
    ) -> None:
        await _invite(db)
        async with db.system_session() as session:
            await revoke_invite(session, IL_PHONE)
        disposition, outbound, inbox_id = await _say(db, owner_conn, onboarding_settings)
        assert disposition == "stranger"
        assert [m.body for m in outbound.sent] == [INVITE_ONLY_TEXT]
        assert await tenant_by_phone(db, IL_PHONE) is None
        assert await owner_conn.fetchval(
            "SELECT body FROM channel_inbox WHERE id = $1", inbox_id
        ) is None

    async def test_the_env_list_still_invites_with_an_empty_table(
        self, db: Database, owner_conn: Any, onboarding_settings: Settings
    ) -> None:
        """The fallback: the owner's own number is never locked out by the table."""
        disposition, _, _ = await _say(
            db, owner_conn, onboarding_settings, allowlist=frozenset({IL_PHONE})
        )
        assert disposition == "onboarding"
        assert await _invite_state(owner_conn) == []

    async def test_an_env_listed_number_uses_its_open_invite_too(
        self, db: Database, owner_conn: Any, onboarding_settings: Settings
    ) -> None:
        await _invite(db)
        await _say(db, owner_conn, onboarding_settings, allowlist=frozenset({IL_PHONE}))
        assert await _invite_state(owner_conn) == [(True, False)]

    async def test_an_invite_works_on_either_channel(
        self, db: Database, owner_conn: Any, onboarding_settings: Settings
    ) -> None:
        """Review Focus 5: first message through Meta, carrying a BSUID and the phone."""
        await _invite(db)
        disposition, _, _ = await _say(
            db, owner_conn, onboarding_settings, channel="whatsapp", user_id="US.42"
        )
        assert disposition == "onboarding"
        assert await tenant_by_phone(db, IL_PHONE) is not None
        assert await _invite_state(owner_conn) == [(True, False)]

    async def test_a_stale_first_message_leaves_the_invite_open(
        self, db: Database, owner_conn: Any, onboarding_settings: Settings
    ) -> None:
        """Review Focus 4: redelivered after an outage, so no tenant -- and their next
        message must still find the invite."""
        await _invite(db)
        stale = datetime.now(UTC) - timedelta(hours=30)
        disposition, _, _ = await _say(db, owner_conn, onboarding_settings, sent_at=stale)
        assert disposition == "stale"
        assert await tenant_by_phone(db, IL_PHONE) is None
        assert await _invite_state(owner_conn) == [(False, False)]

        disposition, _, _ = await _say(db, owner_conn, onboarding_settings)
        assert disposition == "onboarding"


class TestComposioOff:
    async def test_a_db_invite_is_allowed_and_stays_open(
        self, db: Database, owner_conn: Any, db_settings: Settings
    ) -> None:
        """Iteration 02's gate creates no tenant, so nothing is used."""
        off = db_settings.model_copy(update={"composio": ComposioSettings()})
        await _invite(db)
        disposition, _, _ = await _say(db, owner_conn, off)
        assert disposition == "allowed"
        assert await _invite_state(owner_conn) == [(False, False)]

    async def test_without_an_invite_it_is_a_stranger(
        self, db: Database, owner_conn: Any, db_settings: Settings
    ) -> None:
        off = db_settings.model_copy(update={"composio": ComposioSettings()})
        disposition, _, _ = await _say(db, owner_conn, off)
        assert disposition == "stranger"


class TestEnrol:
    async def test_racing_first_messages_use_the_invite_once(
        self, db: Database, owner_conn: Any
    ) -> None:
        """One person, both channels at once: one tenant, one ``invite.used``."""
        for _ in range(5):
            await _invite(db)
            gowa = await load_row(db, await insert_inbox(owner_conn, phone=IL_PHONE))
            meta = await load_row(
                db,
                await insert_inbox(owner_conn, phone=IL_PHONE, user_id="US.7", channel="whatsapp"),
            )
            assert gowa is not None
            assert meta is not None
            with capture_logs() as logs:
                ids = await asyncio.gather(
                    enrol(db, gowa, language="en"), enrol(db, meta, language="en")
                )
            assert ids[0] == ids[1]
            assert await _invite_state(owner_conn) == [(True, False)]
            used = [entry for entry in logs if entry["event"] == "invite.used"]
            assert len(used) == 1
            assert set(used[0]) <= {"event", "log_level", "tenant_id"}
            await owner_conn.execute("TRUNCATE tenants, invites CASCADE")

    async def test_invite_used_is_logged_without_the_number(
        self, db: Database, owner_conn: Any, onboarding_settings: Settings
    ) -> None:
        stream = io.StringIO()
        configure_logging(onboarding_settings, stream=stream)
        await _invite(db)
        await _say(db, owner_conn, onboarding_settings)
        captured = stream.getvalue()
        assert "invite.used" in captured
        for leaked in (IL_PHONE, IL_PHONE.removeprefix("+")):
            assert leaked not in captured
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/db/test_invites_gate.py -v`
Expected: the invite-only tests FAIL (`test_an_open_invite_onboards_and_is_used` gets `"stranger"`), while `test_the_env_list_still_invites_with_an_empty_table` and `test_without_an_invite_it_is_a_stranger` already PASS.

- [ ] **Step 3: Write `is_invited`**

Create `src/personal_organizer/messaging/invites.py`:

```python
"""Is this number invited? The env list first, then the ``invites`` table (ADR 0006).

``WHATSAPP__ALLOWED_PHONES`` stays as the fallback list, so the owner's own number is invited
whatever the table says -- empty, or a mistaken revoke. It is checked first, so a listed
number costs no query.

Only a sender with no tenant is ever asked: identity wins over invites, as it did over the
env list. Cutting off a member is ``tenants.status`` (``po-admin suspend``).
"""

from __future__ import annotations

from personal_organizer.db.engine import Database
from personal_organizer.db.repositories.invites import has_open_invite


async def is_invited(db: Database, phone: str | None, env_list: frozenset[str]) -> bool:
    if phone is None:
        return False
    if phone in env_list:
        return True
    async with db.system_session() as session:
        return await has_open_invite(session, phone)


__all__ = ["is_invited"]
```

- [ ] **Step 4: Make both gates ask it**

In `src/personal_organizer/messaging/inbound.py`:

Add the import beside the other `personal_organizer.messaging` imports:

```python
from personal_organizer.messaging.invites import is_invited
```

In the module docstring, change the third row of the table and the paragraph after it:

```
no tenant, number invited             ``enrol``, then the first step (``onboarding``)
```

and after the table's closing line add:

```
"Invited" is :func:`~personal_organizer.messaging.invites.is_invited`: on
``WHATSAPP__ALLOWED_PHONES``, or holding an open invite in ``invites`` (ADR 0006).
```

In `_allowlist_gate`, replace:

```python
    allowed = row.sender_phone is not None and row.sender_phone in allowlist
```

with:

```python
    allowed = await is_invited(db, row.sender_phone, allowlist)
```

In `_tenant_gate`, replace:

```python
    tenant_id = await resolve_sender(db, row)
    invited = row.sender_phone is not None and row.sender_phone in allowlist
    if tenant_id is None and invited and not stale:
```

with:

```python
    tenant_id = await resolve_sender(db, row)
    # Asked only for a sender with no tenant: identity wins over invites.
    if tenant_id is None and not stale and await is_invited(db, row.sender_phone, allowlist):
```

- [ ] **Step 5: Mark the invite used in `enrol`**

In `src/personal_organizer/messaging/tenancy.py`, add the import:

```python
from personal_organizer.db.repositories.invites import mark_used
```

In `enrol`'s docstring, append:

```
    An open invite for the number is marked used in the transaction that creates the
    tenant (ADR 0006), so the two commit together; a re-run or a racing first message finds
    nothing open and logs nothing.
```

Replace:

```python
    async with db.system_session() as session:
        tenant_id = await create_tenant(
            session,
            network=network,
            external_id=first,
            phone=row.sender_phone,
            language=language,
        )
    rest = tuple(key for key in keys if key != first)
```

with:

```python
    async with db.system_session() as session:
        tenant_id = await create_tenant(
            session,
            network=network,
            external_id=first,
            phone=row.sender_phone,
            language=language,
        )
        used = row.sender_phone is not None and await mark_used(session, row.sender_phone)
    if used:
        log.info("invite.used", tenant_id=str(tenant_id))
    rest = tuple(key for key in keys if key != first)
```

- [ ] **Step 6: Run the gate tests and the existing messaging suites**

Run: `uv run pytest tests/db/test_invites_gate.py tests/db/test_onboarding.py tests/db/test_tenancy.py tests/db/test_handle_inbound.py -v`
Expected: all PASS. If `test_racing_first_messages_use_the_invite_once` shows two `invite.used`, the `UPDATE ... RETURNING` is not filtering on `_OPEN` — check Task 1's `_close_open`.

- [ ] **Step 7: Lint, type-check, commit**

```bash
uv run ruff format . && uv run ruff check . && uv run mypy
git add src/personal_organizer/messaging/invites.py src/personal_organizer/messaging/inbound.py \
  src/personal_organizer/messaging/tenancy.py tests/db/test_invites_gate.py
git commit -m "feat: the gate reads invites beside the env list; enrol marks them used

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01BxyAfBrZWfpvo7PGh2yLoS"
```

---

### Task 3: `po-admin invite`, `revoke`, `list`, and `ONBOARDING__BOT_PHONE`

**Files:**
- Modify: `src/personal_organizer/settings.py` (`OnboardingSettings`)
- Modify: `.env.example` (onboarding block)
- Modify: `tests/unit/test_settings.py` (`TestOnboardingLinks` → add tests)
- Create: `src/personal_organizer/admin/__init__.py`, `src/personal_organizer/admin/cli.py`
- Modify: `pyproject.toml` (`[project.scripts]`)
- Create: `tests/db/test_admin_cli.py`, `tests/unit/test_admin_cli.py`

**Interfaces:**
- Consumes: Task 1's repository; `resolve_tenant`, `get_tenant` from `db.repositories.tenants`; `normalise_e164` from `core.phone`.
- Produces:
  - `Settings.onboarding.bot_phone: str | None` — normalised E.164 or `None`.
  - In `personal_organizer.admin.cli`: `wa_link(bot_phone: str) -> str`; `async def invite(db, settings, raw_phone: str, *, note: str | None) -> int`; `async def revoke(db, settings, raw_phone: str) -> int`; `async def list_invites_command(db, settings) -> int`; `async def tenant_of(db, phone: str) -> Tenant | None`; `def parse_phone(raw: str) -> str | None` (prints the refusal itself); `def main(argv: list[str] | None = None) -> int`. Task 4 adds `suspend` / `unsuspend` to the same module and to `main`.

- [ ] **Step 1: Write the failing settings tests**

In `tests/unit/test_settings.py`, add to `class TestOnboardingLinks`:

```python
    def test_the_bot_phone_is_optional_and_normalised(self, settings_factory: Any) -> None:
        assert settings_factory().onboarding.bot_phone is None
        cfg = settings_factory(ONBOARDING__BOT_PHONE="+972 50-123-4567")
        assert cfg.onboarding.bot_phone == "+972501234567"

    def test_an_invalid_bot_phone_fails_boot_without_echoing_it(
        self, settings_factory: Any
    ) -> None:
        with pytest.raises(ValidationError, match="BOT_PHONE") as excinfo:
            settings_factory(ONBOARDING__BOT_PHONE="0501234567")
        assert "0501234567" not in str(excinfo.value)
```

Run: `uv run pytest tests/unit/test_settings.py -k bot_phone -v`
Expected: FAIL (`AttributeError: 'OnboardingSettings' object has no attribute 'bot_phone'`).

- [ ] **Step 2: Add the setting**

In `src/personal_organizer/settings.py`, inside `class OnboardingSettings`, after `link_ttl_s`:

```python
    #: The number invitees write to, E.164 -- today the GOWA SIM. Only ``po-admin invite``
    #: reads it, to print a ``wa.me`` link; unset, the invite is still recorded.
    bot_phone: str | None = None

    @field_validator("bot_phone")
    @classmethod
    def _bot_phone_is_e164(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        phone = normalise_e164(value)
        if phone is None:
            # Not echoed: hide_input_in_errors covers pydantic's part, and this is ours.
            msg = "ONBOARDING__BOT_PHONE is not an E.164 number"
            raise ValueError(msg)
        return phone
```

In `.env.example`, after `# ONBOARDING__LINK_TTL_S=900`, add:

```
# The bot's own number, for the wa.me link po-admin invite prints. E.164.
# ONBOARDING__BOT_PHONE=+972501234567
```

Run: `uv run pytest tests/unit/test_settings.py -v`
Expected: all PASS.

- [ ] **Step 3: Write the failing CLI tests**

Create `tests/db/test_admin_cli.py`:

```python
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
```

Create `tests/unit/test_admin_cli.py` (no database; synchronous):

```python
"""``po-admin`` pieces that need no database."""

from __future__ import annotations

import pytest

from personal_organizer.admin import cli


def test_the_link_has_no_prefilled_text() -> None:
    """D11: a prefilled "Hi" would make every invitee English."""
    assert cli.wa_link("+972531112222") == "https://wa.me/972531112222"


def test_a_missing_argument_is_a_usage_error() -> None:
    """argparse refuses before any settings are read or any connection is made."""
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["invite"])
    assert excinfo.value.code == 2
```

Run: `uv run pytest tests/db/test_admin_cli.py tests/unit/test_admin_cli.py -v`
Expected: collection error — `ModuleNotFoundError: No module named 'personal_organizer.admin'`.

- [ ] **Step 4: Write the CLI**

Create `src/personal_organizer/admin/__init__.py`:

```python
"""Operator tools: ``po-admin``."""
```

Create `src/personal_organizer/admin/cli.py`:

```python
"""``po-admin`` -- who is in the circle (ADR 0006).

``invite <phone> [--note TEXT]``
    Open an invite and print the ``wa.me`` link to forward. The bot never writes first: an
    unsolicited first message is what gets a QR-gateway number banned.
``revoke <phone>``
    Cancel an invite nobody has used. A used one belongs to a member: ``suspend`` them.
``suspend <phone>`` / ``unsuspend <phone>``
    Turn a member away, and back. Numbers on ``WHATSAPP__ALLOWED_PHONES`` cannot be
    suspended, so the owner cannot lock themselves out by a typo.
``list``
    Every invite, newest first, with the member's status for used ones.

Runs as ``app_user``, like the worker. On Railway: ``railway ssh --service worker
--environment staging``, then ``po-admin ...``. Operator output -- numbers, notes, links --
goes to stdout and never to structlog; log lines carry ``sender`` (emitted as
``sender_hash``) and ``tenant_id`` only.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from typing import Final

import structlog

from personal_organizer.core.phone import normalise_e164
from personal_organizer.core.types import TenantId
from personal_organizer.db.engine import Database
from personal_organizer.db.models.tenant import NETWORK_WHATSAPP, Tenant
from personal_organizer.db.repositories.invites import (
    create_invite,
    has_used_invite,
    list_invites,
    revoke_invite,
)
from personal_organizer.db.repositories.tenants import get_tenant, resolve_tenant
from personal_organizer.observability.logging import configure_logging
from personal_organizer.settings import Settings, get_settings

log = structlog.get_logger(__name__)

OK: Final = 0
REFUSED: Final = 1


def _out(line: str) -> None:
    sys.stdout.write(line + "\n")


def wa_link(bot_phone: str) -> str:
    """No ``?text=``: the tenant's language comes from their first words (D11)."""
    return f"https://wa.me/{bot_phone.removeprefix('+')}"


def parse_phone(raw: str) -> str | None:
    """The normalised number, or ``None`` after printing why. The input is never echoed."""
    phone = normalise_e164(raw)
    if phone is None:
        _out("not an E.164 number, e.g. +972501234567")
    return phone


async def tenant_of(db: Database, phone: str) -> Tenant | None:
    async with db.system_session() as session:
        tenant_id = await resolve_tenant(
            session, network=NETWORK_WHATSAPP, external_id=f"tel:{phone}"
        )
    if tenant_id is None:
        return None
    async with db.tenant_session(TenantId(tenant_id)) as session:
        return await get_tenant(session, tenant_id)


def _print_link(settings: Settings) -> None:
    bot_phone = settings.onboarding.bot_phone
    if bot_phone is None:
        _out("no ONBOARDING__BOT_PHONE; send them the bot's number yourself")
        return
    _out(f"send them: {wa_link(bot_phone)}")


async def invite(db: Database, settings: Settings, raw_phone: str, *, note: str | None) -> int:
    phone = parse_phone(raw_phone)
    if phone is None:
        return REFUSED
    tenant = await tenant_of(db, phone)
    if tenant is not None:
        hint = "; use unsuspend" if tenant.status == "suspended" else ""
        _out(f"already a member ({tenant.status}){hint}")
        return REFUSED
    async with db.system_session() as session:
        created = await create_invite(session, phone, note=note)
    if created:
        log.info("admin.invite.created", sender=f"tel:{phone}")
        _out(f"invited {phone}")
    else:
        _out(f"{phone} already has an open invite")
    _print_link(settings)
    return OK


async def revoke(db: Database, settings: Settings, raw_phone: str) -> int:
    del settings
    phone = parse_phone(raw_phone)
    if phone is None:
        return REFUSED
    async with db.system_session() as session:
        revoked = await revoke_invite(session, phone)
        used = not revoked and await has_used_invite(session, phone)
    if revoked:
        log.info("admin.invite.revoked", sender=f"tel:{phone}")
        _out(f"revoked the invite for {phone}")
        return OK
    _out("invite already used; use suspend" if used else "no open invite")
    return REFUSED


async def list_invites_command(db: Database, settings: Settings) -> int:
    del settings
    async with db.system_session() as session:
        invites = await list_invites(session)
    if not invites:
        _out("no invites")
        return OK
    for entry in invites:
        if entry.used_at is not None:
            state = "used"
            tenant = await tenant_of(db, entry.phone)
            member = tenant.status if tenant is not None else "gone"
        else:
            state = "revoked" if entry.revoked_at is not None else "open"
            member = "-"
        fields = [entry.phone, f"{state:<7}", f"{entry.created_at:%Y-%m-%d}", f"{member:<10}"]
        _out("  ".join([*fields, entry.note or ""]).rstrip())
    return OK


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="po-admin", description="who is in the circle")
    commands = parser.add_subparsers(dest="command", required=True)
    invited = commands.add_parser("invite", help="open an invite and print the wa.me link")
    invited.add_argument("phone", help="E.164, e.g. +972501234567")
    invited.add_argument("--note", help="your label for them, e.g. Mom")
    revoked = commands.add_parser("revoke", help="cancel an invite nobody has used")
    revoked.add_argument("phone")
    commands.add_parser("list", help="every invite, newest first")
    return parser


async def _run(args: argparse.Namespace, settings: Settings) -> int:
    db = Database(settings)
    try:
        if args.command == "invite":
            return await invite(db, settings, args.phone, note=args.note)
        if args.command == "revoke":
            return await revoke(db, settings, args.phone)
        return await list_invites_command(db, settings)
    finally:
        await db.dispose()


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    settings = get_settings()
    configure_logging(settings)
    return asyncio.run(_run(args, settings))


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
```

In `pyproject.toml`, under `[project.scripts]`, add after `po-gowa`:

```toml
po-admin = "personal_organizer.admin.cli:main"
```

Then: `uv sync` (so the script is installed).

- [ ] **Step 5: Run the CLI tests**

Run: `uv run pytest tests/db/test_admin_cli.py tests/unit/test_admin_cli.py -v`
Expected: all PASS. If `test_main_runs_a_command` fails with a settings error, your shell's `.env` lacks database URLs — the DB fixtures would skip in that case too; fix `.env` from `.env.example`.

- [ ] **Step 6: Lint, type-check, commit**

```bash
uv run ruff format . && uv run ruff check . && uv run mypy
git add src/personal_organizer/settings.py .env.example tests/unit/test_settings.py \
  src/personal_organizer/admin/ pyproject.toml uv.lock tests/db/test_admin_cli.py \
  tests/unit/test_admin_cli.py
git commit -m "feat: po-admin invite, revoke and list; ONBOARDING__BOT_PHONE for the wa.me link

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01BxyAfBrZWfpvo7PGh2yLoS"
```

(`uv.lock` only if `uv sync` changed it.)

---

### Task 4: `po-admin suspend` and `unsuspend`

**Files:**
- Modify: `src/personal_organizer/db/repositories/tenants.py` (add `set_status` after `activate`)
- Modify: `src/personal_organizer/admin/cli.py` (two commands, parser, `_run`)
- Modify: `tests/db/test_admin_cli.py` (append classes)

**Interfaces:**
- Consumes: `tenant_of`, `parse_phone`, `_out`, `OK`, `REFUSED` from Task 3; `handle_inbound` for the end-to-end check.
- Produces: `personal_organizer.db.repositories.tenants.set_status(session, tenant_id: UUID, status: str) -> None`; `cli.suspend(db, settings, raw_phone) -> int`; `cli.unsuspend(db, settings, raw_phone) -> int`. Log events `admin.tenant.suspended`, `admin.tenant.unsuspended` with `tenant_id` and `sender`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/db/test_admin_cli.py` (add these imports at the top with the others):

```python
from personal_organizer.messaging.inbound import handle_inbound
from personal_organizer.messaging.replies import INVITE_ONLY_TEXT
from tests.fixtures.channels import FakeOutbound, Spy, insert_inbox
from tests.fixtures.tenants import tenant_by_phone, with_onboarding
```

```python
class TestSuspend:
    async def test_a_suspended_member_is_turned_away(
        self, db: Database, admin_settings: Settings, owner_conn: Any
    ) -> None:
        """The whole point: their next message gets the invite-only line."""
        await new_tenant(db, phone=IL_PHONE, status="active", step=None)
        assert await cli.suspend(db, admin_settings, IL_PHONE) == 0
        tenant = await tenant_by_phone(db, IL_PHONE)
        assert tenant is not None
        assert tenant.status == "suspended"

        outbound = FakeOutbound(name="gowa")
        disposition = await handle_inbound(
            await insert_inbox(owner_conn, phone=IL_PHONE),
            db=db,
            channels={"gowa": outbound}.__getitem__,
            allowlist=frozenset(),
            on_allowed=Spy(),
            settings=with_onboarding(admin_settings),
        )
        assert disposition == "stranger"
        assert [m.body for m in outbound.sent] == [INVITE_ONLY_TEXT]

    async def test_an_env_listed_number_cannot_be_suspended(
        self,
        db: Database,
        admin_settings: Settings,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        await new_tenant(db, phone=OWNER_PHONE, status="active", step=None)
        assert await cli.suspend(db, admin_settings, OWNER_PHONE) == 1
        assert "on WHATSAPP__ALLOWED_PHONES; remove it there first" in capsys.readouterr().out
        tenant = await tenant_by_phone(db, OWNER_PHONE)
        assert tenant is not None
        assert tenant.status == "active"

    async def test_not_a_member(
        self, db: Database, admin_settings: Settings, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert await cli.suspend(db, admin_settings, IL_PHONE) == 1
        assert "not a member" in capsys.readouterr().out

    async def test_suspending_twice_is_fine(
        self, db: Database, admin_settings: Settings, capsys: pytest.CaptureFixture[str]
    ) -> None:
        await new_tenant(db, phone=IL_PHONE, status="suspended", step=None)
        assert await cli.suspend(db, admin_settings, IL_PHONE) == 0
        assert "already suspended" in capsys.readouterr().out


class TestUnsuspend:
    @pytest.mark.parametrize(("step", "restored"), [(None, "active"), ("connect", "onboarding")])
    async def test_it_restores_the_status_from_the_onboarding_step(
        self, db: Database, admin_settings: Settings, step: str | None, restored: str
    ) -> None:
        await new_tenant(db, phone=IL_PHONE, status="suspended", step=step)
        assert await cli.unsuspend(db, admin_settings, IL_PHONE) == 0
        tenant = await tenant_by_phone(db, IL_PHONE)
        assert tenant is not None
        assert (tenant.status, tenant.onboarding_step) == (restored, step)

    async def test_a_member_who_is_not_suspended(
        self, db: Database, admin_settings: Settings, capsys: pytest.CaptureFixture[str]
    ) -> None:
        await new_tenant(db, phone=IL_PHONE, status="active", step=None)
        assert await cli.unsuspend(db, admin_settings, IL_PHONE) == 0
        assert "not suspended (active)" in capsys.readouterr().out

    async def test_not_a_member(
        self, db: Database, admin_settings: Settings, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert await cli.unsuspend(db, admin_settings, IL_PHONE) == 1
        assert "not a member" in capsys.readouterr().out


class TestSuspendLogs:
    async def test_suspend_and_unsuspend_log_no_number(
        self, db: Database, admin_settings: Settings
    ) -> None:
        await new_tenant(db, phone=IL_PHONE, status="active", step=None)
        stream = io.StringIO()
        configure_logging(admin_settings, stream=stream)
        await cli.suspend(db, admin_settings, IL_PHONE)
        await cli.unsuspend(db, admin_settings, IL_PHONE)
        captured = stream.getvalue()
        assert "admin.tenant.suspended" in captured
        assert "admin.tenant.unsuspended" in captured
        for leaked in (IL_PHONE, IL_PHONE.removeprefix("+")):
            assert leaked not in captured
```

Also add to `TestMain`:

```python
    async def test_suspend_is_wired(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert await asyncio.to_thread(cli.main, ["suspend", IL_PHONE]) == 1
        assert "not a member" in capsys.readouterr().out
```

Run: `uv run pytest tests/db/test_admin_cli.py -v`
Expected: the new tests FAIL with `AttributeError: module 'personal_organizer.admin.cli' has no attribute 'suspend'` (and `test_suspend_is_wired` with argparse's `SystemExit: 2`, raised in the thread).

- [ ] **Step 2: Add `set_status`**

In `src/personal_organizer/db/repositories/tenants.py`, after `activate`:

```python
async def set_status(session: AsyncSession, tenant_id: UUID, status: str) -> None:
    """``suspended`` and back, from ``po-admin``. Onboarding's own move is :func:`activate`."""
    await _update(session, tenant_id, status=status)
```

and add `"set_status"` to that module's `__all__` if it has one (keep it sorted).

- [ ] **Step 3: Add the commands**

In `src/personal_organizer/admin/cli.py`, extend the tenants import:

```python
from personal_organizer.db.repositories.tenants import get_tenant, resolve_tenant, set_status
```

Add after `revoke`:

```python
async def suspend(db: Database, settings: Settings, raw_phone: str) -> int:
    phone = parse_phone(raw_phone)
    if phone is None:
        return REFUSED
    if phone in settings.whatsapp.allowlist:
        _out("on WHATSAPP__ALLOWED_PHONES; remove it there first")
        return REFUSED
    tenant = await tenant_of(db, phone)
    if tenant is None:
        _out("not a member")
        return REFUSED
    if tenant.status == "suspended":
        _out("already suspended")
        return OK
    async with db.tenant_session(TenantId(tenant.id)) as session:
        await set_status(session, tenant.id, "suspended")
    log.info("admin.tenant.suspended", tenant_id=str(tenant.id), sender=f"tel:{phone}")
    _out(f"suspended {phone}; their next message gets the invite-only line")
    return OK


async def unsuspend(db: Database, settings: Settings, raw_phone: str) -> int:
    del settings
    phone = parse_phone(raw_phone)
    if phone is None:
        return REFUSED
    tenant = await tenant_of(db, phone)
    if tenant is None:
        _out("not a member")
        return REFUSED
    if tenant.status != "suspended":
        _out(f"not suspended ({tenant.status})")
        return OK
    # activate() clears the step, so no step means onboarding was finished.
    restored = "active" if tenant.onboarding_step is None else "onboarding"
    async with db.tenant_session(TenantId(tenant.id)) as session:
        await set_status(session, tenant.id, restored)
    log.info("admin.tenant.unsuspended", tenant_id=str(tenant.id), sender=f"tel:{phone}")
    _out(f"{phone} is {restored} again")
    return OK
```

In `_parser`, before `commands.add_parser("list", ...)`:

```python
    for name, text in (
        ("suspend", "turn a member away; their messages get the invite-only line"),
        ("unsuspend", "let a suspended member back in"),
    ):
        command = commands.add_parser(name, help=text)
        command.add_argument("phone")
```

In `_run`, before the final `return`:

```python
        if args.command == "suspend":
            return await suspend(db, settings, args.phone)
        if args.command == "unsuspend":
            return await unsuspend(db, settings, args.phone)
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/db/test_admin_cli.py tests/unit/test_tenants_repository.py -v`
Expected: all PASS.

- [ ] **Step 5: Lint, type-check, commit**

```bash
uv run ruff format . && uv run ruff check . && uv run mypy
git add src/personal_organizer/db/repositories/tenants.py src/personal_organizer/admin/cli.py \
  tests/db/test_admin_cli.py
git commit -m "feat: po-admin suspend and unsuspend; env-listed numbers cannot be suspended

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01BxyAfBrZWfpvo7PGh2yLoS"
```

---

### Task 5: Docs, the full suite, and staging

**Files:**
- Create: `docs/adr/0006-invites-in-the-database.md`
- Modify: `docs/runbook-iteration-03.md` (section 4's table; a new section before "When it does not work")
- Modify: `README.md` (intro paragraph; `What is here` table; local commands)
- Modify: `docs/plan-iteration-03.md` ("Not in this iteration")

**Interfaces:**
- Consumes: everything above. Produces: no code.

- [ ] **Step 1: Write ADR 0006**

Create `docs/adr/0006-invites-in-the-database.md`:

```markdown
# ADR 0006: Invites in the database

Status: accepted, 2026-10-07.

## Context

Until now the invite list was `WHATSAPP__ALLOWED_PHONES`. Inviting one person meant editing
a Railway variable and redeploying the api and the worker. The circle is ≤ ~50 people, and
docs/plan-iteration-03.md deferred "DB-backed invites" until it grew past a handful.

## Decision

- **An `invites` table, outside RLS.** An invite exists before its tenant does, so — like
  `channel_inbox` — it has no `tenant_id` and no policy, and is read through
  `system_session`. One open invite per number (a partial unique index on `phone` where
  neither `used_at` nor `revoked_at` is set); used and revoked rows stay as history.
  `app_user` has no `DELETE` on it.
- **The env list stays, as the fallback.** `is_invited` is true for a number on
  `WHATSAPP__ALLOWED_PHONES` or with an open invite, the env list checked first. The owner's
  number lives in the env list, so no state of the table can lock them out.
- **Identity still wins.** Only a sender with no tenant is asked. `enrol` marks the invite
  used in the transaction that creates the tenant.
- **Revoke and suspend are separate.** `po-admin revoke` cancels an unused invite and
  refuses a used one. Cutting off a member is `po-admin suspend` (`tenants.status =
  'suspended'`, which the gate already turns away); `unsuspend` restores `active` or
  `onboarding` from `onboarding_step`. Env-listed numbers cannot be suspended.
- **The bot never writes first.** `po-admin invite` prints `https://wa.me/<bot number>`,
  with no prefilled text, for the owner to forward. An unsolicited first message is what
  gets a QR-gateway number banned, Meta would need a template, and a prefilled "Hi" would
  make every invitee English (D11).
- **`po-admin` runs as `app_user`.** No new role and no new definer function: members are
  found through `resolve_tenant` and changed in their own `tenant_session`.

## Consequences

- Inviting, revoking and suspending need no deploy.
- An invite revoked in the milliseconds between the gate's check and `enrol` still enrols
  that person; `suspend` covers it.
- A first message through Meta that carries no phone number (a user hiding theirs behind a
  username) is not matched to an invite, which is keyed by phone.
- Invitations from chat need only a caller of `create_invite` that passes
  `invited_by_tenant_id`; the column is there.
- Not done: invite expiry, a cap on invites, deleting members.
```

- [ ] **Step 2: Update the runbook**

In `docs/runbook-iteration-03.md`, section 4's variables table, change the `WHATSAPP__ALLOWED_PHONES` row's middle column to start with: `the **fallback** invite list, for every channel — your own number at least; invite everyone else with po-admin (section "Inviting people").` and keep the rest of that cell. Add a row after `ONBOARDING__LINK_TTL_S`:

```markdown
| `ONBOARDING__BOT_PHONE` | worker | the number invitees write to, E.164 — today the GOWA SIM | not E.164: boot fails. Unset: `po-admin invite` records the invite but prints no link |
```

Add a new section immediately before `## When it does not work`:

````markdown
## Inviting people

Invites live in the database (ADR 0006); no deploy is involved. Run `po-admin` inside the
worker, where the private database host resolves:

```sh
railway ssh --service worker --environment staging
po-admin invite +972501234567 --note "Mom"   # prints https://wa.me/<bot number>
po-admin list                                # newest first: open / used / revoked, member status
po-admin revoke +972501234567                # an invite nobody has used yet
po-admin suspend +972501234567               # a member: their messages get the invite-only line
po-admin unsuspend +972501234567             # back to active, or onboarding if they never finished
```

Forward the link yourself. The invitee taps it and writes anything; their first words pick
the language. `po-admin list` shows `used` once their first message is in.

- `already a member (…)`: they have a tenant; an invite would never be read.
- `invite already used; use suspend`: revoke is for invites, suspend for members.
- `on WHATSAPP__ALLOWED_PHONES; remove it there first`: env-listed numbers cannot be
  suspended, so you cannot lock yourself out.
- `po-admin` prints numbers to your terminal only. Its log lines carry `sender_hash` and
  `tenant_id`, matching `inbound.handled`.

If `railway ssh` is unavailable, `railway run --service worker --environment staging uv run
po-admin list` works only from a machine that can reach the database host.
````

Step 4 confirms which of the two works and corrects this paragraph.

- [ ] **Step 3: Update README and the plan**

In `README.md`:
- In the intro, replace `An invited sender` with `A sender invited with \`po-admin invite\` (or on \`WHATSAPP__ALLOWED_PHONES\`)`.
- In the `What is here` table, add a row after `messaging/`:
  `| \`src/personal_organizer/admin/\` | \`po-admin\`: invite, revoke, suspend, unsuspend and list the circle (docs/adr/0006). |`
- After the `uv run po-gowa simulate ...` block, add:

  ```sh
  uv run po-admin invite +31612345678 --note "me"   # then simulate a message from that number
  uv run po-admin list
  ```

In `docs/plan-iteration-03.md`, in "Not in this iteration", change
`- Tenant deletion and data export, and DB-backed invites. Before the circle grows past a`
`  handful of people.` to
`- Tenant deletion and data export, before the circle grows past a handful of people.`
`  (DB-backed invites: done, docs/adr/0006.)`

- [ ] **Step 4: Run the full suite**

Run: `uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest && uv run pytest -m rls`
Expected: all PASS, DB tests **running** (check the summary for skips: `grep -c SKIPPED` should not include `tests/db/`).

- [ ] **Step 5: Commit the docs**

```bash
git add docs/adr/0006-invites-in-the-database.md docs/runbook-iteration-03.md README.md \
  docs/plan-iteration-03.md
git commit -m "docs: ADR 0006, inviting people in the runbook, README and plan

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01BxyAfBrZWfpvo7PGh2yLoS"
```

- [ ] **Step 6: Push and open the PR (ask the user first)**

```bash
git push -u origin invites/db-invites
gh pr create --base main --title "feat: invites in the database, and po-admin" --body "..."
```

The body: summary of ADR 0006, the test counts from Step 4, the deploy note ("migration
0005 runs in pre-deploy; set `ONBOARDING__BOT_PHONE` on the worker"), and ends with:

```
🤖 Generated with [Claude Code](https://claude.com/claude-code)

https://claude.ai/code/session_01BxyAfBrZWfpvo7PGh2yLoS
```

- [ ] **Step 7: Staging Done-When (after merge and deploy; with the user)**

1. `railway ssh --service worker --environment staging`, then `po-admin list` → `no invites`. (If `railway ssh` fails, try `railway run`; fix the runbook paragraph from Step 2 to say which works, and commit.)
2. `po-admin invite +<a second phone, not on WHATSAPP__ALLOWED_PHONES> --note test` → a `wa.me` link.
3. From that phone, tap the link and write `hi` → the welcome and zone choice arrive.
4. `po-admin list` → `used`, member `onboarding`.
5. `po-admin suspend +<that phone>`; write again → the invite-only line. `po-admin unsuspend +<that phone>`; write again → the current onboarding step.
6. Worker logs: `invite.used`, `admin.tenant.suspended`, `admin.tenant.unsuspended`, each with `tenant_id` and no number.
