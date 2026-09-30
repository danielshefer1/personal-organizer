# Iteration 03 plan — Data Model, RLS & Onboarding via Composio

**Source:** v7 plan, Section 7, Iteration 03 (and Sections 3.3, 4 gates 3/5/9, 5).
**v7 dates:** Sep 28–29, 2026. **Realistic:** Sep 30 – Oct 2 (Iteration 02 Phase B is parked on
the disabled WABA, see below). Iteration 03 is on the "never cut" list, so the slip is absorbed
later by the v7 cut line (voice notes first).

## Objective and Done-When

> Tenant model with database-enforced isolation and an onboarding flow that binds a WhatsApp
> identity to a Composio Google Calendar connection.

| v7 Done-When | Where it will be proved |
|---|---|
| A new user onboards from WhatsApp and connects Google Calendar | Staging, end to end: WhatsApp emulator (or `po-whatsapp simulate`) → onboarding chat → real Composio Connect Link → real Google account → callback → "you're all set". Runbook step, plus `tests/db/test_onboarding.py` with a fake Composio. |
| Isolation tests pass with app-level tenant filters disabled | `tests/db/test_isolation.py` (`-m rls`): every repository function as tenant A returns zero tenant B rows, with the `scoped()` filter patched to a no-op so only RLS is doing the work. |

## Where we start

Iteration 01 and 02 already laid most of the RLS groundwork, so this iteration is mostly
filling in slots that exist:

- `TenantMixin` (`db/base.py`) marks tenant tables; `tests/db/test_rls.py` already asserts that
  exactly the mixin tables have RLS **enabled and forced** — it grows automatically.
- `Database.tenant_session()` + the `after_begin` listener (`db/session.py`) set
  `app.tenant_id` with `set_config(..., true)` on every transaction, re-begins included.
- Three roles: bootstrap superuser, `app_owner` (non-super, owns tables, so FORCE is real),
  `app_user` (NOBYPASSRLS, NOINHERIT, owns nothing). `test_roles.py` pins them.
- `composio`, `itsdangerous`, `jinja2` are already dependencies (composio 0.24.0:
  `connected_accounts.link(user_id, auth_config_id, callback_url=...)` → `redirect_url`).
- Deferred to us by Iteration 02's runbook: per-tenant budgets, buttons/lists, replies to
  BSUID-only senders, and moving message content out of `channel_inbox` (ADR 0002).

## Prerequisites (outside the code)

1. **Composio staging project** with the managed Google Calendar auth config: note its
   `auth_config_id` and an API key. Set the Connect Link logo and title (Iteration 00 item).
2. **A public https base URL** for the connect page and callback. Staging's Railway domain is
   enough for this iteration; the product domain (Gate 3) is needed by Iteration 08, not here.
3. **WhatsApp:** the WABA is disabled (2026-09-30), so the WhatsApp half is proved the way
   Iteration 02 was — the `@whatsapp-cloudapi` emulator on staging. **Check on day 1 that the
   emulator accepts `type: interactive` sends** (buttons, lists); if it does not, prove
   outbound interactive against the unit tests and a local stub, and inbound with
   `po-whatsapp simulate --button-reply`.
4. **Templates (Gate 5: "submit in Iteration 03")** — blocked with the WABA. Tracked, not
   worked around.

## Decisions

Each of these is not obvious from the code afterwards, so D1 and D4 get ADRs (0004, 0005).

**D1 — Resolving a sender to a tenant under RLS: a narrow `SECURITY DEFINER` door, owned by a
new BYPASSRLS role.** The worker has to answer "which tenant is this BSUID?" before it knows
a tenant, which RLS forbids by design. Rejected: leaving `tenant_identities` without RLS (a
phone directory readable by the runtime role), and a policy clause `OR current_user =
'app_owner'` (undoes FORCE, the "second guard"). Instead, bootstrap creates `app_definer`
(`NOLOGIN BYPASSRLS`, granted to `app_owner` so migrations can hand it functions — costs
nothing, since the owner can already `ALTER TABLE … DISABLE RLS`). Two functions, owned by
`app_definer`, `SET search_path = public, pg_temp`, `EXECUTE` granted to `app_user` only:

- `resolve_tenant(channel text, external_id text) RETURNS uuid` — the tenant id or NULL, nothing else.
- `create_tenant(channel text, external_id text, wa_phone text) RETURNS uuid` — inserts the
  tenant and its first identity atomically, idempotent on the identity's unique key.

