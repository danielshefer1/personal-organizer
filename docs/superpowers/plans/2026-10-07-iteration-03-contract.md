# Iteration 03-lite — cross-PR interface contract

**Spec:** `docs/plan-iteration-03.md` (approved 2026-10-07). This file fixes every name that
crosses a PR boundary, so the four per-PR plans, written in parallel, agree. A plan may add
private helpers freely. **It may not rename, re-sign or move anything listed here.** If a plan
finds an item here unworkable, it says so in a "Contract deviations" section at its top, with
the reason. It does not silently diverge.

PR order (stacked, each green on its own): `iteration-03/2-schema` → `3-onboarding-chat` →
`4-connect` → `5-health-docs`. Base: `origin/main` at `fb27a46` (GOWA and the #17 settings
merged).

## PR 2 — `2-schema` produces

**Roles** (`src/personal_organizer/db/roles.py`, `alembic/bootstrap.sql`)
- `DatabaseRole.DEFINER = "definer"`. It has no DSN: `Settings.dsn_for(DatabaseRole.DEFINER)`
  raises `MissingDatabaseRoleError` with a message saying the role is NOLOGIN.
- `DEFINER_ROLE_NAME: Final = "app_definer"` in `roles.py`. It is passed to bootstrap as the
  GUC `po.definer_role`, like the other role names.
- Bootstrap creates `app_definer` `NOLOGIN NOSUPERUSER BYPASSRLS` and grants it `USAGE, CREATE`
  on schema `public`, plus `GRANT app_definer TO app_owner`. Both are needed for
  `ALTER FUNCTION … OWNER TO app_definer` on PG16. It is **not** granted to `app_user`.

**Base** (`src/personal_organizer/db/base.py`)
- `class TenantRoot:` is a marker for a table whose own `id` is the tenant id (only `tenants`).
  The RLS invariant test treats `TenantMixin ∪ TenantRoot` tables as "must have RLS enabled and
  forced".

**Models** (`src/personal_organizer/db/models/tenant.py`, exported from `db/models/__init__.py`)
- Constants: `TENANT_STATUSES = ("onboarding", "active", "suspended")`,
  `ONBOARDING_STEPS = ("zone", "connect")`, `LANGUAGES = ("he", "en")`,
  `NETWORK_WHATSAPP: Final = "whatsapp"`,
  `CONNECTION_STATUSES = ("active", "revoked", "failed")`, `DIRECTIONS = ("in", "out")`.
- `Tenant(Base, TenantRoot)`, table `tenants`: `id: UUID` (pk, `gen_random_uuid()`),
  `status: str` (default `'onboarding'`), `onboarding_step: str | None` (default `'zone'`),
  `language: str`, `timezone: str | None`, `created_at`, `updated_at`.
- `TenantIdentity(Base, TenantMixin)`, table `tenant_identities`: `id`, `tenant_id` (FK
  `tenants.id` `ON DELETE CASCADE`), `network: str`, `external_id: str`, `phone: str | None`,
  `created_at`. `UNIQUE (network, external_id)`.
- `CalendarConnection(Base, TenantMixin)`, table `calendar_connections`: `id`, `tenant_id` (FK,
  cascade), `composio_user_id: str`, `connected_account_id: str` (unique), `auth_config_id: str`,
  `status: str` (default `'active'`), `connected_at`. Partial unique index on `(tenant_id)
  WHERE status = 'active'`.
- `OnboardingLink(Base, TenantMixin)`, table `onboarding_links`: `id`, `tenant_id` (FK,
  cascade), `nonce: str` (unique), `expires_at`, `used_at: datetime | None`, `created_at`.
- `Message(Base, TenantMixin)`, table `messages`: `id`, `tenant_id` (FK, cascade),
  `direction: str`, `channel: str`, `inbox_id: UUID | None` (FK `channel_inbox.id` `ON DELETE
  SET NULL`), `message_type: str`, `body: str | None`, `sent_at`, `created_at`.
- `ChannelOutbox` gains `idempotency_key: str | None` with `UniqueConstraint("idempotency_key")`.
  NULLs are distinct, so rows that answer an inbox row do not collide.
- `DISPOSITIONS` (in `db/models/channel.py`) gains `"onboarding"`, and migration 0004 widens
  `ck_channel_inbox_disposition` to match. A message from an onboarding tenant is
  `"onboarding"`; one from an active tenant is `"allowed"`.

**Migration** `alembic/versions/0004_tenants.py` (`down_revision = "0003_channel_ledgers"`),
using `alembic/rls.py`:
- `enable_tenant_rls(table: str, column: str = "tenant_id") -> None` emits `ENABLE`, `FORCE`, and
  one policy `tenant_isolation` `FOR ALL USING (<column> = NULLIF(current_setting('app.tenant_id',
  true), '')::uuid) WITH CHECK (same)`.
- `disable_tenant_rls(table: str) -> None` is the downgrade.
- `tenants` uses `column="id"`.

