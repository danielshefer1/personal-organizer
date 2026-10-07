# Iteration 03-lite, PR 2 (`iteration-03/2-schema`) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Put tenants and everything they own under forced row-level security, with one narrow `SECURITY DEFINER` door for mapping a sender to a tenant. Add the repositories PRs 3 and 4 call, and an isolation suite that proves RLS alone keeps tenants apart.

**Architecture:** Bootstrap gains a fourth role, `app_definer` (`NOLOGIN BYPASSRLS`, granted to `app_owner` only). Migration 0004 creates five tenant tables. Each goes under RLS through `alembic/rls.py` with one `tenant_isolation` policy that reads `NULLIF(current_setting('app.tenant_id', true), '')::uuid`. The migration also creates `resolve_tenant` and `create_tenant` and hands their ownership to `app_definer`. Repositories in `db/repositories/` add an app-level `WHERE` through `scoped()`. `tests/db/test_isolation.py` runs twice, once with that filter and once with it patched out, so RLS is shown to be the actual guarantee.

**Tech Stack:** Python 3.14, uv, SQLAlchemy 2 async (asyncpg), Alembic (sync psycopg, runs as `app_owner`), PostgreSQL 16 + pgvector, pytest + pytest-asyncio (`asyncio_mode = "auto"`), ruff, mypy strict.

**Spec:** `docs/plan-iteration-03.md` (D1, D3, D6, D7, D12, "Schema (migration 0004)", PR stack item 1). **Contract:** `docs/superpowers/plans/2026-10-07-iteration-03-contract.md`, section "PR 2 — `2-schema` produces". Every name listed there is implemented exactly as written.

## Contract deviations

No contract name, signature or location changes. The items below are additive, or they settle something the contract leaves open. The PR 3 and PR 4 plans should know about them:

1. **`app_user`'s `EXECUTE` comes from bootstrap's default privileges, not from an explicit `GRANT … TO app_user` in 0004.** The runtime role's name is parsed from `DATABASE__APP_URL` and is not a constant, so a migration that hard-coded `app_user` could grant to a role that does not exist. Bootstrap already installs `ALTER DEFAULT PRIVILEGES … GRANT EXECUTE ON FUNCTIONS TO <app role>`. 0004 runs `REVOKE ALL … FROM PUBLIC` and then `ALTER … OWNER TO app_definer`, and the ACL carries across. The end state is the one the contract describes, and `test_only_the_runtime_role_may_call_the_definer_functions` pins it: `{app_definer=X, app_user=X}` and no `PUBLIC`. The prototype confirmed this on PG 16.15.
2. **`TenantMixin.tenant_id` now carries the FK** `tenants.id ON DELETE CASCADE`, so every tenant table gets it from the mixin, not per model. Both markers also gain `tenant_column: ClassVar[str]`: `"tenant_id"` on `TenantMixin` and `"id"` on `TenantRoot`. `scoped()` and the RLS tests read the column from there.
3. **`MissingDatabaseRoleError(role, *, reason: str | None = None)`**, which carries the "NOLOGIN" message for `DatabaseRole.DEFINER`.
4. **`bind_connection` raises `LookupError`** when the `connected_account_id` is already bound to *another* tenant. The contract does not cover this case. RLS hides the other row, so the insert conflicts with a row the caller cannot read.
5. **`create_tenant` raises `sqlalchemy.exc.IntegrityError`** (`ck_tenants_language`) for a language outside `LANGUAGES`, and nothing is created.
6. **`resolve_tenant` takes one key.** D12's "try `uid:` first, then `tel:`" is two calls, and the order is PR 3's to make.
7. **`alembic.ini`'s `prepend_sys_path` becomes `src:%(here)s/alembic`**, so revisions can `from rls import …`. Alembic applies it before loading any revision, `alembic heads` included. A `sys.path` tweak in `env.py` would come too late for that.
8. **Downgrade rewrites `disposition = 'onboarding'` to `'allowed'`** before restoring 0003's check. Without this, a downgrade over real data fails.
9. **Test fixtures for later PRs:** `clean_tenant_tables` (owner `TRUNCATE tenants CASCADE`) and `database` (a `Database` on the real settings) are added to `tests/db/conftest.py`. `clean_channel_tables`' `TRUNCATE channel_inbox … CASCADE` now also empties `messages`, which has an FK to `channel_inbox`.
10. Extra public helpers: `check_in()` in `db/base.py` (replaces `channel.py`'s private `_in`), and `CURRENT_TENANT`, `POLICY_NAME` and `tenant_rls_statements()` in `alembic/rls.py`.

## Global Constraints

- PostgreSQL 16 + pgvector (`pgvector/pgvector:pg16` locally and in CI; Railway `postgres-ssl:16`).
- D1: `app_definer` (`NOLOGIN BYPASSRLS`, granted to `app_owner` so migrations can give it functions) owns exactly two functions, with `SET search_path = public, pg_temp` and `EXECUTE` granted to `app_user` only. `app_user` cannot become `app_definer`. `app_definer` cannot log in. A new `DatabaseRole.DEFINER` member has no DSN.
- D3: every policy reads `NULLIF(current_setting('app.tenant_id', true), '')::uuid`. `enable_tenant_rls(table, column="tenant_id")` emits `ENABLE`, `FORCE`, and one `FOR ALL … USING … WITH CHECK` policy.
- D12: `tenant_identities.network` is `whatsapp` for both the `gowa` and the Meta `whatsapp` channel. `UNIQUE (network, external_id)`.
- Composio's `user_id` is the tenant UUID, never a phone number.
- `channel_outbox` gains `idempotency_key` (nullable, unique). It is not a tenant table.
- `calendar_connections`: at most one `active` row per tenant (partial unique index).
- Migrations are hand-written and must match the models (`tests/db/test_models_match_migrations.py`), and the downgrade round-trips.
- Repository functions take an `AsyncSession` first and never commit. Tenant-scoped ones filter through `scoped(stmt, Model, tenant_id)`.
- Code style: ruff (line length 100, `ANN`, `S`, `DTZ` …) and `mypy --strict` over `src` and `tests`. Dense explanatory docstrings, `__all__` in every module, `Final` constants. Exception messages never carry PII.
- DB tests are marked `db`, the RLS ones also `rls`, and all skip without a database. RLS assertions run as `app_user` (`app_conn`), never as the owner.
- Docs (runbook, ADR 0005, README) belong to PR 5. This PR changes only docstrings and SQL comments.

## Review Focus

1. **Two first messages from a new sender handled at once** (two workers, one person): one tenant and one identity, and no orphan `tenants` row from the loser. Test: `test_concurrent_create_tenant_leaves_one_tenant` (Task 4) counts past RLS as `app_definer`.
2. **`create_tenant` called with a language other than `he`/`en`** (a PR 3 detection bug): refused as a whole, with no tenant and no identity left behind. Test: `test_create_tenant_rejects_an_unknown_language` (Task 4).
3. **The connect page POSTed twice at once (double tap), or a link used at exactly `expires_at`**: exactly one `True`, and an expired link never consumes. Tests: `test_concurrent_consumes_have_one_winner`, `test_an_expired_link_cannot_be_consumed` (Task 5).
4. **Composio's callback delivered twice, possibly concurrently**: one row, still `active`, and nothing revoked by the duplicate. Tests: `test_bind_connection_is_idempotent_on_the_account`, `test_concurrent_binds_of_one_account_make_one_row` (Task 5).
5. **A `connected_account_id` already bound to tenant B arriving for tenant A**: refused with `LookupError`, and neither tenant's rows change, A's active connection included. Test: `test_an_account_bound_to_b_cannot_be_bound_to_a` (Task 6).

---

## Before Task 1 (once)

```bash
git fetch origin
git switch -c iteration-03/2-schema origin/main      # base fb27a46, per the contract
docker compose up -d postgres                         # local PG16 + pgvector on :5433
cp -n .env.example .env                               # DSNs for bootstrap/owner/app roles
uv sync --all-groups
uv run po-db bootstrap && uv run alembic upgrade head
uv run pytest -q                                      # baseline: everything green
```

Each time `alembic/bootstrap.sql` changes, re-run `uv run po-db bootstrap`. Each time a migration changes, re-run `uv run alembic upgrade head`. The DB tests run against the live local schema.

## File map

| File | Responsibility |
|---|---|
| `src/personal_organizer/db/roles.py` | `DatabaseRole.DEFINER`, `DEFINER_ROLE_NAME` |
| `src/personal_organizer/core/errors.py` | `MissingDatabaseRoleError` gains `reason` |
| `src/personal_organizer/settings.py` | `dsn_for(DEFINER)` refuses |
| `alembic/bootstrap.sql`, `src/personal_organizer/db/bootstrap.py` | create `app_definer`; `po.definer_role` GUC; `po-db check` definer checks |
| `src/personal_organizer/db/base.py` | `check_in`, `TenantMixin` FK + `tenant_column`, `TenantRoot` |
| `src/personal_organizer/db/models/tenant.py` (new) | the five tenant models and their constants |
| `src/personal_organizer/db/models/channel.py`, `models/__init__.py` | `"onboarding"` disposition, `ChannelOutbox.idempotency_key`, exports |
| `alembic/rls.py` (new), `alembic.ini` | the one RLS policy shape; import path for revisions |
| `alembic/versions/0004_tenants.py` (new) | tables, RLS, definer functions, outbox key, disposition check |
| `src/personal_organizer/db/repositories/` (new) | `scope.py`, `tenants.py`, `links.py`, `connections.py`, `messages.py` |
| `tests/db/tenant_tables.py` (new) | `{table: tenant column}` derived from the models |
| `tests/db/test_rls.py`, `test_roles.py`, `test_migrations.py`, `test_bootstrap.py`, `conftest.py` | invariants and fixtures |
| `tests/db/test_repositories.py`, `tests/unit/test_scope.py` (new) | repository behaviour |
| `tests/db/test_isolation.py` (new) | the isolation suite (Done-When 2) |
| `tests/unit/test_settings.py`, `tests/unit/test_bootstrap_connect.py` | definer DSN refusal; bootstrap GUC |

---

### Task 1: The `app_definer` role

**Files:**
- Modify: `src/personal_organizer/db/roles.py` (whole file)
- Modify: `src/personal_organizer/core/errors.py:21-28` (`MissingDatabaseRoleError`)
- Modify: `src/personal_organizer/settings.py:394-399` (`Settings.dsn_for`)
- Modify: `alembic/bootstrap.sql` (role DO block)
- Modify: `src/personal_organizer/db/bootstrap.py` (`run_bootstrap`, `check_database`)
- Test: `tests/unit/test_settings.py`, `tests/unit/test_bootstrap_connect.py`, `tests/db/test_roles.py`, `tests/db/test_bootstrap.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `DatabaseRole.DEFINER = "definer"`; `DEFINER_ROLE_NAME: Final = "app_definer"` (in `personal_organizer.db.roles`); `MissingDatabaseRoleError(role: str, *, reason: str | None = None)`; the role `app_definer` (NOLOGIN NOSUPERUSER BYPASSRLS, `USAGE, CREATE` on `public`, member-of granted to the owner role); `check_database()` reports definer problems.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_settings.py`, inside `class TestParsing` after `test_missing_role_raises`:

```python
    def test_the_definer_role_has_no_dsn(self, settings_factory: Any) -> None:
        """app_definer is NOLOGIN and BYPASSRLS: a DSN for it must never be honoured, even
        one someone configured."""
        cfg = settings_factory(DATABASE__DEFINER_URL="postgresql://app_definer:pw@h/db")
        with pytest.raises(MissingDatabaseRoleError, match="NOLOGIN"):
            cfg.dsn_for(DatabaseRole.DEFINER)
```

In `tests/unit/test_bootstrap_connect.py` change the import to `from personal_organizer.db.roles import DEFINER_ROLE_NAME, DatabaseRole` and append:

```python
class _Transaction:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *exc: object) -> None:
        return None


class _RecordingConnection:
    def __init__(self) -> None:
        self.gucs: dict[str, str] = {}
        self.scripts: list[str] = []

    def transaction(self) -> _Transaction:
        return _Transaction()

    async def execute(self, sql: str, *args: str) -> None:
        if args:
            self.gucs[args[0]] = args[1]
        else:
            self.scripts.append(sql)

    async def close(self) -> None:
        return None


async def test_bootstrap_names_the_definer_role(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``bootstrap.sql`` reads every role name from a GUC; a missing one aborts the script."""
    conn = _RecordingConnection()

    async def connect(_settings: Settings, _role: DatabaseRole) -> _RecordingConnection:
        return conn

    monkeypatch.setattr(bootstrap, "_connect", connect)
    await bootstrap.run_bootstrap(settings)

    assert conn.gucs["po.definer_role"] == DEFINER_ROLE_NAME == "app_definer"
    assert set(conn.gucs) == {
        "po.owner_role",
        "po.owner_password",
        "po.app_role",
        "po.app_password",
        "po.definer_role",
    }
    assert conn.scripts == [bootstrap.BOOTSTRAP_SQL.read_text()]
```

Append to `tests/db/test_roles.py` (and add `import asyncpg` above `import pytest`):

```python
# --- app_definer (Iteration 03, D1) -------------------------------------------------------


async def test_runtime_role_cannot_become_the_definer(app_conn: Any) -> None:
    """app_definer is BYPASSRLS; app_user holding it would make every policy optional."""
    assert await app_conn.fetchval("SELECT pg_has_role(current_user, 'app_definer', 'MEMBER')") is (
        False
    )
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        await app_conn.execute("SET ROLE app_definer")


async def test_the_definer_cannot_log_in(app_conn: Any) -> None:
    row = await app_conn.fetchrow(
        "SELECT rolcanlogin, rolsuper, rolbypassrls FROM pg_roles WHERE rolname = 'app_definer'"
    )
    assert row is not None, "app_definer is missing; run po-db bootstrap"
    assert dict(row) == {"rolcanlogin": False, "rolsuper": False, "rolbypassrls": True}


async def test_the_definer_owns_no_tables(app_conn: Any) -> None:
    owned = await app_conn.fetchval(
        "SELECT count(*) FROM pg_class c JOIN pg_roles r ON c.relowner = r.oid "
        "WHERE r.rolname = 'app_definer'"
    )
    assert owned == 0
```

In `tests/db/test_bootstrap.py` add these imports below `import pytest`:

```python
from personal_organizer.core.errors import MissingDatabaseRoleError
from personal_organizer.db.bootstrap import check_database, run_bootstrap
from personal_organizer.db.roles import DatabaseRole
from personal_organizer.settings import Settings
```

and append:

```python
async def test_bootstrap_re_runs_cleanly_and_po_db_check_passes(
    db_settings: Settings, app_conn: Any
) -> None:
    """Bootstrap runs on every deploy. A second run must converge -- app_definer included --
    and leave ``po-db check`` with nothing to report."""
    del app_conn  # reuses the fixture's skip-if-unavailable behaviour
    try:
        db_settings.dsn_for(DatabaseRole.BOOTSTRAP)
    except MissingDatabaseRoleError:
        pytest.skip("DATABASE__BOOTSTRAP_URL is not configured")
    await run_bootstrap(db_settings)
    assert await check_database(db_settings) == []
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/unit/test_settings.py tests/unit/test_bootstrap_connect.py tests/db/test_roles.py tests/db/test_bootstrap.py -v`
Expected: collection errors in the two unit files (`AttributeError: DEFINER` / `ImportError: cannot import name 'DEFINER_ROLE_NAME'`); `test_runtime_role_cannot_become_the_definer` fails with `role "app_definer" does not exist`; `test_the_definer_cannot_log_in` fails on `row is not None`. `test_bootstrap_re_runs_cleanly_and_po_db_check_passes` already passes. It is a convergence guard, and it starts checking `app_definer` once Step 4 teaches `check_database` about the role.

- [ ] **Step 3: Implement the role constant, the error and the DSN refusal**

Replace `src/personal_organizer/db/roles.py` with:

```python
"""Database roles.

Four privilege tiers, because the spec's two-role split is not self-sufficient:

* ``BOOTSTRAP`` -- Railway's superuser. The only role that may ``CREATE EXTENSION`` and
  ``CREATE ROLE``. Used by ``po-db bootstrap`` and by nothing else, ever.
* ``OWNER`` -- ``app_owner``. Owns the ``public`` schema and every table; runs Alembic.
  Deliberately *not* a superuser: superusers bypass RLS unconditionally, which would make
  the spec's own ``FORCE ROW LEVEL SECURITY`` "second guard" a no-op.
* ``APP`` -- ``app_user``. The runtime role for api and worker. Owns nothing, cannot create
  in ``public``, ``NOBYPASSRLS``, and is not granted ``app_owner`` (no ``SET ROLE`` escape).
* ``DEFINER`` -- ``app_definer``. ``NOLOGIN BYPASSRLS``: it owns the two ``SECURITY DEFINER``
  functions that map a sender to a tenant before any tenant is known (D1 in
  docs/plan-iteration-03.md), and nothing else. Nothing ever connects as it, so it has no
  DSN -- :meth:`Settings.dsn_for` refuses it by name -- and its name is a constant here
  rather than a username parsed out of a URL. It is granted to ``app_owner``, because on
  PostgreSQL 16 ``ALTER FUNCTION ... OWNER TO`` requires the caller to be able to become the
  new owner; it is never granted to ``app_user``.
"""

from enum import StrEnum
from typing import Final

#: The ``SECURITY DEFINER`` owner. Passed to ``bootstrap.sql`` as the ``po.definer_role`` GUC
#: and used by migration 0004 for ``ALTER FUNCTION ... OWNER TO`` and its table grants.
DEFINER_ROLE_NAME: Final = "app_definer"


class DatabaseRole(StrEnum):
    BOOTSTRAP = "bootstrap"
    OWNER = "owner"
    APP = "app"
    DEFINER = "definer"


__all__ = ["DEFINER_ROLE_NAME", "DatabaseRole"]
```

In `src/personal_organizer/core/errors.py`, replace `MissingDatabaseRoleError.__init__` with:

```python
    def __init__(self, role: str, *, reason: str | None = None) -> None:
        message = f"No DSN configured for database role {role!r}"
        super().__init__(f"{message}: {reason}" if reason else message)
        self.role = role
```

In `src/personal_organizer/settings.py`, replace the head of `Settings.dsn_for` (docstring through the line before `value: SecretStr | None = …`) so it reads:

```python
    def dsn_for(self, role: DatabaseRole) -> str:
        """Return the DSN for ``role``, or raise if it was never configured.

        ``DEFINER`` is refused outright rather than looked up: ``app_definer`` is ``NOLOGIN``,
        so no DSN for it can work, and a ``DATABASE__DEFINER_URL`` someone adds would be a
        BYPASSRLS login waiting to be enabled.
        """
        if role is DatabaseRole.DEFINER:
            raise MissingDatabaseRoleError(
                role.value, reason="app_definer is NOLOGIN; it owns functions, nothing connects"
            )
        value: SecretStr | None = getattr(self.database, f"{role.value}_url", None)
        if value is None:
            raise MissingDatabaseRoleError(role.value)
        return value.get_secret_value()
```

- [ ] **Step 4: Create the role in bootstrap**

In `alembic/bootstrap.sql`, in the `-- 2. Roles.` block's `DECLARE`, add after `app_pw …`:

```sql
    definer    text := current_setting('po.definer_role');
```

and insert this between the `-- Deliberately NOT granted: app_owner to app_user …` comment and `-- 4. Default privileges.`:

```sql
    -- 3b. The SECURITY DEFINER owner (Iteration 03, D1). It owns resolve_tenant and
    -- create_tenant, which map a sender to a tenant before any tenant GUC can be set, so
    -- it reads tenant_identities past RLS: BYPASSRLS, which only a superuser can grant --
    -- hence here and not in a migration. NOLOGIN, so nothing can connect as it. The ALTER
    -- re-asserts every attribute on re-runs, so a role someone loosened by hand is put back.
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = definer) THEN
        EXECUTE format(
            'CREATE ROLE %I NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE BYPASSRLS', definer
        );
    END IF;
    EXECUTE format(
        'ALTER ROLE %I NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE BYPASSRLS', definer
    );
    -- PostgreSQL 16's rule for ALTER FUNCTION ... OWNER TO, which migration 0004 runs as
    -- app_owner: the caller must be able to SET ROLE to the new owner, and the new owner
    -- must hold CREATE on the function's schema. Both grants exist for that and nothing
    -- else. app_owner already owns every tenant table, so becoming app_definer gains it
    -- nothing it could not do by disabling RLS on its own tables.
    EXECUTE format('GRANT USAGE, CREATE ON SCHEMA public TO %I', definer);
    EXECUTE format('GRANT %I TO %I', definer, owner_role);
    -- Deliberately NOT granted: app_definer to app_user. That would be BYPASSRLS on demand.
```

In `src/personal_organizer/db/bootstrap.py`:
- import: `from personal_organizer.db.roles import DEFINER_ROLE_NAME, DatabaseRole`
- in `run_bootstrap`, add `("po.definer_role", DEFINER_ROLE_NAME),` as the last tuple of the GUC loop, and change the final log line to:

```python
    log.info(
        "db.bootstrap.ok", owner_role=owner_role, app_role=app_role, definer_role=DEFINER_ROLE_NAME
    )
```

- add this function above `check_database`:

```python
async def _definer_problems(conn: Any) -> list[str]:
    """``app_definer`` exists, cannot log in, and the runtime role cannot become it."""
    row = await conn.fetchrow(
        "SELECT rolcanlogin, rolbypassrls, pg_has_role(current_user, oid, 'MEMBER') AS held "
        "FROM pg_roles WHERE rolname = $1",
        DEFINER_ROLE_NAME,
    )
    if row is None:
        return [f"{DEFINER_ROLE_NAME} is missing; run po-db bootstrap"]
    problems: list[str] = []
    if row["rolcanlogin"]:
        problems.append(f"{DEFINER_ROLE_NAME} can log in; it must be NOLOGIN")
    if not row["rolbypassrls"]:
        problems.append(f"{DEFINER_ROLE_NAME} lacks BYPASSRLS; resolve_tenant would see nothing")
    if row["held"]:
        problems.append(f"runtime role is a member of {DEFINER_ROLE_NAME}, which bypasses RLS")
    return problems
```

- at the end of `check_database`'s `try:` block, after the `can_create` check, add:

```python
        problems += await _definer_problems(conn)
```

- [ ] **Step 5: Bootstrap and run the tests to verify they pass**

Run: `uv run po-db bootstrap && uv run po-db check && uv run pytest tests/unit/test_settings.py tests/unit/test_bootstrap_connect.py tests/db/test_roles.py tests/db/test_bootstrap.py -v`
Expected: `db.check.ok`; all PASS. Running `uv run po-db bootstrap` a second time also succeeds (the `ALTER ROLE` and the grants are idempotent).

- [ ] **Step 6: Commit**

```bash
git add src/personal_organizer/db/roles.py src/personal_organizer/core/errors.py \
  src/personal_organizer/settings.py alembic/bootstrap.sql src/personal_organizer/db/bootstrap.py \
  tests/unit/test_settings.py tests/unit/test_bootstrap_connect.py tests/db/test_roles.py \
  tests/db/test_bootstrap.py
git commit -m "feat: app_definer, the NOLOGIN owner of the tenant-resolution functions

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: Tenant markers and the RLS invariant that can fail

**Files:**
- Modify: `src/personal_organizer/db/base.py`
- Modify: `src/personal_organizer/db/models/channel.py` (`_in` → `check_in`; nothing else yet)
- Create: `tests/db/tenant_tables.py`
- Modify: `tests/db/test_rls.py` (whole file)

**Interfaces:**
- Consumes: nothing from Task 1.
- Produces: `check_in(column: str, values: tuple[str, ...]) -> str`; `TenantMixin` with `tenant_column: ClassVar[str] = "tenant_id"` and `tenant_id` FK to `tenants.id ON DELETE CASCADE`; `class TenantRoot` with `tenant_column: ClassVar[str] = "id"`; `tests.db.tenant_tables.tenant_tables() -> dict[str, str]`; `tests.db.test_rls.rls_problems(conn, expected: set[str]) -> list[str]`.

- [ ] **Step 1: Write the failing tests**

Create `tests/db/tenant_tables.py`:

```python
"""Which tables the models declare tenant-scoped, and the column each is scoped by.

Shared by the RLS invariant (``test_rls.py``) and the isolation suite (``test_isolation.py``),
so both derive their expectations from the same place: the models, never a hand-kept list.
"""

from __future__ import annotations

from personal_organizer.db import models  # noqa: F401  (registers every table)
from personal_organizer.db.base import Base, TenantMixin, TenantRoot


def tenant_tables() -> dict[str, str]:
    """``{table name: tenant column}`` for every ``TenantMixin`` or ``TenantRoot`` model."""
    return {
        table.name: mapper.class_.tenant_column
        for mapper in Base.registry.mappers
        if issubclass(mapper.class_, TenantMixin | TenantRoot)
        for table in mapper.tables
    }


__all__ = ["tenant_tables"]
```

Replace `tests/db/test_rls.py` with:

```python
"""RLS invariants.

These assert the *invariant* rather than any one policy: every table carrying ``TenantMixin``
or ``TenantRoot`` has RLS enabled and forced and the one ``tenant_isolation`` policy, and no
other table has RLS. The set grows with the models automatically -- catching both "forgot to
enable RLS" and "enabled it on the queue by accident". What the policies *do* is
``test_isolation.py``'s job.
"""

from __future__ import annotations

from typing import Any

import pytest

from tests.db.tenant_tables import tenant_tables

pytestmark = [pytest.mark.db, pytest.mark.rls]

_RLS_TABLES = (
    "SELECT relname, relforcerowsecurity FROM pg_class c "
    "JOIN pg_namespace n ON c.relnamespace = n.oid "
    "WHERE n.nspname = 'public' AND c.relkind = 'r' AND c.relrowsecurity"
)


async def rls_problems(conn: Any, expected: set[str]) -> list[str]:
    """Every way the database's RLS tables differ from ``expected``, as messages."""
    rows = await conn.fetch(_RLS_TABLES)
    enabled = {row["relname"] for row in rows}
    problems = [f"{name}: tenant table without RLS" for name in sorted(expected - enabled)]
    problems += [f"{name}: RLS on a table no model scopes" for name in sorted(enabled - expected)]
    problems += [
        f"{row['relname']}: RLS but not FORCE; the owner would bypass its own policies"
        for row in rows
        if not row["relforcerowsecurity"]
    ]
    return problems


async def test_every_tenant_table_has_rls_enabled_and_forced(app_conn: Any) -> None:
    assert await rls_problems(app_conn, set(tenant_tables())) == []


async def test_a_table_with_rls_but_no_force_is_reported(owner_conn: Any) -> None:
    """The check above must be able to fail. Rolled back, so nothing is left behind."""
    tables = set(tenant_tables())
    transaction = owner_conn.transaction()
    await transaction.start()
    try:
        await owner_conn.execute("CREATE TABLE rls_probe (id int)")
        await owner_conn.execute("ALTER TABLE rls_probe ENABLE ROW LEVEL SECURITY")
        problems = await rls_problems(owner_conn, tables | {"rls_probe"})
    finally:
        await transaction.rollback()
    assert problems == ["rls_probe: RLS but not FORCE; the owner would bypass its own policies"]


async def test_the_queue_never_has_rls(app_conn: Any) -> None:
    """RLS on procrastinate_jobs would deadlock the worker against its own queue."""
    forced = await app_conn.fetchval(
        "SELECT relrowsecurity FROM pg_class WHERE relname = 'procrastinate_jobs'"
    )
    assert forced is False
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/db/test_rls.py -v`
Expected: collection error, `ImportError: cannot import name 'TenantRoot' from 'personal_organizer.db.base'`.

- [ ] **Step 3: Implement the markers**

In `src/personal_organizer/db/base.py`: change the import to `from sqlalchemy import DateTime, ForeignKey, MetaData`, and replace everything from `class TenantMixin:` to the end of the file with:

```python
def check_in(column: str, values: tuple[str, ...]) -> str:
    """The SQL of a ``CHECK`` that ``column`` is one of ``values``.

    Built from the same tuple the code uses, so a model's constraint cannot drift from its
    constants. The migration spells the result out by hand, and
    ``tests/db/test_models_match_migrations.py`` compares the two.
    """
    return f"{column} IN ({', '.join(repr(value) for value in values)})"


class TenantMixin:
    """Marks a table as tenant-scoped.

    Migrations enable ``ROW LEVEL SECURITY`` and ``FORCE ROW LEVEL SECURITY`` on exactly the
    tables carrying this mixin or :class:`TenantRoot`, through ``alembic/rls.py``, and
    ``tests/db/test_rls.py`` asserts that set matches the tables with RLS enabled -- catching
    both "forgot to enable" and "enabled on the wrong table". ``procrastinate_*`` tables
    deliberately never carry it: RLS on the queue would deadlock the worker against its own
    jobs.

    ``tenant_id`` references ``tenants`` with ``ON DELETE CASCADE``, so deleting a tenant
    (not built yet; see the plan's "Not in this iteration") is one statement.
    """

    #: The column the RLS policy compares with ``app.tenant_id``. Read by
    #: :func:`personal_organizer.db.repositories.scope.scoped` and by the RLS tests.
    tenant_column: ClassVar[str] = "tenant_id"

    tenant_id: Mapped[UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )


class TenantRoot:
    """Marks the table whose own ``id`` *is* the tenant id -- ``tenants``, and only it.

    It carries no ``tenant_id``, so :class:`TenantMixin` cannot describe it, yet it holds a
    tenant's settings and must sit under RLS like everything else: its policy compares
    ``id`` with ``app.tenant_id``. The RLS invariant treats ``TenantMixin`` and
    ``TenantRoot`` tables alike.
    """

    tenant_column: ClassVar[str] = "id"


__all__ = ["NAMING_CONVENTION", "Base", "TenantMixin", "TenantRoot", "check_in"]
```

In `src/personal_organizer/db/models/channel.py`: delete the private `_in` function, change the import to `from personal_organizer.db.base import Base, check_in`, and replace the two `_in(` calls with `check_in(`. Nothing else changes in this task.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/db/test_rls.py tests/db/test_models_match_migrations.py -v`
Expected: all PASS. `tenant_tables()` is still `{}`, so the invariant says "no table has RLS", and the probe test shows the check reports a table that has RLS without FORCE.

- [ ] **Step 5: Commit**

```bash
git add src/personal_organizer/db/base.py src/personal_organizer/db/models/channel.py \
  tests/db/tenant_tables.py tests/db/test_rls.py
git commit -m "feat: TenantRoot marker, and an RLS invariant that is shown to fail

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: Tenant models, `alembic/rls.py` and migration 0004

**Files:**
- Create: `src/personal_organizer/db/models/tenant.py`
- Modify: `src/personal_organizer/db/models/__init__.py` (whole file)
- Modify: `src/personal_organizer/db/models/channel.py` (`DISPOSITIONS`, `ChannelOutbox`)
- Create: `alembic/rls.py`
- Modify: `alembic.ini:3` (`prepend_sys_path`)
- Create: `alembic/versions/0004_tenants.py`
- Test: `tests/db/test_rls.py`, `tests/db/test_roles.py`, `tests/db/test_migrations.py`, `tests/db/test_models_match_migrations.py` (unchanged, must stay green)

**Interfaces:**
- Consumes: `check_in`, `TenantMixin`, `TenantRoot` (Task 2); `DEFINER_ROLE_NAME` and the `app_definer` role (Task 1).
- Produces: models `Tenant`, `TenantIdentity`, `CalendarConnection`, `OnboardingLink`, `Message`, and the constants `TENANT_STATUSES`, `ONBOARDING_STEPS`, `LANGUAGES`, `NETWORK_WHATSAPP`, `CONNECTION_STATUSES`, `DIRECTIONS` (`personal_organizer.db.models.tenant`; models re-exported from `personal_organizer.db.models`); `ChannelOutbox.idempotency_key: Mapped[str | None]`; `DISPOSITIONS` gains `"onboarding"`; `alembic/rls.py`: `enable_tenant_rls(table: str, column: str = "tenant_id") -> None`, `disable_tenant_rls(table: str) -> None`; SQL `resolve_tenant(text, text) RETURNS uuid`, `create_tenant(text, text, text, text) RETURNS uuid`; revision `0004_tenants`.

- [ ] **Step 1: Write the failing tests**

In `tests/db/test_rls.py`, add below `_RLS_TABLES`:

```python
#: How PostgreSQL prints ``alembic/rls.py``'s predicate back out of ``pg_policies``.
_CURRENT_TENANT = "(NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid"
```

and insert these two tests before `test_the_queue_never_has_rls`:

```python
async def test_the_tenants_table_is_a_tenant_table() -> None:
    """``tenants`` has no ``tenant_id``; ``TenantRoot`` is what puts it under the invariant."""
    assert tenant_tables()["tenants"] == "id"


async def test_every_tenant_table_has_exactly_the_tenant_isolation_policy(
    app_conn: Any,
) -> None:
    expected = {
        table: (f"({column} = {_CURRENT_TENANT})",) * 2 for table, column in tenant_tables().items()
    }
    rows = await app_conn.fetch(
        "SELECT tablename, policyname, cmd, permissive, qual, with_check FROM pg_policies "
        "WHERE schemaname = 'public'"
    )
    found = {row["tablename"]: (row["qual"], row["with_check"]) for row in rows}
    assert found == expected
    assert len(rows) == len(expected), "a second policy on a tenant table widens what it shows"
    for row in rows:
        assert (row["policyname"], row["cmd"], row["permissive"]) == (
            "tenant_isolation",
            "ALL",
            "PERMISSIVE",
        )
```

Append to `tests/db/test_roles.py`:

```python
#: Exactly what app_definer may own. A third function here is a third door past RLS.
DEFINER_FUNCTIONS = {"resolve_tenant", "create_tenant"}


async def test_the_definer_owns_exactly_the_two_resolution_functions(app_conn: Any) -> None:
    rows = await app_conn.fetch(
        "SELECT p.proname, p.prosecdef, p.proconfig FROM pg_proc p "
        "JOIN pg_roles r ON p.proowner = r.oid WHERE r.rolname = 'app_definer'"
    )
    assert {row["proname"] for row in rows} == DEFINER_FUNCTIONS
    for row in rows:
        assert row["prosecdef"] is True
        # Without a pinned search_path, a SECURITY DEFINER function resolves names through
        # whatever schema its caller put first.
        assert row["proconfig"] == ["search_path=public, pg_temp"]


async def test_only_the_runtime_role_may_call_the_definer_functions(app_conn: Any) -> None:
    """PUBLIC holds EXECUTE on every new function by default. ``acldefault`` stands in for
    a NULL ACL, which means exactly that default -- reading only ``proacl`` would miss it."""
    rows = await app_conn.fetch(
        "SELECT p.proname, coalesce(g.rolname, 'PUBLIC') AS grantee "
        "FROM pg_proc p "
        "CROSS JOIN LATERAL aclexplode(coalesce(p.proacl, acldefault('f', p.proowner))) a "
        "LEFT JOIN pg_roles g ON a.grantee = g.oid "
        "WHERE p.proname = ANY($1::text[]) AND a.privilege_type = 'EXECUTE'",
        sorted(DEFINER_FUNCTIONS),
    )
    grantees = {(row["proname"], row["grantee"]) for row in rows}
    assert grantees == {
        (name, role) for name in DEFINER_FUNCTIONS for role in ("app_definer", "app_user")
    }
```

Append to `tests/db/test_migrations.py`:

```python
#: What 0004 adds, by catalog: tables, definer functions, and the outbox column.
_TENANT_TABLES = [
    "calendar_connections",
    "messages",
    "onboarding_links",
    "tenant_identities",
    "tenants",
]
_TENANT_TABLES_SQL = (
    "SELECT tablename FROM pg_tables WHERE schemaname = 'public' "
    "AND tablename = ANY($1::text[]) ORDER BY tablename"
)
_DEFINER_FUNCTIONS_SQL = (
    "SELECT p.proname, r.rolname FROM pg_proc p JOIN pg_roles r ON p.proowner = r.oid "
    "WHERE p.proname IN ('resolve_tenant', 'create_tenant') ORDER BY p.proname"
)
_OUTBOX_KEY_SQL = (
    "SELECT count(*) FROM information_schema.columns "
    "WHERE table_name = 'channel_outbox' AND column_name = 'idempotency_key'"
)


async def test_the_tenants_downgrade_round_trips(owner_conn: Any) -> None:
    """0004 down and up again, with an ``onboarding`` row in the inbox to carry across.

    The row is the case a plain drop-and-recreate misses: 0003's disposition check does not
    know ``onboarding``, so restoring that check over such a row fails the downgrade.
    """
    config = _config()
    await owner_conn.execute("TRUNCATE channel_outbox, channel_inbox CASCADE")
    inbox_id = await owner_conn.fetchval(
        "INSERT INTO channel_inbox (channel, provider_message_id, sender_key, sender_phone, "
        "message_type, sent_at, disposition) "
        "VALUES ('gowa', 'round-trip', 'tel:+15550000000', '+15550000000', 'text', now(), "
        "'onboarding') RETURNING id"
    )
    try:
        command.downgrade(config, "0003_channel_ledgers")
        assert await owner_conn.fetch(_TENANT_TABLES_SQL, _TENANT_TABLES) == []
        assert await owner_conn.fetch(_DEFINER_FUNCTIONS_SQL) == []
        assert await owner_conn.fetchval(_OUTBOX_KEY_SQL) == 0
        assert (
            await owner_conn.fetchval(
                "SELECT disposition FROM channel_inbox WHERE id = $1", inbox_id
            )
            == "allowed"
        )

        command.upgrade(config, "head")
        tables = await owner_conn.fetch(_TENANT_TABLES_SQL, _TENANT_TABLES)
        assert [row["tablename"] for row in tables] == _TENANT_TABLES
        functions = await owner_conn.fetch(_DEFINER_FUNCTIONS_SQL)
        assert [(row["proname"], row["rolname"]) for row in functions] == [
            ("create_tenant", "app_definer"),
            ("resolve_tenant", "app_definer"),
        ]
        assert await owner_conn.fetchval(_OUTBOX_KEY_SQL) == 1
    finally:
        command.upgrade(config, "head")
        await owner_conn.execute("TRUNCATE channel_outbox, channel_inbox CASCADE")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/db/test_rls.py tests/db/test_roles.py tests/db/test_migrations.py -v`
Expected: `test_the_tenants_table_is_a_tenant_table` fails with `KeyError: 'tenants'`. `test_the_definer_owns_exactly_the_two_resolution_functions` fails because `set() != {'resolve_tenant', 'create_tenant'}`. `test_the_tenants_downgrade_round_trips` fails on inserting the `'onboarding'` disposition (`CheckViolationError … ck_channel_inbox_disposition`).

- [ ] **Step 3: Write the models**

Create `src/personal_organizer/db/models/tenant.py`:

```python
"""Tenants and everything that belongs to one.

Every table here sits under RLS (migration 0004): ``tenants`` through :class:`TenantRoot`,
the rest through :class:`TenantMixin`. The runtime role reaches a row only inside
``Database.tenant_session(tenant_id)`` for that tenant. The one way in without a tenant is the
pair of ``SECURITY DEFINER`` functions ``resolve_tenant`` and ``create_tenant``, owned by
``app_definer``, which return a tenant id and nothing else (D1 in docs/plan-iteration-03.md).

A person is one tenant however many numbers or gateways reach us: identities are keyed by
*network*, not by channel, so the GOWA gateway and Meta's Cloud API both write
``network = 'whatsapp'`` (D12). ``external_id`` is ``SenderRef.key`` -- ``tel:+...`` or
``uid:...``.

Composio's ``user_id`` for a tenant is ``str(tenant.id)``, never a phone number, which is why
``calendar_connections.composio_user_id`` is filled from the tenant id.
"""

from __future__ import annotations

from datetime import datetime
from typing import Final
from uuid import UUID

from sqlalchemy import CheckConstraint, ForeignKey, Index, Text, UniqueConstraint, func, text
from sqlalchemy.orm import Mapped, mapped_column

from personal_organizer.db.base import Base, TenantMixin, TenantRoot, check_in

TENANT_STATUSES: Final = ("onboarding", "active", "suspended")
#: The open onboarding step; ``NULL`` once onboarding is over. D10.
ONBOARDING_STEPS: Final = ("zone", "connect")
#: Detected from the first message, never asked. D11.
LANGUAGES: Final = ("he", "en")
#: The identity network both WhatsApp channels (``gowa`` and ``whatsapp``) write. D12.
NETWORK_WHATSAPP: Final = "whatsapp"
CONNECTION_STATUSES: Final = ("active", "revoked", "failed")
DIRECTIONS: Final = ("in", "out")

_GEN_UUID = text("gen_random_uuid()")


class Tenant(Base, TenantRoot):
    __tablename__ = "tenants"
    __table_args__ = (
        CheckConstraint(check_in("status", TENANT_STATUSES), name="status"),
        CheckConstraint(check_in("onboarding_step", ONBOARDING_STEPS), name="onboarding_step"),
        CheckConstraint(check_in("language", LANGUAGES), name="language"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=_GEN_UUID)
    status: Mapped[str] = mapped_column(Text, server_default=text("'onboarding'"))
    onboarding_step: Mapped[str | None] = mapped_column(Text, server_default=text("'zone'"))
    language: Mapped[str] = mapped_column(Text)
    #: IANA name. ``NULL`` until the user confirms one: a guess from the country code is
    #: shown, never stored (D10).
    timezone: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now())


class TenantIdentity(Base, TenantMixin):
    __tablename__ = "tenant_identities"
    __table_args__ = (
        # One person, one key (D12). create_tenant's idempotency rests on this constraint.
        UniqueConstraint("network", "external_id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=_GEN_UUID)
    network: Mapped[str] = mapped_column(Text)
    external_id: Mapped[str] = mapped_column(Text)
    #: E.164, when the identity carries one. A Meta BSUID identity may not.
    phone: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class CalendarConnection(Base, TenantMixin):
    __tablename__ = "calendar_connections"
    __table_args__ = (
        UniqueConstraint("connected_account_id"),
        CheckConstraint(check_in("status", CONNECTION_STATUSES), name="status"),
        # At most one live connection per tenant. Named by hand: the convention would call it
        # ix_calendar_connections_tenant_id, which TenantMixin's plain index already is.
        Index(
            "uq_calendar_connections_tenant_id_active",
            "tenant_id",
            unique=True,
            postgresql_where=text("status = 'active'"),
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=_GEN_UUID)
    #: Always ``str(tenant_id)``; stored so a row can be checked against Composio by itself.
    composio_user_id: Mapped[str] = mapped_column(Text)
    connected_account_id: Mapped[str] = mapped_column(Text)
    auth_config_id: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, server_default=text("'active'"))
    connected_at: Mapped[datetime] = mapped_column(server_default=func.now())


class OnboardingLink(Base, TenantMixin):
    """One connect link. The signature proves we issued it; this row makes it single-use."""

    __tablename__ = "onboarding_links"
    __table_args__ = (UniqueConstraint("nonce"),)

    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=_GEN_UUID)
    nonce: Mapped[str] = mapped_column(Text)
    expires_at: Mapped[datetime]
    used_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class Message(Base, TenantMixin):
    """Conversation content under RLS (D7).

    For a known tenant the worker copies ``body`` here and nulls it in ``channel_inbox``, in
    one transaction, so long-lived personal content never stays in the un-scoped ledger.
    ``channel`` is the channel *name* (``gowa``, ``whatsapp``): a proactive send goes out on
    the channel of the tenant's latest inbound message.
    """

    __tablename__ = "messages"
    __table_args__ = (CheckConstraint(check_in("direction", DIRECTIONS), name="direction"),)

    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=_GEN_UUID)
    direction: Mapped[str] = mapped_column(Text)
    channel: Mapped[str] = mapped_column(Text)
    inbox_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("channel_inbox.id", ondelete="SET NULL")
    )
    message_type: Mapped[str] = mapped_column(Text)
    body: Mapped[str | None] = mapped_column(Text)
    sent_at: Mapped[datetime]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


__all__ = [
    "CONNECTION_STATUSES",
    "DIRECTIONS",
    "LANGUAGES",
    "NETWORK_WHATSAPP",
    "ONBOARDING_STEPS",
    "TENANT_STATUSES",
    "CalendarConnection",
    "Message",
    "OnboardingLink",
    "Tenant",
    "TenantIdentity",
]
```

Replace `src/personal_organizer/db/models/__init__.py` with:

```python
"""ORM models. Importing this package registers every table on ``Base.metadata``, which is
what Alembic's autogenerate and the model-vs-migration test compare against."""

from personal_organizer.db.models.channel import ChannelInbox, ChannelOutbox
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
    "Message",
    "OnboardingLink",
    "Tenant",
    "TenantIdentity",
]
```

In `src/personal_organizer/db/models/channel.py`, replace the `DISPOSITIONS` line with:

```python
#: What the worker decided about an inbound row. ``onboarding`` (Iteration 03) is a message
#: from a tenant still onboarding, answered by its current step; ``allowed`` is one from an
#: active tenant, handed to ``on_allowed``.
DISPOSITIONS: Final = ("allowed", "stranger", "stranger_muted", "stale", "onboarding")
```

In `ChannelOutbox.__table_args__`, insert after `UniqueConstraint("channel", "provider_message_id"),`:

```python
        # The claim key for a send that answers no inbound row (D6): "You're all set" is
        # ``connected:<connection_id>``. NULLs are distinct, so rows claimed on
        # ``(inbox_id, kind)`` -- every reply -- never collide here.
        UniqueConstraint("idempotency_key"),
```

and insert after the `status` column:

```python
    #: Set instead of ``inbox_id`` for a send that answers nothing. See ``messaging.outbox``.
    idempotency_key: Mapped[str | None] = mapped_column(Text)
```

- [ ] **Step 4: Watch the drift guard catch the missing migration**

Run: `uv run pytest tests/db/test_models_match_migrations.py -v`
Expected: FAIL. `models and migrations disagree: [('add_table', Table('tenants', …)), …, ('add_column', …, 'channel_outbox', Column('idempotency_key', …)), …]`

- [ ] **Step 5: Write the RLS helper and make it importable from revisions**

Create `alembic/rls.py`:

```python
"""Row-level security for tenant tables, as migrations apply it.

One policy shape for every tenant table, written once (D3 in docs/plan-iteration-03.md):

* ``ENABLE`` turns policies on for every role but the owner and BYPASSRLS roles.
* ``FORCE`` turns them on for the owner too. ``app_owner`` owns every table and runs the
  migrations, so without it a data migration would silently see every tenant at once.
* One ``FOR ALL`` policy, ``tenant_isolation``, whose ``USING`` and ``WITH CHECK`` are the
  same predicate: a row is visible, and may be written, only under its own tenant's GUC.

The predicate reads ``NULLIF(current_setting('app.tenant_id', true), '')::uuid``. The
``true`` makes a missing setting NULL instead of an error. The ``NULLIF`` is for pooled
connections: once a ``SET LOCAL`` has been rolled back or committed, the setting does not
disappear, it reverts to ``''`` -- and ``''::uuid`` raises. NULL compares unequal to every
id, so a session with no tenant sees zero rows and can write none: fail closed.

Imported by revisions as ``from rls import ...``: ``alembic.ini`` puts this directory on
``sys.path`` (``prepend_sys_path``), which Alembic applies before it loads any revision.
"""

from __future__ import annotations

from typing import Final

from alembic import op

POLICY_NAME: Final = "tenant_isolation"

#: The current tenant, NULL when none is set. See the module docstring for the NULLIF.
CURRENT_TENANT: Final = "NULLIF(current_setting('app.tenant_id', true), '')::uuid"


def tenant_rls_statements(table: str, column: str = "tenant_id") -> list[str]:
    """The DDL :func:`enable_tenant_rls` runs, as strings.

    ``table`` and ``column`` come from revision source, never from input, so they are
    interpolated; each is still quoted as an identifier.
    """
    predicate = f'"{column}" = {CURRENT_TENANT}'
    return [
        f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY',
        f'ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY',
        f'CREATE POLICY {POLICY_NAME} ON "{table}" FOR ALL '
        f"USING ({predicate}) WITH CHECK ({predicate})",
    ]


def enable_tenant_rls(table: str, column: str = "tenant_id") -> None:
    """Put ``table`` under the tenant policy. ``tenants`` passes ``column="id"``."""
    for statement in tenant_rls_statements(table, column):
        op.execute(statement)


def disable_tenant_rls(table: str) -> None:
    """Undo :func:`enable_tenant_rls`, for a downgrade that keeps the table."""
    op.execute(f'DROP POLICY IF EXISTS {POLICY_NAME} ON "{table}"')
    op.execute(f'ALTER TABLE "{table}" NO FORCE ROW LEVEL SECURITY')
    op.execute(f'ALTER TABLE "{table}" DISABLE ROW LEVEL SECURITY')


__all__ = [
    "CURRENT_TENANT",
    "POLICY_NAME",
    "disable_tenant_rls",
    "enable_tenant_rls",
    "tenant_rls_statements",
]
```

In `alembic.ini`, replace the line `prepend_sys_path = src` with:

```ini
# src for the application package; alembic/ itself so revisions can `from rls import ...`
# (alembic/rls.py). Alembic applies this before it loads any revision -- `alembic heads`
# included -- which env.py could not.
prepend_sys_path = src:%(here)s/alembic
```

- [ ] **Step 6: Write migration 0004**

Create `alembic/versions/0004_tenants.py`:

```python
"""Tenants under RLS, the tenant-resolution door, and the outbox idempotency key.

Iteration 03 (docs/plan-iteration-03.md, "Schema"):

* Five tenant tables -- ``tenants``, ``tenant_identities``, ``calendar_connections``,
  ``onboarding_links``, ``messages`` -- each with RLS enabled, forced, and the one
  ``tenant_isolation`` policy from ``alembic/rls.py`` (D3). ``tenants`` is keyed on ``id``.
* ``resolve_tenant`` and ``create_tenant``: ``SECURITY DEFINER``, owned by ``app_definer``
  (``NOLOGIN BYPASSRLS``, created by ``bootstrap.sql``), callable by ``app_user`` and nobody
  else (D1). They are the only way to reach a tenant row without that tenant's GUC, and they
  return an id and nothing more.
* ``channel_outbox.idempotency_key`` (D6) and the ``onboarding`` disposition.

Three PostgreSQL details shape the function DDL:

1. A new function is executable by ``PUBLIC``. ``REVOKE ... FROM PUBLIC`` is what makes
   "callable by app_user only" true; app_user's own EXECUTE comes from the default
   privileges bootstrap installs for objects app_owner creates.
2. Privileges are set *before* the owner changes. ``ALTER ... OWNER TO`` carries the ACL
   across, rewriting the grantor, so the order leaves nothing to re-grant as the new owner.
3. On PostgreSQL 16 ``ALTER FUNCTION ... OWNER TO app_definer`` needs app_owner to be able
   to ``SET ROLE app_definer`` and app_definer to hold ``CREATE`` on ``public``. Bootstrap
   grants both; this revision fails loudly on a database bootstrapped before Iteration 03.

``app_definer`` gets ``SELECT, INSERT`` on the two tables the functions touch: the default
privileges cover app_user only. Hand-written to match ``personal_organizer.db.models``;
``tests/db/test_models_match_migrations.py`` fails if the two drift.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from rls import disable_tenant_rls, enable_tenant_rls

from personal_organizer.db.roles import DEFINER_ROLE_NAME

revision: str = "0004_tenants"
down_revision: str | None = "0003_channel_ledgers"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TZ = sa.DateTime(timezone=True)
_GEN_UUID = sa.text("gen_random_uuid()")

#: Child tables first, so the downgrade can drop in this order.
_CHILD_TABLES = ("messages", "onboarding_links", "calendar_connections", "tenant_identities")

_DISPOSITIONS_0003 = "disposition IN ('allowed', 'stranger', 'stranger_muted', 'stale')"
_DISPOSITIONS_0004 = (
    "disposition IN ('allowed', 'stranger', 'stranger_muted', 'stale', 'onboarding')"
)

_RESOLVE_TENANT = """
CREATE FUNCTION resolve_tenant(p_network text, p_external_id text)
RETURNS uuid
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
    SELECT tenant_id FROM tenant_identities
    WHERE network = p_network AND external_id = p_external_id
$$
"""

# The fast path answers a known sender without a subtransaction. The slow path inserts the
# tenant and its identity inside one exception block, so that losing a race to a concurrent
# call for the same identity rolls back *both* rows -- no orphan tenant -- and the
# winner's id is read once its row is committed (READ COMMITTED: each statement sees it).
_CREATE_TENANT = """
CREATE FUNCTION create_tenant(
    p_network text, p_external_id text, p_phone text, p_language text
)
RETURNS uuid
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
    v_tenant uuid;
BEGIN
    SELECT tenant_id INTO v_tenant FROM tenant_identities
    WHERE network = p_network AND external_id = p_external_id;
    IF FOUND THEN
        RETURN v_tenant;
    END IF;
    BEGIN
        INSERT INTO tenants (language) VALUES (p_language) RETURNING id INTO v_tenant;
        INSERT INTO tenant_identities (tenant_id, network, external_id, phone)
        VALUES (v_tenant, p_network, p_external_id, p_phone);
        RETURN v_tenant;
    EXCEPTION WHEN unique_violation THEN
        SELECT tenant_id INTO v_tenant FROM tenant_identities
        WHERE network = p_network AND external_id = p_external_id;
        RETURN v_tenant;
    END;
END
$$
"""

_FUNCTIONS = (
    ("resolve_tenant(text, text)", _RESOLVE_TENANT),
    ("create_tenant(text, text, text, text)", _CREATE_TENANT),
)


def _create_tables() -> None:
    op.create_table(
        "tenants",
        sa.Column("id", sa.Uuid(), server_default=_GEN_UUID, nullable=False),
        sa.Column("status", sa.Text(), server_default=sa.text("'onboarding'"), nullable=False),
        sa.Column("onboarding_step", sa.Text(), server_default=sa.text("'zone'"), nullable=True),
        sa.Column("language", sa.Text(), nullable=False),
        sa.Column("timezone", sa.Text(), nullable=True),
        sa.Column("created_at", _TZ, server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", _TZ, server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_tenants"),
        sa.CheckConstraint(
            "status IN ('onboarding', 'active', 'suspended')", name="ck_tenants_status"
        ),
        sa.CheckConstraint(
            "onboarding_step IN ('zone', 'connect')", name="ck_tenants_onboarding_step"
        ),
        sa.CheckConstraint("language IN ('he', 'en')", name="ck_tenants_language"),
    )

    op.create_table(
        "tenant_identities",
        sa.Column("id", sa.Uuid(), server_default=_GEN_UUID, nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("network", sa.Text(), nullable=False),
        sa.Column("external_id", sa.Text(), nullable=False),
        sa.Column("phone", sa.Text(), nullable=True),
        sa.Column("created_at", _TZ, server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_tenant_identities"),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_tenant_identities_tenant_id_tenants",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "network", "external_id", name="uq_tenant_identities_network_external_id"
        ),
    )
    op.create_index("ix_tenant_identities_tenant_id", "tenant_identities", ["tenant_id"])

    op.create_table(
        "calendar_connections",
        sa.Column("id", sa.Uuid(), server_default=_GEN_UUID, nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("composio_user_id", sa.Text(), nullable=False),
        sa.Column("connected_account_id", sa.Text(), nullable=False),
        sa.Column("auth_config_id", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), server_default=sa.text("'active'"), nullable=False),
        sa.Column("connected_at", _TZ, server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_calendar_connections"),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_calendar_connections_tenant_id_tenants",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "connected_account_id", name="uq_calendar_connections_connected_account_id"
        ),
        sa.CheckConstraint(
            "status IN ('active', 'revoked', 'failed')", name="ck_calendar_connections_status"
        ),
    )
    op.create_index("ix_calendar_connections_tenant_id", "calendar_connections", ["tenant_id"])
    op.create_index(
        "uq_calendar_connections_tenant_id_active",
        "calendar_connections",
        ["tenant_id"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )

    op.create_table(
        "onboarding_links",
        sa.Column("id", sa.Uuid(), server_default=_GEN_UUID, nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("nonce", sa.Text(), nullable=False),
        sa.Column("expires_at", _TZ, nullable=False),
        sa.Column("used_at", _TZ, nullable=True),
        sa.Column("created_at", _TZ, server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_onboarding_links"),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_onboarding_links_tenant_id_tenants",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("nonce", name="uq_onboarding_links_nonce"),
    )
    op.create_index("ix_onboarding_links_tenant_id", "onboarding_links", ["tenant_id"])

    op.create_table(
        "messages",
        sa.Column("id", sa.Uuid(), server_default=_GEN_UUID, nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("direction", sa.Text(), nullable=False),
        sa.Column("channel", sa.Text(), nullable=False),
        sa.Column("inbox_id", sa.Uuid(), nullable=True),
        sa.Column("message_type", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), nullable=True),
        sa.Column("sent_at", _TZ, nullable=False),
        sa.Column("created_at", _TZ, server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_messages"),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_messages_tenant_id_tenants",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["inbox_id"],
            ["channel_inbox.id"],
            name="fk_messages_inbox_id_channel_inbox",
            ondelete="SET NULL",
        ),
        sa.CheckConstraint("direction IN ('in', 'out')", name="ck_messages_direction"),
    )
    op.create_index("ix_messages_tenant_id", "messages", ["tenant_id"])


def _create_functions() -> None:
    for signature, ddl in _FUNCTIONS:
        op.execute(ddl)
        op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC")
        op.execute(f'ALTER FUNCTION {signature} OWNER TO "{DEFINER_ROLE_NAME}"')


def upgrade() -> None:
    _create_tables()

    enable_tenant_rls("tenants", column="id")
    for table in _CHILD_TABLES:
        enable_tenant_rls(table)

    op.execute(f'GRANT SELECT, INSERT ON tenants, tenant_identities TO "{DEFINER_ROLE_NAME}"')
    _create_functions()

    op.add_column("channel_outbox", sa.Column("idempotency_key", sa.Text(), nullable=True))
    op.create_unique_constraint(
        "uq_channel_outbox_idempotency_key", "channel_outbox", ["idempotency_key"]
    )

    op.drop_constraint("ck_channel_inbox_disposition", "channel_inbox", type_="check")
    op.create_check_constraint("ck_channel_inbox_disposition", "channel_inbox", _DISPOSITIONS_0004)


def downgrade() -> None:
    # 0003's check does not know 'onboarding'. Those senders were invited, which is what
    # 'allowed' meant before tenants existed.
    op.execute("UPDATE channel_inbox SET disposition = 'allowed' WHERE disposition = 'onboarding'")
    op.drop_constraint("ck_channel_inbox_disposition", "channel_inbox", type_="check")
    op.create_check_constraint("ck_channel_inbox_disposition", "channel_inbox", _DISPOSITIONS_0003)

    op.drop_constraint("uq_channel_outbox_idempotency_key", "channel_outbox", type_="unique")
    op.drop_column("channel_outbox", "idempotency_key")

    # app_owner may drop them: it owns schema public, and it is a member of app_definer.
    for signature, _ in reversed(_FUNCTIONS):
        op.execute(f"DROP FUNCTION {signature}")

    for table in _CHILD_TABLES:
        disable_tenant_rls(table)
        op.drop_table(table)
    disable_tenant_rls("tenants")
    op.drop_table("tenants")
```

- [ ] **Step 7: Migrate and run the tests to verify they pass**

Run: `uv run alembic heads && uv run alembic upgrade head && uv run pytest tests/db -v`
Expected: `0004_tenants (head)`, then every DB test passes, including `test_models_match_the_migrated_schema`, both downgrade round-trips (`0001` and `0003`), the new role/ACL tests and the RLS policy-shape test. If `ALTER FUNCTION … OWNER TO` fails with `must be able to SET ROLE "app_definer"`, Task 1's bootstrap was not re-run: `uv run po-db bootstrap`.

- [ ] **Step 8: Commit**

```bash
git add src/personal_organizer/db/models/ alembic/rls.py alembic.ini \
  alembic/versions/0004_tenants.py tests/db/test_rls.py tests/db/test_roles.py \
  tests/db/test_migrations.py
git commit -m "feat: tenant tables under forced RLS, and the two SECURITY DEFINER functions

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 4: `scoped()` and the tenants repository

**Files:**
- Create: `src/personal_organizer/db/repositories/__init__.py`, `scope.py`, `tenants.py`
- Modify: `tests/db/conftest.py` (two fixtures)
- Create: `tests/unit/test_scope.py`, `tests/db/test_repositories.py`

**Interfaces:**
- Consumes: the models and SQL functions (Task 3); `Database.tenant_session(TenantId)` / `system_session()` (`db/engine.py`, unchanged).
- Produces (exact contract signatures):
  - `scoped[S: (Select[Any], Update, Delete)](stmt: S, model: type[Base], tenant_id: UUID) -> S`. It raises `TypeError` for a non-tenant model.
  - `tenants.resolve_tenant(session, *, network: str, external_id: str) -> UUID | None`
  - `tenants.create_tenant(session, *, network: str, external_id: str, phone: str | None, language: str) -> UUID`
  - `tenants.get_tenant(session, tenant_id: UUID) -> Tenant | None`
  - `tenants.set_timezone(session, tenant_id: UUID, timezone: str) -> None`
  - `tenants.set_onboarding_step(session, tenant_id: UUID, step: str | None) -> None`
  - `tenants.activate(session, tenant_id: UUID) -> None`
  - `tenants.primary_phone(session, tenant_id: UUID) -> str | None`
  - fixtures `clean_tenant_tables` and `database` in `tests/db/conftest.py`.

- [ ] **Step 1: Write the failing tests**

In `tests/db/conftest.py` add `from personal_organizer.db.engine import Database` to the imports, and insert above `def whatsapp_db_settings`:

```python
#: Every tenant table hangs off ``tenants`` with ``ON DELETE CASCADE``, so CASCADE from it
#: empties them all -- and keeps doing so as tables are added.
_TENANT_ROOT = "tenants"


@pytest.fixture
async def clean_tenant_tables(owner_conn: Any) -> AsyncIterator[None]:
    """Empty every tenant table before and after a test.

    TRUNCATE is not subject to row-level security, so the owner empties these tables even
    though ``FORCE`` makes every SELECT, INSERT, UPDATE and DELETE of its own see nothing
    without a tenant GUC.
    """
    await owner_conn.execute(f"TRUNCATE {_TENANT_ROOT} CASCADE")
    try:
        yield
    finally:
        await owner_conn.execute(f"TRUNCATE {_TENANT_ROOT} CASCADE")


@pytest.fixture
async def database(db_settings: Settings, owner_conn: Any) -> AsyncIterator[Database]:
    """A :class:`Database` on the real settings. ``owner_conn`` supplies the skip."""
    del owner_conn
    db = Database(db_settings)
    try:
        yield db
    finally:
        await db.dispose()
```

Create `tests/unit/test_scope.py`:

```python
"""``scoped`` -- the app-level tenant filter the isolation suite switches off.

No database: what is pinned is the SQL it adds, and that it refuses a model RLS does not
cover, where a tenant filter would be a bug hiding behind a missing policy.
"""

from __future__ import annotations

from uuid import UUID

import pytest
from sqlalchemy import delete, select, update
from sqlalchemy.sql import ClauseElement

from personal_organizer.db.models import ChannelInbox, Message, Tenant, TenantIdentity
from personal_organizer.db.repositories.scope import scoped

TENANT = UUID("00000000-0000-4000-8000-000000000001")


def _sql(stmt: ClauseElement) -> str:
    return str(stmt)


def test_a_mixin_table_is_filtered_on_tenant_id() -> None:
    sql = _sql(scoped(select(TenantIdentity), TenantIdentity, TENANT))
    assert "WHERE tenant_identities.tenant_id = :tenant_id_1" in sql


def test_the_root_table_is_filtered_on_id() -> None:
    sql = _sql(scoped(select(Tenant), Tenant, TENANT))
    assert "WHERE tenants.id = :id_1" in sql


def test_updates_and_deletes_are_filtered_too() -> None:
    expected = "WHERE messages.tenant_id = :tenant_id_1"
    assert expected in _sql(scoped(update(Message), Message, TENANT))
    assert expected in _sql(scoped(delete(Message), Message, TENANT))


def test_a_table_outside_rls_is_refused() -> None:
    with pytest.raises(TypeError, match="ChannelInbox is not a tenant model"):
        scoped(select(ChannelInbox), ChannelInbox, TENANT)
```

Create `tests/db/test_repositories.py`:

```python
"""Repository behaviour, against Postgres, as ``app_user`` under RLS.

What each function promises its callers in PRs 3 and 4: idempotent tenant creation, a
single-use link, a reconnect that leaves one active connection, and where a send that
answers nothing should go. Isolation between tenants is ``test_isolation.py``'s job.
"""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy.exc import IntegrityError

from personal_organizer.core.types import TenantId
from personal_organizer.db.engine import Database
from personal_organizer.db.models.tenant import NETWORK_WHATSAPP, TenantIdentity
from personal_organizer.db.repositories import tenants

pytestmark = [pytest.mark.db, pytest.mark.usefixtures("clean_tenant_tables")]

PHONE = "+972501234567"
KEY = f"tel:{PHONE}"


async def _create(database: Database, *, key: str = KEY, phone: str | None = PHONE) -> UUID:
    async with database.system_session() as session:
        return await tenants.create_tenant(
            session, network=NETWORK_WHATSAPP, external_id=key, phone=phone, language="he"
        )


# --- tenants ------------------------------------------------------------------------------


async def test_create_tenant_starts_onboarding_at_the_zone_step(database: Database) -> None:
    tenant_id = await _create(database)
    async with database.tenant_session(TenantId(tenant_id)) as session:
        tenant = await tenants.get_tenant(session, tenant_id)
    assert tenant is not None
    assert (tenant.status, tenant.onboarding_step, tenant.language, tenant.timezone) == (
        "onboarding",
        "zone",
        "he",
        None,
    )


async def test_create_tenant_is_idempotent_on_the_identity(database: Database) -> None:
    first = await _create(database)
    second = await _create(database, phone="+15550000000")
    assert first == second
    async with database.tenant_session(TenantId(first)) as session:
        # The second call changed nothing, not even the phone it was given.
        assert await tenants.primary_phone(session, first) == PHONE


async def test_concurrent_create_tenant_leaves_one_tenant(
    database: Database, owner_conn: Any
) -> None:
    """Two messages from a new sender, handled by two workers at once."""
    ids = await asyncio.gather(*(_create(database) for _ in range(5)))
    assert len(set(ids)) == 1
    # A loser's tenant row would be an orphan no GUC can see, so count past RLS: app_owner
    # may become app_definer (bootstrap grants it for ALTER FUNCTION), which is BYPASSRLS.
    async with owner_conn.transaction():
        await owner_conn.execute("SET LOCAL ROLE app_definer")
        assert await owner_conn.fetchval("SELECT count(*) FROM tenants") == 1
        assert await owner_conn.fetchval("SELECT count(*) FROM tenant_identities") == 1


async def test_create_tenant_rejects_an_unknown_language(
    database: Database, owner_conn: Any
) -> None:
    """D11 detects ``he`` or ``en``; anything else is a caller bug, refused whole."""
    with pytest.raises(IntegrityError, match="ck_tenants_language"):
        async with database.system_session() as session:
            await tenants.create_tenant(
                session, network=NETWORK_WHATSAPP, external_id=KEY, phone=PHONE, language="fr"
            )
    async with database.system_session() as session:
        assert (
            await tenants.resolve_tenant(session, network=NETWORK_WHATSAPP, external_id=KEY) is None
        )


async def test_resolve_tenant(database: Database) -> None:
    async with database.system_session() as session:
        assert (
            await tenants.resolve_tenant(session, network=NETWORK_WHATSAPP, external_id=KEY) is None
        )
    tenant_id = await _create(database)
    async with database.system_session() as session:
        assert (
            await tenants.resolve_tenant(session, network=NETWORK_WHATSAPP, external_id=KEY)
            == tenant_id
        )
        # Keyed by network (D12): the same key on another network is someone else.
        assert await tenants.resolve_tenant(session, network="telegram", external_id=KEY) is None


async def test_onboarding_state_updates(database: Database) -> None:
    tenant_id = await _create(database)
    async with database.tenant_session(TenantId(tenant_id)) as session:
        await tenants.set_timezone(session, tenant_id, "Asia/Jerusalem")
        await tenants.set_onboarding_step(session, tenant_id, "connect")
        tenant = await tenants.get_tenant(session, tenant_id)
        assert tenant is not None
        await session.refresh(tenant)
        assert (tenant.timezone, tenant.onboarding_step, tenant.status) == (
            "Asia/Jerusalem",
            "connect",
            "onboarding",
        )
        assert tenant.updated_at >= tenant.created_at

        await tenants.activate(session, tenant_id)
        await session.refresh(tenant)
        assert (tenant.status, tenant.onboarding_step) == ("active", None)


async def test_primary_phone_is_the_earliest_identity_with_one(database: Database) -> None:
    tenant_id = await _create(database, key="uid:BSUID.1", phone=None)
    async with database.tenant_session(TenantId(tenant_id)) as session:
        assert await tenants.primary_phone(session, tenant_id) is None
        session.add(
            TenantIdentity(
                tenant_id=tenant_id, network=NETWORK_WHATSAPP, external_id=KEY, phone=PHONE
            )
        )
    # A separate transaction: created_at is now(), the transaction's start time.
    async with database.tenant_session(TenantId(tenant_id)) as session:
        session.add(
            TenantIdentity(
                tenant_id=tenant_id,
                network="telegram",
                external_id="tel:+15550000000",
                phone="+15550000000",
            )
        )
    async with database.tenant_session(TenantId(tenant_id)) as session:
        assert await tenants.primary_phone(session, tenant_id) == PHONE
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/unit/test_scope.py tests/db/test_repositories.py -v`
Expected: collection errors, `ModuleNotFoundError: No module named 'personal_organizer.db.repositories'`.

- [ ] **Step 3: Implement the package, `scoped` and the tenants repository**

Create `src/personal_organizer/db/repositories/__init__.py`:

```python
"""Data access for tenant tables.

Every function takes an ``AsyncSession`` first and never commits: the caller owns the
transaction, and for tenant tables that is ``Database.tenant_session(tenant_id)``, whose GUC
is what RLS checks. Each tenant-scoped query *also* filters through
:func:`~personal_organizer.db.repositories.scope.scoped`, an app-level ``WHERE`` that is
deliberately redundant: ``tests/db/test_isolation.py`` runs once with it and once with it
patched to a no-op, which is how the suite proves RLS alone keeps tenants apart.

``tenants.resolve_tenant`` and ``tenants.create_tenant`` are the exceptions: they run in a
``system_session`` and go through the ``SECURITY DEFINER`` functions, because the tenant is
what they are finding out.
"""
```

Create `src/personal_organizer/db/repositories/scope.py`:

```python
"""The app-level tenant filter.

RLS is the guarantee; this is the belt to its braces, and the seam the isolation suite cuts.
``scoped(stmt, Model, tenant_id)`` adds ``WHERE <tenant column> = :tenant_id`` to a
``select``, ``update`` or ``delete`` of a tenant model, reading the column from the model's
marker: ``tenant_id`` for ``TenantMixin``, ``id`` for ``TenantRoot``.

Repositories import it by name (``from ...scope import scoped``), and the isolation suite
replaces that name in every repository module, so a query that filters by tenant *without*
going through here is one the suite cannot switch off -- and one RLS is not being shown to
cover. Use it for every tenant filter.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import Delete, Select, Update

from personal_organizer.db.base import Base, TenantMixin, TenantRoot


def scoped[S: (Select[Any], Update, Delete)](stmt: S, model: type[Base], tenant_id: UUID) -> S:
    """Return ``stmt`` restricted to ``tenant_id``'s rows of ``model``."""
    if not issubclass(model, TenantMixin | TenantRoot):
        msg = f"{model.__name__} is not a tenant model"
        raise TypeError(msg)
    return stmt.where(model.__table__.c[model.tenant_column] == tenant_id)


__all__ = ["scoped"]
```

Create `src/personal_organizer/db/repositories/tenants.py`:

```python
"""Tenants: resolution, creation, and the onboarding state on the ``tenants`` row.

:func:`resolve_tenant` and :func:`create_tenant` run in a ``system_session`` -- the worker
does not know the tenant yet, which is the whole point -- and call the ``SECURITY DEFINER``
functions of the same names (migration 0004, D1). They get back an id and nothing else.
Everything else here runs in ``Database.tenant_session(tenant_id)``.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import Uuid, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from personal_organizer.db.models.tenant import Tenant, TenantIdentity
from personal_organizer.db.repositories.scope import scoped


async def resolve_tenant(session: AsyncSession, *, network: str, external_id: str) -> UUID | None:
    """The tenant ``(network, external_id)`` belongs to, or ``None``.

    One key per call. Trying ``uid:`` before ``tel:`` (D12) is the caller's order to choose.
    """
    tenant_id: UUID | None = await session.scalar(
        select(func.resolve_tenant(network, external_id, type_=Uuid()))
    )
    return tenant_id


async def create_tenant(
    session: AsyncSession, *, network: str, external_id: str, phone: str | None, language: str
) -> UUID:
    """Create an ``onboarding`` tenant with this identity, or return the one that has it.

    Idempotent and race-safe on ``(network, external_id)``: two concurrent calls for the same
    sender return the same id and leave one tenant behind.
    """
    result = await session.execute(
        select(func.create_tenant(network, external_id, phone, language, type_=Uuid()))
    )
    tenant_id: UUID = result.scalar_one()
    return tenant_id


async def get_tenant(session: AsyncSession, tenant_id: UUID) -> Tenant | None:
    tenant: Tenant | None = await session.scalar(scoped(select(Tenant), Tenant, tenant_id))
    return tenant


async def _update(session: AsyncSession, tenant_id: UUID, **values: object) -> None:
    await session.execute(
        scoped(update(Tenant), Tenant, tenant_id).values(**values, updated_at=func.now())
    )


async def set_timezone(session: AsyncSession, tenant_id: UUID, timezone: str) -> None:
    """Store a *confirmed* IANA zone. A guess is never stored (D10)."""
    await _update(session, tenant_id, timezone=timezone)


async def set_onboarding_step(session: AsyncSession, tenant_id: UUID, step: str | None) -> None:
    await _update(session, tenant_id, onboarding_step=step)


async def activate(session: AsyncSession, tenant_id: UUID) -> None:
    """Onboarding is over: ``active``, with no open step."""
    await _update(session, tenant_id, status="active", onboarding_step=None)


async def primary_phone(session: AsyncSession, tenant_id: UUID) -> str | None:
    """The phone of the tenant's earliest identity that has one -- where a send that answers
    no inbound message goes (D10)."""
    phone: str | None = await session.scalar(
        scoped(select(TenantIdentity.phone), TenantIdentity, tenant_id)
        .where(TenantIdentity.phone.is_not(None))
        .order_by(TenantIdentity.created_at, TenantIdentity.id)
        .limit(1)
    )
    return phone


__all__ = [
    "activate",
    "create_tenant",
    "get_tenant",
    "primary_phone",
    "resolve_tenant",
    "set_onboarding_step",
    "set_timezone",
]
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/unit/test_scope.py tests/db/test_repositories.py -v && uv run mypy`
Expected: all PASS, mypy `Success`. `test_concurrent_create_tenant_leaves_one_tenant` is the race in Review Focus 1. If it fails with two tenants, check that `create_tenant`'s slow path still wraps *both* inserts in the one `BEGIN … EXCEPTION` block.

- [ ] **Step 5: Commit**

```bash
git add src/personal_organizer/db/repositories/ tests/db/conftest.py tests/unit/test_scope.py \
  tests/db/test_repositories.py
git commit -m "feat: scoped() and the tenants repository

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 5: Links, connections and messages repositories

**Files:**
- Create: `src/personal_organizer/db/repositories/links.py`, `connections.py`, `messages.py`
- Modify: `tests/db/test_repositories.py` (imports, a helper, three sections)

**Interfaces:**
- Consumes: `scoped` (Task 4); `OnboardingLink`, `CalendarConnection`, `Message` (Task 3).
- Produces (exact contract signatures):
  - `links.create_link(session, tenant_id: UUID, *, nonce: str, expires_at: datetime) -> OnboardingLink`
  - `links.latest_usable_link(session, tenant_id: UUID, *, now: datetime) -> OnboardingLink | None`
  - `links.consume_link(session, tenant_id: UUID, *, nonce: str, now: datetime) -> bool`. Expiry is exclusive: `expires_at > now`.
  - `connections.bind_connection(session, tenant_id: UUID, *, connected_account_id: str, auth_config_id: str) -> CalendarConnection`. Raises `LookupError` if another tenant holds the account.
  - `messages.record_inbound(session, tenant_id: UUID, *, inbox_id: UUID, channel: str, message_type: str, body: str | None, sent_at: datetime) -> UUID`
  - `messages.latest_inbound_channel(session, tenant_id: UUID) -> str | None`

- [ ] **Step 1: Write the failing tests**

In `tests/db/test_repositories.py`, replace the import block's three changed lines so the imports read:

```python
import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy.exc import IntegrityError

from personal_organizer.core.types import TenantId
from personal_organizer.db.engine import Database
from personal_organizer.db.models.tenant import (
    NETWORK_WHATSAPP,
    CalendarConnection,
    TenantIdentity,
)
from personal_organizer.db.repositories import connections, links, messages, tenants
```

add below `KEY = …`:

```python
NOW = datetime(2026, 10, 8, 9, 0, tzinfo=UTC)
```

add below `_create`:

```python
async def _inbox(owner_conn: Any, *, channel: str = "gowa") -> UUID:
    inbox_id: UUID = await owner_conn.fetchval(
        "INSERT INTO channel_inbox (channel, provider_message_id, sender_key, sender_phone, "
        "message_type, sent_at) VALUES ($1, $2, $3, $4, 'text', now()) RETURNING id",
        channel,
        f"repo.{uuid4().hex}",
        KEY,
        PHONE,
    )
    return inbox_id
```

and append to the end of the file:

```python
# --- links --------------------------------------------------------------------------------


async def test_consume_link_is_single_use(database: Database) -> None:
    tenant_id = await _create(database)
    async with database.tenant_session(TenantId(tenant_id)) as session:
        await links.create_link(
            session, tenant_id, nonce="n-1", expires_at=NOW + timedelta(minutes=30)
        )
    async with database.tenant_session(TenantId(tenant_id)) as session:
        assert await links.consume_link(session, tenant_id, nonce="n-1", now=NOW) is True
    async with database.tenant_session(TenantId(tenant_id)) as session:
        assert await links.consume_link(session, tenant_id, nonce="n-1", now=NOW) is False
        assert await links.consume_link(session, tenant_id, nonce="n-other", now=NOW) is False


async def test_concurrent_consumes_have_one_winner(database: Database) -> None:
    """Two POSTs of the same connect page, e.g. a double tap."""
    tenant_id = await _create(database)
    async with database.tenant_session(TenantId(tenant_id)) as session:
        await links.create_link(session, tenant_id, nonce="n-1", expires_at=NOW + timedelta(1))

    async def consume() -> bool:
        async with database.tenant_session(TenantId(tenant_id)) as session:
            return await links.consume_link(session, tenant_id, nonce="n-1", now=NOW)

    assert sorted(await asyncio.gather(*(consume() for _ in range(4)))) == [
        False,
        False,
        False,
        True,
    ]


async def test_an_expired_link_cannot_be_consumed(database: Database) -> None:
    tenant_id = await _create(database)
    async with database.tenant_session(TenantId(tenant_id)) as session:
        await links.create_link(session, tenant_id, nonce="n-1", expires_at=NOW)
        # Expiry is exclusive: a link is dead at its expires_at.
        assert await links.consume_link(session, tenant_id, nonce="n-1", now=NOW) is False


async def test_latest_usable_link(database: Database) -> None:
    tenant_id = await _create(database)
    async with database.tenant_session(TenantId(tenant_id)) as session:
        assert await links.latest_usable_link(session, tenant_id, now=NOW) is None
        await links.create_link(session, tenant_id, nonce="old", expires_at=NOW + timedelta(1))
    async with database.tenant_session(TenantId(tenant_id)) as session:
        await links.create_link(session, tenant_id, nonce="new", expires_at=NOW + timedelta(1))
        await links.create_link(session, tenant_id, nonce="dead", expires_at=NOW)
    async with database.tenant_session(TenantId(tenant_id)) as session:
        link = await links.latest_usable_link(session, tenant_id, now=NOW)
        assert link is not None
        assert link.nonce == "new"
        assert await links.consume_link(session, tenant_id, nonce="new", now=NOW)
        link = await links.latest_usable_link(session, tenant_id, now=NOW)
        assert link is not None
        assert link.nonce == "old"


# --- connections --------------------------------------------------------------------------


async def test_bind_connection_revokes_the_previous_one(database: Database) -> None:
    tenant_id = await _create(database)
    async with database.tenant_session(TenantId(tenant_id)) as session:
        first = await connections.bind_connection(
            session, tenant_id, connected_account_id="ca_1", auth_config_id="ac_1"
        )
        assert (first.status, first.composio_user_id) == ("active", str(tenant_id))
    async with database.tenant_session(TenantId(tenant_id)) as session:
        second = await connections.bind_connection(
            session, tenant_id, connected_account_id="ca_2", auth_config_id="ac_1"
        )
        previous = await session.get(CalendarConnection, first.id, populate_existing=True)
        assert previous is not None
        assert (previous.status, second.status) == ("revoked", "active")


async def test_bind_connection_is_idempotent_on_the_account(database: Database) -> None:
    """The callback delivered twice: one row, still active, nothing revoked."""
    tenant_id = await _create(database)
    async with database.tenant_session(TenantId(tenant_id)) as session:
        first = await connections.bind_connection(
            session, tenant_id, connected_account_id="ca_1", auth_config_id="ac_1"
        )
    async with database.tenant_session(TenantId(tenant_id)) as session:
        again = await connections.bind_connection(
            session, tenant_id, connected_account_id="ca_1", auth_config_id="ac_1"
        )
        assert (again.id, again.status) == (first.id, "active")


async def test_concurrent_binds_of_one_account_make_one_row(
    database: Database, owner_conn: Any
) -> None:
    tenant_id = await _create(database)

    async def bind() -> UUID:
        async with database.tenant_session(TenantId(tenant_id)) as session:
            row = await connections.bind_connection(
                session, tenant_id, connected_account_id="ca_1", auth_config_id="ac_1"
            )
            return row.id

    assert len(set(await asyncio.gather(bind(), bind()))) == 1
    async with owner_conn.transaction():
        await owner_conn.execute("SELECT set_config('app.tenant_id', $1, true)", str(tenant_id))
        assert await owner_conn.fetchval("SELECT count(*) FROM calendar_connections") == 1


# --- messages -----------------------------------------------------------------------------


async def test_latest_inbound_channel_follows_the_newest_message(
    database: Database, owner_conn: Any
) -> None:
    tenant_id = await _create(database)
    async with database.tenant_session(TenantId(tenant_id)) as session:
        assert await messages.latest_inbound_channel(session, tenant_id) is None
        for channel, minutes in (("whatsapp", 1), ("gowa", 2)):
            await messages.record_inbound(
                session,
                tenant_id,
                inbox_id=await _inbox(owner_conn, channel=channel),
                channel=channel,
                message_type="text",
                body="hello",
                sent_at=NOW + timedelta(minutes=minutes),
            )
        assert await messages.latest_inbound_channel(session, tenant_id) == "gowa"


async def test_record_inbound_survives_the_inbox_row(database: Database, owner_conn: Any) -> None:
    """``inbox_id`` is ``ON DELETE SET NULL``: the ledger may be pruned, content stays."""
    tenant_id = await _create(database)
    inbox_id = await _inbox(owner_conn)
    async with database.tenant_session(TenantId(tenant_id)) as session:
        message_id = await messages.record_inbound(
            session,
            tenant_id,
            inbox_id=inbox_id,
            channel="gowa",
            message_type="text",
            body="hello",
            sent_at=NOW,
        )
    await owner_conn.execute("DELETE FROM channel_inbox WHERE id = $1", inbox_id)
    async with owner_conn.transaction():
        await owner_conn.execute("SELECT set_config('app.tenant_id', $1, true)", str(tenant_id))
        row = await owner_conn.fetchrow(
            "SELECT inbox_id, body, direction FROM messages WHERE id = $1", message_id
        )
    assert dict(row) == {"inbox_id": None, "body": "hello", "direction": "in"}
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/db/test_repositories.py -v`
Expected: collection error, `ImportError: cannot import name 'connections' from 'personal_organizer.db.repositories'`.

- [ ] **Step 3: Implement the three repositories**

Create `src/personal_organizer/db/repositories/links.py`:

```python
"""Onboarding links: the row that makes a signed connect link single-use (D4, D10).

The token's signature proves we issued it; ``used_at`` proves it has not been spent.
:func:`consume_link` is one conditional ``UPDATE``, so two POSTs racing on the same link
cannot both win: the second finds ``used_at`` already set and changes nothing.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from personal_organizer.db.models.tenant import OnboardingLink
from personal_organizer.db.repositories.scope import scoped


async def create_link(
    session: AsyncSession, tenant_id: UUID, *, nonce: str, expires_at: datetime
) -> OnboardingLink:
    link = OnboardingLink(tenant_id=tenant_id, nonce=nonce, expires_at=expires_at)
    session.add(link)
    await session.flush()
    await session.refresh(link)
    return link


async def latest_usable_link(
    session: AsyncSession, tenant_id: UUID, *, now: datetime
) -> OnboardingLink | None:
    """The newest link that is unused and unexpired at ``now``, for a re-send (D10)."""
    link: OnboardingLink | None = await session.scalar(
        scoped(select(OnboardingLink), OnboardingLink, tenant_id)
        .where(OnboardingLink.used_at.is_(None), OnboardingLink.expires_at > now)
        .order_by(OnboardingLink.created_at.desc(), OnboardingLink.expires_at.desc())
        .limit(1)
    )
    return link


async def consume_link(
    session: AsyncSession, tenant_id: UUID, *, nonce: str, now: datetime
) -> bool:
    """Spend the link. ``True`` exactly once per link, and never once it has expired."""
    result = await session.execute(
        scoped(update(OnboardingLink), OnboardingLink, tenant_id)
        .where(
            OnboardingLink.nonce == nonce,
            OnboardingLink.used_at.is_(None),
            OnboardingLink.expires_at > now,
        )
        .values(used_at=now)
        .returning(OnboardingLink.id)
    )
    return result.first() is not None


__all__ = ["consume_link", "create_link", "latest_usable_link"]
```

Create `src/personal_organizer/db/repositories/connections.py`:

```python
"""Calendar connections: which Composio account a tenant's calendar is reached through.

At most one ``active`` row per tenant (a partial unique index). Reconnecting revokes the old
row and inserts the new one in the caller's transaction, so there is never a moment with two
active rows or, for anyone reading through RLS, none.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from personal_organizer.db.models.tenant import CalendarConnection
from personal_organizer.db.repositories.scope import scoped


async def _by_account(
    session: AsyncSession, tenant_id: UUID, connected_account_id: str
) -> CalendarConnection | None:
    row: CalendarConnection | None = await session.scalar(
        scoped(select(CalendarConnection), CalendarConnection, tenant_id).where(
            CalendarConnection.connected_account_id == connected_account_id
        )
    )
    return row


async def bind_connection(
    session: AsyncSession, tenant_id: UUID, *, connected_account_id: str, auth_config_id: str
) -> CalendarConnection:
    """Make ``connected_account_id`` the tenant's active connection.

    Idempotent on ``connected_account_id``: a callback delivered twice finds its row and
    returns it unchanged, whatever its status, without revoking anything. A *new* account
    revokes the previous active one first. ``composio_user_id`` is the tenant id, never a
    phone number. ``ON CONFLICT DO NOTHING`` covers the race of two callbacks for the same
    account: the loser waits for the winner's commit and then reads its row.

    Raises :class:`LookupError` when the account is already bound to *another* tenant. The
    callback's ``user_id`` check (D5) makes that unreachable in practice; if it ever happens,
    the caller's transaction rolls back and nothing of either tenant changes.
    """
    existing = await _by_account(session, tenant_id, connected_account_id)
    if existing is not None:
        return existing
    await session.execute(
        scoped(update(CalendarConnection), CalendarConnection, tenant_id)
        .where(CalendarConnection.status == "active")
        .values(status="revoked")
    )
    await session.execute(
        insert(CalendarConnection)
        .values(
            tenant_id=tenant_id,
            composio_user_id=str(tenant_id),
            connected_account_id=connected_account_id,
            auth_config_id=auth_config_id,
        )
        .on_conflict_do_nothing(index_elements=["connected_account_id"])
    )
    bound = await _by_account(session, tenant_id, connected_account_id)
    if bound is None:
        # The insert conflicted with a row RLS will not show us: another tenant's.
        msg = "connected account is bound to another tenant"
        raise LookupError(msg)
    return bound


__all__ = ["bind_connection"]
```

Create `src/personal_organizer/db/repositories/messages.py`:

```python
"""Conversation content under RLS (D7), and which channel a tenant last wrote from."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from personal_organizer.db.models.tenant import Message
from personal_organizer.db.repositories.scope import scoped


async def record_inbound(
    session: AsyncSession,
    tenant_id: UUID,
    *,
    inbox_id: UUID,
    channel: str,
    message_type: str,
    body: str | None,
    sent_at: datetime,
) -> UUID:
    """Copy one inbound message into ``messages``. Returns the new row's id.

    The caller nulls the inbox row's content in the same transaction (D7).
    """
    result = await session.execute(
        insert(Message)
        .values(
            tenant_id=tenant_id,
            direction="in",
            channel=channel,
            inbox_id=inbox_id,
            message_type=message_type,
            body=body,
            sent_at=sent_at,
        )
        .returning(Message.id)
    )
    message_id: UUID = result.scalar_one()
    return message_id


async def latest_inbound_channel(session: AsyncSession, tenant_id: UUID) -> str | None:
    """The channel name of the tenant's most recent inbound message: where a send that
    answers nothing goes (ADR 0004's "proactive sends need a channel choice")."""
    channel: str | None = await session.scalar(
        scoped(select(Message.channel), Message, tenant_id)
        .where(Message.direction == "in")
        .order_by(Message.sent_at.desc(), Message.created_at.desc())
        .limit(1)
    )
    return channel


__all__ = ["latest_inbound_channel", "record_inbound"]
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/db/test_repositories.py -v && uv run mypy`
Expected: all PASS (16 tests), mypy `Success`.

- [ ] **Step 5: Commit**

```bash
git add src/personal_organizer/db/repositories/ tests/db/test_repositories.py
git commit -m "feat: onboarding links, calendar connections and messages repositories

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 6: The isolation suite (Done-When 2), then whole-branch verification

**Files:**
- Create: `tests/db/test_isolation.py`

**Interfaces:**
- Consumes: every repository function (Tasks 4–5), `tenant_tables()` (Task 2), the fixtures `clean_tenant_tables`, `clean_channel_tables`, `database`, `app_conn` (`tests/db/conftest.py`).
- Produces: `SEEDS: dict[str, Seed]`, one seed factory per tenant table. A later PR that adds a tenant table must add its factory here, or this suite fails.

This task's tests check behaviour that Tasks 1–5 already built, so they pass on first run. Step 3 is the red step: it breaks RLS on purpose and watches the suite catch it.

- [ ] **Step 1: Write the suite**

Create `tests/db/test_isolation.py`:

```python
"""Tenant isolation, proved against Postgres as ``app_user`` (Done-When 2).

Two tenants, A and B, each with at least one row in every tenant table. Four things hold:

1. Under A's GUC, no B row is visible -- by raw SQL on every table, and through every
   repository function.
2. Under A's GUC, a row carrying B's id cannot be written: ``WITH CHECK`` refuses it, on
   insert and on update.
3. With no GUC -- or the ``''`` a pooled connection is left with after a ``SET LOCAL`` --
   every tenant table shows zero rows and accepts none.
4. The ``SECURITY DEFINER`` functions return a tenant id and nothing else.

The whole module runs twice: once as written, once with
:func:`~personal_organizer.db.repositories.scope.scoped` replaced by a no-op in every
repository module. The second run is the one that matters -- it shows RLS alone keeps tenants
apart, so an app-level filter forgotten in some future query leaks nothing.

Every tenant table needs a seed factory in :data:`SEEDS`. A model added without one fails
:func:`test_every_tenant_table_has_a_seed_factory` and the ``seeded`` fixture, so the suite
cannot silently stop covering a table.
"""

from __future__ import annotations

import importlib
import pkgutil
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import asyncpg
import pytest
from sqlalchemy.exc import DBAPIError

from personal_organizer.core.types import TenantId
from personal_organizer.db import repositories
from personal_organizer.db.engine import Database
from personal_organizer.db.repositories import connections, links, messages, tenants
from tests.db.tenant_tables import tenant_tables

pytestmark = [
    pytest.mark.db,
    pytest.mark.rls,
    pytest.mark.usefixtures("clean_tenant_tables", "clean_channel_tables"),
]

PHONES = {"a": "+972500000001", "b": "+972500000002", "x": "+972500000009"}
CHANNELS = {"a": "gowa", "b": "whatsapp", "x": "gowa"}
NOW = datetime(2026, 10, 8, 9, 0, tzinfo=UTC)

Seed = Callable[[Any, UUID, str], Awaitable[None]]


# --- seed factories: one per tenant table, run under the tenant's own GUC -----------------


async def _seed_tenant(conn: Any, tenant: UUID, tag: str) -> None:
    await conn.execute(
        "INSERT INTO tenants (id, language, timezone) VALUES ($1, 'en', 'Asia/Jerusalem')", tenant
    )


async def _seed_identity(conn: Any, tenant: UUID, tag: str) -> None:
    await conn.execute(
        "INSERT INTO tenant_identities (tenant_id, network, external_id, phone) "
        "VALUES ($1, 'whatsapp', $2, $3)",
        tenant,
        f"tel:{PHONES[tag]}",
        PHONES[tag],
    )


async def _seed_connection(conn: Any, tenant: UUID, tag: str) -> None:
    await conn.execute(
        "INSERT INTO calendar_connections (tenant_id, composio_user_id, connected_account_id, "
        "auth_config_id) VALUES ($1, $2, $3, 'ac_test')",
        tenant,
        str(tenant),
        f"ca_{tag}",
    )


async def _seed_link(conn: Any, tenant: UUID, tag: str) -> None:
    await conn.execute(
        "INSERT INTO onboarding_links (tenant_id, nonce, expires_at) VALUES ($1, $2, $3)",
        tenant,
        f"nonce-{tag}",
        NOW + timedelta(days=1),
    )


async def _seed_message(conn: Any, tenant: UUID, tag: str) -> None:
    inbox_id = await conn.fetchval(
        "INSERT INTO channel_inbox (channel, provider_message_id, sender_key, sender_phone, "
        "message_type, sent_at) VALUES ($1, $2, $3, $4, 'text', $5) RETURNING id",
        CHANNELS[tag],
        f"iso.{uuid4().hex}",
        f"tel:{PHONES[tag]}",
        PHONES[tag],
        NOW,
    )
    await conn.execute(
        "INSERT INTO messages (tenant_id, direction, channel, inbox_id, message_type, body, "
        "sent_at) VALUES ($1, 'in', $2, $3, 'text', $4, $5)",
        tenant,
        CHANNELS[tag],
        inbox_id,
        f"secret of {tag}",
        NOW,
    )


#: ``tenants`` first: every other table references it.
SEEDS: dict[str, Seed] = {
    "tenants": _seed_tenant,
    "tenant_identities": _seed_identity,
    "calendar_connections": _seed_connection,
    "onboarding_links": _seed_link,
    "messages": _seed_message,
}

TABLES = sorted(tenant_tables())


@asynccontextmanager
async def as_tenant(conn: Any, tenant: UUID | None) -> AsyncIterator[None]:
    """A transaction on ``conn`` with ``app.tenant_id`` set as the application sets it."""
    async with conn.transaction():
        if tenant is not None:
            await conn.execute("SELECT set_config('app.tenant_id', $1, true)", str(tenant))
        yield


@dataclass(frozen=True, slots=True)
class Seeded:
    a: UUID
    b: UUID


@pytest.fixture
async def seeded(app_conn: Any) -> Seeded:
    assert set(SEEDS) == set(TABLES), "every tenant table needs a seed factory"
    pair = Seeded(a=uuid4(), b=uuid4())
    for tenant, tag in ((pair.a, "a"), (pair.b, "b")):
        async with as_tenant(app_conn, tenant):
            for seed in SEEDS.values():
                await seed(app_conn, tenant, tag)
    return pair


# --- the two modes ------------------------------------------------------------------------


def _no_scope(stmt: Any, model: Any, tenant_id: UUID) -> Any:
    return stmt


def _disable_app_filter(monkeypatch: pytest.MonkeyPatch) -> set[str]:
    """Replace ``scoped`` in every repository module, including ones added later."""
    patched: set[str] = set()
    for info in pkgutil.iter_modules(repositories.__path__):
        module = importlib.import_module(f"{repositories.__name__}.{info.name}")
        if hasattr(module, "scoped"):
            monkeypatch.setattr(module, "scoped", _no_scope)
            patched.add(info.name)
    return patched


@pytest.fixture(autouse=True, params=["app-filter", "rls-only"])
def mode(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> str:
    if request.param == "rls-only":
        patched = _disable_app_filter(monkeypatch)
        assert patched >= {"scope", "tenants", "links", "connections", "messages"}
    return str(request.param)


async def _snapshot(conn: Any, tenant: UUID) -> dict[str, list[tuple[Any, ...]]]:
    """Every row ``tenant`` owns, in every tenant table: what "B is untouched" compares."""
    snapshot: dict[str, list[tuple[Any, ...]]] = {}
    async with as_tenant(conn, tenant):
        for table in TABLES:
            rows = await conn.fetch(f"SELECT * FROM {table}")  # noqa: S608 - names from models
            snapshot[table] = sorted(tuple(row.values()) for row in rows)
    return snapshot


# --- 0. the suite covers every table ------------------------------------------------------


def test_every_tenant_table_has_a_seed_factory() -> None:
    missing = set(TABLES) - set(SEEDS)
    assert not missing, f"add a seed factory to SEEDS for {sorted(missing)}"
    assert set(SEEDS) <= set(TABLES), "SEEDS names a table no model scopes"


# --- 1. A sees no B rows ------------------------------------------------------------------


@pytest.mark.parametrize("table", TABLES)
async def test_a_sees_only_its_own_rows(app_conn: Any, seeded: Seeded, table: str) -> None:
    column = tenant_tables()[table]
    async with as_tenant(app_conn, seeded.a):
        owners = await app_conn.fetch(f"SELECT DISTINCT {column} AS owner FROM {table}")  # noqa: S608
    assert [row["owner"] for row in owners] == [seeded.a]


async def test_the_app_filter_is_really_off_in_rls_only_mode(
    database: Database, seeded: Seeded, mode: str
) -> None:
    """The canary for the patch: asking for B under A returns A's row once the WHERE is
    gone, and nothing while it is there. Either way, never B's."""
    async with database.tenant_session(TenantId(seeded.a)) as session:
        tenant = await tenants.get_tenant(session, seeded.b)
    expected = seeded.a if mode == "rls-only" else None
    assert (tenant.id if tenant else None) == expected


async def test_reads_through_every_repository_function(database: Database, seeded: Seeded) -> None:
    a, b = seeded.a, seeded.b
    async with database.tenant_session(TenantId(a)) as session:
        tenant = await tenants.get_tenant(session, b)
        assert tenant is None or tenant.id == a
        assert await tenants.primary_phone(session, b) != PHONES["b"]
        link = await links.latest_usable_link(session, b, now=NOW)
        assert link is None or link.tenant_id == a
        assert await messages.latest_inbound_channel(session, b) != CHANNELS["b"]
        assert await links.consume_link(session, b, nonce="nonce-b", now=NOW) is False


async def test_updates_through_every_repository_function_leave_b_alone(
    database: Database, app_conn: Any, seeded: Seeded
) -> None:
    before = await _snapshot(app_conn, seeded.b)
    async with database.tenant_session(TenantId(seeded.a)) as session:
        await tenants.set_timezone(session, seeded.b, "Europe/London")
        await tenants.set_onboarding_step(session, seeded.b, "connect")
        await tenants.activate(session, seeded.b)
        await links.consume_link(session, seeded.b, nonce="nonce-b", now=NOW)
    assert await _snapshot(app_conn, seeded.b) == before


@pytest.mark.parametrize(
    "write",
    [
        lambda s, b: links.create_link(s, b, nonce="nonce-new", expires_at=NOW),
        lambda s, b: connections.bind_connection(
            s, b, connected_account_id="ca_new", auth_config_id="ac_test"
        ),
        lambda s, b: messages.record_inbound(
            s, b, inbox_id=uuid4(), channel="gowa", message_type="text", body="x", sent_at=NOW
        ),
    ],
    ids=["create_link", "bind_connection", "record_inbound"],
)
async def test_inserts_for_b_under_a_are_refused(
    database: Database,
    app_conn: Any,
    seeded: Seeded,
    write: Callable[[Any, UUID], Awaitable[object]],
) -> None:
    before = await _snapshot(app_conn, seeded.b)
    with pytest.raises(DBAPIError, match="row-level security"):
        async with database.tenant_session(TenantId(seeded.a)) as session:
            await write(session, seeded.b)
    assert await _snapshot(app_conn, seeded.b) == before


async def test_an_account_bound_to_b_cannot_be_bound_to_a(
    database: Database, app_conn: Any, seeded: Seeded
) -> None:
    """``connected_account_id`` is unique across tenants, and RLS hides B's row from A: the
    insert conflicts with a row A cannot read. Refused, and A's own connection survives."""
    before = {
        tag: await _snapshot(app_conn, tenant) for tag, tenant in (("a", seeded.a), ("b", seeded.b))
    }
    with pytest.raises(LookupError):
        async with database.tenant_session(TenantId(seeded.a)) as session:
            await connections.bind_connection(
                session, seeded.a, connected_account_id="ca_b", auth_config_id="ac_test"
            )
    after = {
        tag: await _snapshot(app_conn, tenant) for tag, tenant in (("a", seeded.a), ("b", seeded.b))
    }
    assert after == before


# --- 2. WITH CHECK ------------------------------------------------------------------------


@pytest.mark.parametrize("table", TABLES)
async def test_writing_b_rows_under_a_fails_with_check(
    app_conn: Any, seeded: Seeded, table: str
) -> None:
    with pytest.raises(asyncpg.InsufficientPrivilegeError, match="row-level security"):
        async with as_tenant(app_conn, seeded.a):
            await SEEDS[table](app_conn, seeded.b, "x")


@pytest.mark.parametrize("table", TABLES)
async def test_moving_a_row_to_b_fails_with_check(
    app_conn: Any, seeded: Seeded, table: str
) -> None:
    column = tenant_tables()[table]
    with pytest.raises(asyncpg.InsufficientPrivilegeError, match="row-level security"):
        async with as_tenant(app_conn, seeded.a):
            await app_conn.execute(f"UPDATE {table} SET {column} = $1", seeded.b)  # noqa: S608


# --- 3. no GUC ----------------------------------------------------------------------------


@pytest.mark.parametrize("table", TABLES)
async def test_no_guc_means_zero_rows(app_conn: Any, seeded: Seeded, table: str) -> None:
    assert await app_conn.fetchval(f"SELECT count(*) FROM {table}") == 0  # noqa: S608


@pytest.mark.parametrize("table", TABLES)
async def test_a_reset_guc_means_zero_rows_not_an_error(
    app_conn: Any, seeded: Seeded, table: str
) -> None:
    """After a ``SET LOCAL`` ends, the setting reads ``''``, not NULL -- the pooled
    connection's normal state, and the reason every policy says ``NULLIF``."""
    async with as_tenant(app_conn, seeded.a):
        pass
    assert await app_conn.fetchval("SELECT current_setting('app.tenant_id', true)") == ""
    assert await app_conn.fetchval(f"SELECT count(*) FROM {table}") == 0  # noqa: S608


@pytest.mark.parametrize("table", TABLES)
async def test_no_guc_means_no_writes(app_conn: Any, seeded: Seeded, table: str) -> None:
    with pytest.raises(asyncpg.InsufficientPrivilegeError, match="row-level security"):
        async with as_tenant(app_conn, None):
            await SEEDS[table](app_conn, seeded.b, "x")


# --- 4. the definer functions -------------------------------------------------------------


async def test_the_definer_functions_return_a_single_uuid(app_conn: Any) -> None:
    rows = await app_conn.fetch(
        "SELECT proname, proretset, pg_get_function_result(oid) AS result FROM pg_proc "
        "WHERE proname IN ('resolve_tenant', 'create_tenant') ORDER BY proname"
    )
    assert [(row["proname"], row["proretset"], row["result"]) for row in rows] == [
        ("create_tenant", False, "uuid"),
        ("resolve_tenant", False, "uuid"),
    ]


async def test_the_definer_functions_open_nothing_else(app_conn: Any, seeded: Seeded) -> None:
    """With no GUC, the door answers with B's id -- its job -- and the session can still
    read nothing: the function's BYPASSRLS does not outlive the call."""
    key = f"tel:{PHONES['b']}"
    assert await app_conn.fetchval("SELECT resolve_tenant('whatsapp', $1)", key) == seeded.b
    assert (
        await app_conn.fetchval("SELECT create_tenant('whatsapp', $1, NULL, 'he')", key) == seeded.b
    )
    for table in TABLES:
        assert await app_conn.fetchval(f"SELECT count(*) FROM {table}") == 0  # noqa: S608
    rows = await _snapshot(app_conn, seeded.b)
    assert (len(rows["tenants"]), len(rows["tenant_identities"])) == (1, 1)
```

- [ ] **Step 2: Run it**

Run: `uv run pytest tests/db/test_isolation.py -v`
Expected: 80 PASS. Each test runs as `[app-filter-…]` and as `[rls-only-…]`. In `rls-only`, `test_the_app_filter_is_really_off_in_rls_only_mode` proves the patch took: `get_tenant(B)` under A returns A's row.

- [ ] **Step 3: Prove the suite can fail**

```bash
PGPASSWORD=owner-dev-password psql -h localhost -p 5433 -U app_owner postgres \
  -c "ALTER TABLE messages DISABLE ROW LEVEL SECURITY"
uv run pytest tests/db/test_isolation.py tests/db/test_rls.py -q   # expect ~20 failures
PGPASSWORD=owner-dev-password psql -h localhost -p 5433 -U app_owner postgres \
  -c "ALTER TABLE messages ENABLE ROW LEVEL SECURITY"
uv run pytest tests/db/test_isolation.py tests/db/test_rls.py -q   # green again
```

(Use the owner password from your `.env`.) Expected: with RLS off on `messages`, the `messages` cases of "sees only its own rows", "no GUC", "WITH CHECK" and the invariant all fail. With it back on, everything passes.

- [ ] **Step 4: Whole-branch verification (what CI runs)**

```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy
uv run po-db bootstrap && uv run alembic upgrade head && uv run po-db check
uv run pytest
uv run pytest -m rls
```

Expected: all clean. In the prototype this was 834 passed for the full suite and 85 for `-m rls`. `db.check.ok`.

- [ ] **Step 5: Commit**

```bash
git add tests/db/test_isolation.py
git commit -m "test: tenant isolation, with and without the app-level filter

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

## Self-review notes

- **Spec coverage (PR stack item 1 + contract PR 2):** `app_definer` + `DatabaseRole.DEFINER` → Task 1. `TenantRoot`, invariant → Task 2. Models, `alembic/rls.py`, 0004 (tables, indexes, RLS incl. `tenants` on `id`, definer functions with REVOKE/ownership/grants, outbox key, widened disposition check), round-trip downgrade → Task 3. `scoped()` + all repository functions → Tasks 4–5. `test_rls.py` (TenantRoot; RLS-without-FORCE fails) → Tasks 2–3. `test_roles.py` (cannot become, NOLOGIN, owned-function set, PUBLIC has no EXECUTE) → Tasks 1, 3. `test_isolation.py` (seeds in every table, A sees no B through every repository function, WITH CHECK, no GUC, definer returns only an id, missing seed factory fails, two modes) → Task 6.
- **Verified, not assumed:** every code block was run on a PG 16.15 + pgvector scratch database before it went into this plan. That covers `ALTER FUNCTION … OWNER TO app_definer` as `app_owner`, PUBLIC's default EXECUTE being gone, owner `TRUNCATE` under FORCE, RLS `WITH CHECK` firing before the PK/unique checks on `UPDATE tenants SET id = B`, and both downgrades. ruff, ruff format and mypy strict were clean.