This is `roles.py`'s planned Iteration 21 `SECURITY DEFINER` owner, arriving early: one new
`DatabaseRole` member, no DSN (NOLOGIN). `test_roles.py` gains: `app_user` cannot `SET ROLE
app_definer`; `app_definer` cannot log in; the function set owned by `app_definer` is exactly
the allowlisted two (catches a future migration quietly adding a third).

**D2 — Invited vs onboarded.** `WHATSAPP__ALLOWED_PHONES` stays, re-read as the **invite
list** for the ≤50-tester beta; `tenant_identities` becomes membership. The gate becomes:

| Sender | Outcome |
|---|---|
| resolves to an `active` tenant | `on_allowed` (still the fixed ack until Iteration 04) |
| resolves to an `onboarding` tenant | next onboarding step |
| no tenant, phone on the invite list | `create_tenant`, start onboarding |
| anything else | invite-only reply, purge (unchanged) |

A BSUID-only sender can only be recognised once their identity row exists; until then they
stay a stranger. Replies go to `tenant_identities.wa_phone`, which settles
`inbound.reply_unaddressable` for known tenants. DB-backed invites (no redeploy to add a
tester) are a pre-beta item, not this iteration.

**D3 — Every policy reads the GUC as `NULLIF(current_setting('app.tenant_id', true), '')::uuid`.**
After a `SET LOCAL` has been used once on a pooled connection, the setting reverts to `''`, not
NULL, and `''::uuid` raises. The plain cast would turn "no tenant" into an error on some
connections and zero rows on others. One helper in `alembic/rls.py` (`enable_tenant_rls(table,
column="tenant_id")`) emits `ENABLE`, `FORCE`, and a single `FOR ALL … USING … WITH CHECK`
policy; every migration from here on uses it.

**D4 — The connect link is consumed on POST, never on GET.** WhatsApp fetches links for
previews, so a GET that redeems a single-use token would redeem it before the user taps.
`GET /connect/{token}` renders a page with a button; `POST` validates, marks the link used and
303s to Composio. The token is in the path, so the page sets `Referrer-Policy: no-referrer`
(or the Composio redirect carries it out), `Cache-Control: no-store`, a CSP of `default-src
'none'`, and Sentry's URL scrubbing learns `/connect/*`.

**D5 — The callback trusts Composio's API, not its query string.** Composio appends
`connected_account_id` and `status` to our callback. We verify our own signed `state` (tenant +
link nonce), then fetch the connected account from Composio and require `user_id ==
str(tenant_id)`, the expected `auth_config_id`, and `ACTIVE` before binding it. A forged
callback therefore cannot bind someone else's calendar to a tenant.

**D6 — Sends that answer no inbound row get an idempotency key.** The "calendar connected"
message is triggered by the callback, which can be hit twice (refresh, back button), and
`UNIQUE (inbox_id, kind)` does not dedupe NULL `inbox_id`s. `channel_outbox` gains a nullable
unique `idempotency_key` (e.g. `connected:<connection_id>`); `send_once` accepts either key.
Iteration 09's reminders need exactly this too.

**D7 — Content moves under RLS, as ADR 0002 promised.** For a message from a known tenant the
worker copies `body` into `messages` (tenant table) and nulls `body`/`raw`/`media_*` in
`channel_inbox` in the same transaction, leaving it a transit and dedupe ledger.

**D8 — Deviation from v7: `pending_actions` and `scheduled_jobs` are not created here.** Their
columns are decided by Iterations 07 and 09; an empty table now is a guess plus a later
migration. Each arrives with `TenantMixin` in its own iteration, and the RLS invariant and
isolation suites cover it with no extra work — which is exactly what they were built for.

## Schema (migration 0004)

All tenant tables carry `TenantMixin` (except `tenants`, keyed on `id`; see note) and get
`enable_tenant_rls`.