**SQL functions** (owned by `app_definer`, `SECURITY DEFINER`, `SET search_path = public,
pg_temp`, `REVOKE EXECUTE … FROM PUBLIC`, `GRANT EXECUTE … TO app_user`. Note that bootstrap's
default privileges already grant EXECUTE on functions to app_user, and PUBLIC holds EXECUTE by
default.)
- `resolve_tenant(p_network text, p_external_id text) RETURNS uuid`: the tenant id or NULL.
- `create_tenant(p_network text, p_external_id text, p_phone text, p_language text) RETURNS
  uuid` inserts a `tenants` row plus its identity. It is idempotent: on a unique conflict on
  `(network, external_id)` it returns the existing tenant id.
- The migration grants `app_definer` `SELECT, INSERT` on `tenants` and `tenant_identities`.

**Repositories** (`src/personal_organizer/db/repositories/`). Every function takes an
`AsyncSession` first. Tenant-scoped functions are called inside `Database.tenant_session(tid)`
and filter through `scoped(stmt, Model, tenant_id)` (`repositories/scope.py`), the app-level
filter the isolation suite patches out.
- `tenants.py`
  - `async def resolve_tenant(session, *, network: str, external_id: str) -> UUID | None`
    (system session; calls the SQL function)
  - `async def create_tenant(session, *, network: str, external_id: str, phone: str | None,
    language: str) -> UUID` (system session; calls the SQL function)
  - `async def get_tenant(session, tenant_id: UUID) -> Tenant | None`
  - `async def set_timezone(session, tenant_id: UUID, timezone: str) -> None`
  - `async def set_onboarding_step(session, tenant_id: UUID, step: str | None) -> None`
  - `async def activate(session, tenant_id: UUID) -> None` sets `status='active'` and
    `onboarding_step=NULL`.
  - `async def primary_phone(session, tenant_id: UUID) -> str | None` returns the `phone` of the
    tenant's earliest identity that has one.
- `links.py`
  - `async def create_link(session, tenant_id: UUID, *, nonce: str, expires_at: datetime) ->
    OnboardingLink`
  - `async def latest_usable_link(session, tenant_id: UUID, *, now: datetime) ->
    OnboardingLink | None` (unused and unexpired, newest first)
  - `async def consume_link(session, tenant_id: UUID, *, nonce: str, now: datetime) -> bool`
    is an atomic `UPDATE … SET used_at = now WHERE used_at IS NULL AND expires_at > now`. It
    returns whether a row changed.
- `connections.py`
  - `async def bind_connection(session, tenant_id: UUID, *, connected_account_id: str,
    auth_config_id: str) -> CalendarConnection` marks any `active` row for the tenant `revoked`,
    then inserts. It is idempotent on `connected_account_id`: an existing row is returned
    unchanged.
- `messages.py`
  - `async def record_inbound(session, tenant_id: UUID, *, inbox_id: UUID, channel: str,
    message_type: str, body: str | None, sent_at: datetime) -> UUID`
  - `async def latest_inbound_channel(session, tenant_id: UUID) -> str | None`

## PR 3 — `3-onboarding-chat` produces

- `src/personal_organizer/messaging/choices.py`
  - `@dataclass(frozen=True, slots=True) class Option: id: str; label: str; aliases:
    tuple[str, ...] = ()`
  - `@dataclass(frozen=True, slots=True) class Choice: prompt: str; options: tuple[Option, ...]`,
    with 1–9 options (validated in `__post_init__`).
  - `def render_text(choice: Choice, language: str) -> str`
  - `def resolve(choice: Choice, *, reply_id: str | None, text: str | None) -> Option | None`
- `src/personal_organizer/messaging/onboarding_text.py`
  - `def t(key: str, language: str, **values: str) -> str` reads a two-language table. Keys used
    across PRs: `"connect_link"` (value `url`), `"all_set"`.
- `src/personal_organizer/messaging/language.py`: `def detect_language(text: str | None) ->
  str` returns `"he"` if any char is in U+0590–U+05FF, else `"en"`.
- `src/personal_organizer/messaging/timezones.py`: `def guess_zone(phone: str | None) -> str |
  None`, `def match_zone(text: str) -> str | None`.
- `src/personal_organizer/onboarding/tokens.py`, signing with
  `itsdangerous.URLSafeTimedSerializer`:
  - `LINK_SALT: Final = "po.onboarding.link"`, `STATE_SALT: Final = "po.onboarding.state"`
  - `def sign(payload: TokenPayload, *, secret: str, salt: str) -> str`
  - `def verify(token: str, *, secret: str, salt: str, max_age_s: int) -> TokenPayload | None`
    (`None` on bad signature, expiry or shape)
  - `@dataclass(frozen=True, slots=True) class TokenPayload: tenant_id: UUID; nonce: str`
- `src/personal_organizer/onboarding/links.py`: `async def issue_or_reuse_link(db: Database,
  tenant_id: UUID, *, settings: Settings, now: datetime) -> str` returns the full URL
  `f"{settings.app.public_base_url}/connect/{token}"`.
- `src/personal_organizer/messaging/outbox.py`: `send_once(db, channel, *, inbox_id: UUID | None,
  kind: str, recipient_key: str, to: str, text: str, idempotency_key: str | None = None) -> str`.
  Exactly one of `inbox_id` / `idempotency_key` is not None (a `ValueError` otherwise). It claims
  on whichever is set.
- `handle_inbound` gains the D2 gate and D7. **Onboarding runs only when
  `settings.composio.enabled`.** When it is off, invited senders keep Iteration 02's
  acknowledgement, so staging keeps working before Composio is configured. The `"onboarding"`
  disposition already exists from PR 2.
- Onboarding outbox kinds: `"onboarding:welcome_zone"`, `"onboarding:zone_ask_city"`,
  `"onboarding:zone_retry"`, `"onboarding:connect"`, `"onboarding:connect_resend"`.

## PR 4 — `4-connect` produces

- `src/personal_organizer/providers/calendar/composio.py`
  - `@dataclass(frozen=True, slots=True) class ConnectedAccount: id: str; user_id: str;
    auth_config_id: str; status: str`
  - `class ComposioConnector` (implements the protocol below) wraps the sync SDK with
    `anyio.to_thread.run_sync` under `settings.composio.request_timeout_s`.
  - Protocol `ConnectLinker` in `src/personal_organizer/interfaces/calendar.py`, extended
    without breaking the existing `CalendarProvider`: `async def link(self, *, user_id: str,
    auth_config_id: str, callback_url: str) -> str` (redirect URL) and `async def get_account(
    self, connected_account_id: str) -> ConnectedAccount`.
- `src/personal_organizer/api/routers/connect.py`: `GET /connect/{token}`, `POST
  /connect/{token}`, `GET /connect/callback`. Mounted only when `settings.composio.enabled`.
- Worker task `ONBOARDING_CONNECTED_TASK: Final = "onboarding:connected"` in
  `src/personal_organizer/worker/tasks/onboarding.py`, kwargs `tenant_id: str, connection_id:
  str` (ids only, ADR 0001). The api's callback binds the connection and activates the tenant in
  one tenant transaction, then defers this task. The task sends `t("all_set", lang)` through
  `send_once(..., inbox_id=None, kind="onboarding:all_set",
  idempotency_key=f"connected:{connection_id}")`, on `latest_inbound_channel`, to
  `primary_phone`.

## PR 5 — `5-health-docs` produces

- Worker periodic task `GOWA_HEALTH_TASK: Final = "system:gowa_health"`, cron `*/5 * * * *`,
  registered only when `settings.gowa.enabled`. It logs `gowa.unhealthy` at **error** level
  (reaching Sentry) when the gateway is unreachable or `logged_in` is false. Otherwise it logs
  `gowa.healthy` at debug. No persisted state.
- Docs: `docs/runbook-iteration-03.md`, `docs/adr/0005-tenant-resolution-and-the-connect-flow.md`,
  README, the `runbook-iteration-02.md` Phase B step 1 fix, and the `ComposioSettings` docstring.

## Reconciliation after the four plans (2026-10-07)

What the per-PR plans added or changed, and what each later PR must honour. The plans themselves
are authoritative for detail.

- **PR 2**
  - `app_user` gets EXECUTE on the definer functions through bootstrap's default privileges, not
    an explicit GRANT. The ACL is exactly `{app_definer, app_user}`.
  - `bind_connection` raises `LookupError` when the account is bound to another tenant.
  - `create_tenant` raises `IntegrityError` for a language outside `he`/`en`.
  - `TenantMixin.tenant_id` carries the FK to `tenants.id`.
  - `alembic.ini` `prepend_sys_path = src:%(here)s/alembic`.
  - It owns the `clean_tenant_tables` and `database` fixtures. PR 3 uses PR 2's fixture and does
    not define its own.
- **PR 3**
  - Appends `add_identity(session, tenant_id, *, network, external_id, phone) -> None` to
    `db/repositories/tenants.py`, and **must add a matching case to `tests/db/test_isolation.py`**.
  - `handle_inbound` gains `settings: Settings | None = None`.
  - `InboxRow` moves to `messaging/inbox.py`, re-exported from `messaging/inbound.py`.
  - Link expiry is decided by the `onboarding_links` row, never by the token's timestamp. A resend
    signs a fresh token for the same nonce.
- **PR 4**
  - `ConnectedAccount` lives in `interfaces/calendar.py`, re-exported from
    `providers/calendar/composio.py`.
  - `link()` is called with `allow_multiple=True`.
  - Page copy goes in `onboarding/page_text.py`.
  - GET `/connect/{token}` checks the signature and the tenant's status only; POST
    (`consume_link`) is the authority on used and expired links.
  - The token's `max_age_s` must not be shorter than the remaining life of a reused link: PR 3
    re-signs on resend, so use `settings.onboarding.link_ttl_s`.
- **PR 5**
  - Four unhealthy reasons: `unreachable`, `not_connected`, `not_logged_in`, `bad_response`.
  - The alert is an explicit Sentry `capture_message` with a fixed fingerprint, because log lines
    never reach Sentry (`LoggingIntegration(event_level=None)`).
  - Registration happens in `build_procrastinate_app` when `settings.gowa.enabled`; `REGISTRARS`
    keeps its shape.