| Table | Columns (beyond `id`, timestamps) | Notes |
|---|---|---|
| `tenants` | `status` (`onboarding`/`active`/`suspended`), `onboarding_step`, `language` (`he`/`en`), `timezone` (IANA, checked against `zoneinfo`), `working_hours` jsonb (default Sun–Thu 09–18 for +972, Mon–Fri otherwise), `briefing_time` (default 08:00), `opted_in_at`, `age_confirmed_at`, `backup_email` | Policy on `id`, not `tenant_id`. The RLS invariant test's "tenant tables" set is extended with a `TenantRoot` marker so `tenants` is required to have RLS too. |
| `tenant_identities` | `tenant_id`, `channel`, `external_id`, `wa_phone` (nullable) | `UNIQUE (channel, external_id)`. `external_id` is the BSUID, or `tel:+…` until Meta sends one (see Risks). |
| `calendar_connections` | `tenant_id`, `composio_user_id`, `connected_account_id` (unique), `auth_config_id`, `status` (`active`/`revoked`/`failed`), `connected_at` | `auth_config_id` per row is the Section 3.3 cutover prerequisite. At most one `active` per tenant (partial unique index). |
| `onboarding_links` | `tenant_id`, `nonce` (unique), `expires_at`, `used_at` | Single-use lives here; the signature only proves we issued it. |
| `messages` | `tenant_id`, `direction`, `channel`, `inbox_id` (FK, `SET NULL`), `message_type`, `body`, `sent_at` | D7. Iteration 04 writes agent replies here. |
| `audit_log` | `tenant_id`, `action`, `detail` jsonb (ids and enums only, never PII) | Consent trail: `age_confirmed`, `proactive_opt_in`, `calendar_connected`, `backup_email_set`. `app_user` gets `INSERT, SELECT` only — append-only by grant. |
| `usage_daily` | `tenant_id`, `day`, `messages_in`, `messages_out`, `tokens_in`, `tokens_out`, `composio_calls` | `UNIQUE (tenant_id, day)`, upsert-increment. Token columns are filled from Iteration 04, Composio from 05. |

Composio `user_id` is the tenant UUID (never a phone number); `composio_user_id` stores it
anyway so it can be re-keyed later without guessing.

## The onboarding conversation

The link comes last, so returning from Google *is* the end of onboarding, and the callback's
"you're all set" is sent while the user's 24-hour window is certainly open.

1. **Age gate** (bilingual, since language is not known yet). Buttons `I'm 13 or older` /
   `I'm under 13`, with the note that 13–17 need a parent's or guardian's permission.
   Under 13 → a polite refusal and the tenant is deleted with its data. No answer → nothing
   else happens and nothing but the identity is stored.
2. **Language** — buttons `עברית` / `English`. Every later string comes from a two-language
   table (`messaging/onboarding_text.py`).
3. **Time zone** — a list with a suggestion from the phone's country code first
   (`+972 → Asia/Jerusalem`), a handful of common zones, and `Other…`, which asks for a city or
   zone name and validates it against `zoneinfo`.
4. **Proactive messages** — `Yes, send me briefings` / `Not now` → `opted_in_at`.
5. **Backup email** — "so we can reach you if WhatsApp is ever unavailable"; a typed address
   or `Skip`. Validated for shape only; never logged (the redaction allowlist already hides it).
6. **Connect Google Calendar** — a signed, single-use link (15 minutes, bound to the tenant)
   → our page → Composio Connect Link (managed auth config) → our callback → D5 checks →
   `calendar_connections` row → status `active` → "You're all set" (D6 key).

Each step is idempotent on a re-run of the job, answers button taps *and* reasonable typed
equivalents ("yes", "en"), and re-prompts on anything else. An expired or used link is
answered by re-issuing a fresh one on the user's next message. Every step's consent writes an
`audit_log` row.

## PR stack

Seven stacked PRs, like Iteration 02 (`iteration-03/<n>-<name>`), each green on its own.

1. **`1-settings`** — `ComposioSettings` (`enabled`, `api_key`, `calendar_auth_config_id`,
   all-or-nothing like WhatsApp), `OnboardingSettings` (`link_secret` ≥ 32 chars,
   `link_ttl`), `APP__PUBLIC_BASE_URL` (must be https when deployed). Deployed-environment
   invariants and `tests/unit/test_settings.py` cases. `.env.example` block.
2. **`2-schema`** — models, migration 0004 via `alembic/rls.py`, `app_definer` in
   `bootstrap.sql` and `DatabaseRole`, the two definer functions, D3 policies.
   `test_rls.py` extended (`TenantRoot`, a tenant table without FORCE fails),
   `test_roles.py` extended (D1 checks), `test_models_match_migrations.py` stays green,
   downgrade round-trips (Iteration 01 learned this the hard way).
3. **`3-isolation`** — repositories (`db/repositories/`) using a single `scoped(stmt, Model)`
   helper for the app-level filter; `tests/db/test_isolation.py` seeds tenants A and B in
   every tenant table and, for each repository function and each table: A sees no B rows;
   insert/update with B's id under A fails `WITH CHECK`; no GUC → zero rows; the definer
   functions return nothing but an id. Run once normally and once with `scoped` patched out
   (the Done-When). New tables in later iterations join via a per-table seed factory, and a
   test fails if a `TenantMixin` table has no factory.
4. **`4-interactive`** — `send_buttons` / `send_list` on `OutboundChannel` and the WhatsApp
   client (3 buttons, 20-char titles, 10 list rows — enforced before the call, not by Meta's
   error); D6 `idempotency_key` on `channel_outbox`; `po-whatsapp simulate --button-reply
   <id>` / `--list-reply <id>`.
5. **`5-onboarding-chat`** — `handle_inbound` resolves the tenant (D2), routes `onboarding`
   tenants through the step machine, copies content into `messages` (D7), counts
   `usage_daily.messages_in`, and applies a per-tenant daily inbound cap (a setting; one
   "limit reached" reply per day, same mute pattern as strangers). Iteration 02's
   `test_handle_inbound.py` keeps passing unchanged for strangers.
6. **`6-connect`** — `/connect/{token}` GET/POST and `/connect/callback` (jinja2, autoescaped,
   no external assets, D4 headers), mounted only when Composio is enabled; itsdangerous
   `URLSafeTimedSerializer` for link and state; `providers/calendar/composio.py` wrapping
   `link()` and the connected-account fetch (the SDK is synchronous, so off the event loop
   via `anyio.to_thread`, with a timeout); the callback defers `onboarding:connected`
   (tenant id and connection id only, ADR 0001). Tests with a fake Composio: forged state,
   replayed link, wrong `user_id`, wrong auth config, non-ACTIVE status, double callback →
   one message.
7. **`7-docs`** — `docs/runbook-iteration-03.md` (Composio setup, env vars, the staging
   end-to-end, failure table, where Done-When is proved), ADRs 0004 (D1) and 0005 (D4/D5),
   README. Also fixes `runbook-iteration-02.md` Phase B step 1 (Meta's "Connect with customers
   through WhatsApp" use case replaced app type + product; Test Users is not needed).

## Deliberately not in this iteration

- **Calendar reads, the `CalendarProvider` implementation, usage metering of Composio calls**
  — Iteration 05. Iteration 03 only connects.
- **`pending_actions`, `scheduled_jobs`** — Iterations 07 and 09 (D8).
- **Token budgets** — the columns exist; enforcement arrives with the LLM in Iteration 04.
- **Reconnect flow on Composio auth errors** — Iteration 05 (it reuses this iteration's
  links).
- **DB-backed invites, admin CLI for tenants** — before the beta (Iteration 14).
- **Deleting a tenant on request** — Iteration 12. The under-13 deletion here is the only
  delete path, and it runs before any calendar is connected.
- **Templates** — blocked on the WABA (Gate 5).

## Risks and open questions

- **BSUID timing.** Meta's BSUID rollout means early payloads may carry only a phone. If a
  tenant's identity is keyed `tel:+…` and a BSUID later appears for the same person,
  resolution must match on phone and **add** the BSUID row, not create a second tenant.
  `resolve_tenant` checks `uid:` first, then `tel:`, and the worker backfills. Covered by a
  test with the `bsuid_only.json` and `text.json` fixtures.
- **Emulator and interactive messages** — see Prerequisites 3.
- **Composio SDK behaviour** to confirm on day 1: the exact query parameters on the callback,
  the `ComposioMultipleConnectedAccountsError` path when a tenant reconnects (expected: mark the
  old row `revoked`, allow the new one), and whether the SDK sends telemetry or logs request
  bodies that would bypass our redaction.
- **Hebrew in lists.** List-row titles are 24 characters; Hebrew zone names are longer. Use
  English IANA-ish labels with a Hebrew description line.
- **Schedule.** Three days against v7's two, starting two days late. If it slips further, the
  first thing to shrink is step 3's `Other…` free-text zone matching (fall back to the list
  only); nothing in the RLS or consent parts is cut.
