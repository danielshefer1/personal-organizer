# Iteration 03-lite plan — tenants, RLS, choices and the Google connect

**Source:** v7 Section 7, Iteration 03, cut to the project's actual purpose: a tool for the
owner and their circle (≤ ~50 invitees), a product only if it proves useful. This replaces
the first Iteration 03 plan (branch `iteration-03/0-plan`, never merged). Decisions kept from
that plan keep their numbers (D1–D8). New ones start at D9.

**Dates:** Oct 8–11, 2026 (four days). The settings (#17) and the GOWA
gateway (#18) are already on `main`.

## Objective and Done-When

> An invited person writes to the bot, confirms their time zone with a numbered reply, taps
> one link, connects Google Calendar and is told they are set up. Their data sits under RLS
> from the first message.

| Done-When | Where it is proved |
|---|---|
| An invited number onboards from WhatsApp and connects Google Calendar | Staging, end to end: your phone → the GOWA SIM → zone choice → connect link → Google's consent screen (unverified-app warning included) → callback → "You're all set". Plus `tests/db/test_onboarding.py` and `tests/api/test_connect.py` with a fake Composio. |
| Isolation holds with app-level tenant filters disabled | `tests/db/test_isolation.py` (`-m rls`), run once normally and once with `scoped()` patched to a no-op. |
| A choice works on every channel | `tests/unit/test_choices.py`: numbered replies, label and alias replies, Meta button `reply_id`, GOWA `selection.selected_id`, all resolve the same way. |

## Where we start

- `TenantMixin` (`db/base.py`): `tests/db/test_rls.py` already requires RLS enabled and forced
  on exactly the mixin tables. It grows automatically.
- `Database.tenant_session()` plus the `after_begin` listener (`db/session.py`) set
  `app.tenant_id` on every transaction, re-begins included.
- Roles: bootstrap superuser, `app_owner` (not a superuser, so FORCE is real), and `app_user`
  (NOBYPASSRLS, owns nothing).
- Channels: `messaging/runtime.py` keeps a registry by name, and a reply follows the inbound
  channel. The GOWA parser already maps list and button selections onto
  `InboundMessage.reply_id`.
- Settings: `ComposioSettings`, `OnboardingSettings` and `APP__PUBLIC_BASE_URL` exist (#17).

## Prerequisites (outside the code)

1. **Google OAuth app, published "In production", unverified.** Calendar scope only. In
   *Testing*, refresh tokens expire after about 7 days, so every user would disconnect weekly.
   Unverified production shows Google's warning screen and caps the app at 100 users, which is
   fine here. Plug its client into a **Composio custom auth config** (staging project first).
   That id is `COMPOSIO__CALENDAR_AUTH_CONFIG_ID`.
2. **The GOWA SIM linked on staging** (QR scan). It is the only live WhatsApp channel.
3. **Staging's Railway domain** as `APP__PUBLIC_BASE_URL`. No product domain is needed.
4. **Day-1 checks on the SIM** (they do not block, but they decide later work): do Meta-style
   buttons and lists sent through GOWA render on iOS and Android? Do polls, and does GOWA
   forward the votes? And what does GOWA do when our webhook answers 5xx or times out: retry,
   how often, for how long?

## Decisions

### Kept from the first plan

- **D1: tenant resolution through a narrow `SECURITY DEFINER` door.** The worker must map a
  sender to a tenant before it knows a tenant, which RLS forbids by design. Bootstrap creates
  `app_definer` (`NOLOGIN BYPASSRLS`, granted to `app_owner` so migrations can give it
  functions). It owns exactly two functions, with `SET search_path = public, pg_temp` and
  `EXECUTE` granted to `app_user` only: `resolve_tenant(network, external_id) → uuid | NULL`
  and `create_tenant(network, external_id, phone, language) → uuid` (atomic and idempotent on
  the identity key). `test_roles.py` pins three things: `app_user` cannot become
  `app_definer`, `app_definer` cannot log in, and the definer-owned function set is exactly
  these two. A new `DatabaseRole.DEFINER` member has no DSN.
- **D3: every policy reads `NULLIF(current_setting('app.tenant_id', true), '')::uuid`.** On a
  pooled connection, after a `SET LOCAL` the setting reverts to `''`, and `''::uuid` raises.
  `alembic/rls.py` provides `enable_tenant_rls(table, column="tenant_id")`, which emits
  `ENABLE`, `FORCE`, and one `FOR ALL … USING … WITH CHECK` policy.
- **D4: the connect link is consumed on POST, never on GET.** WhatsApp fetches links to build
  previews. `GET /connect/{token}` renders a page with one button. `POST` validates the token,
  marks it used and answers 303 to Composio. The page sets `Referrer-Policy: no-referrer`,
  `Cache-Control: no-store` and CSP `default-src 'none'`, and Sentry's URL scrubbing learns
  `/connect/*`.
- **D5: the callback trusts Composio's API, not its query string.** It verifies our signed
  `state` (tenant + link nonce), then fetches the connected account and requires
  `user_id == str(tenant_id)`, the expected `auth_config_id` and `ACTIVE`. Only then does it
  bind the account.
- **D6: sends that answer no inbound row carry an idempotency key.** `channel_outbox` gains a
  nullable unique `idempotency_key`, and `send_once` accepts either `(inbox_id, kind)` or the
  key. The first user is "You're all set" (`connected:<connection_id>`). Reminders in a later
  iteration need the same thing.
- **D7: content moves under RLS.** For a known tenant, the worker copies `body` into
  `messages` and nulls `body`, `raw` and `media_*` in `channel_inbox`, in one transaction, as
  ADR 0002 promised.
- **D8: no `pending_actions`, no `scheduled_jobs`.** Each arrives in the iteration that decides
  its columns.

### Changed or new

- **D2 (simplified): invited vs onboarded.** `WHATSAPP__ALLOWED_PHONES` remains the invite list
  for every channel. Who is onboarded lives in `tenant_identities`.

  | Sender | Outcome |
  |---|---|
  | resolves to an `active` tenant | `on_allowed` (still the fixed ack until the agent arrives) |
  | resolves to an `onboarding` tenant | the current onboarding step |
  | no tenant, phone on the invite list | `create_tenant`, then the first step |
  | anything else | invite-only reply and purge (unchanged) |

- **D9: one channel-neutral "choice" message, rendered as numbered text.** Linked-device
  WhatsApp cannot be relied on to show buttons, and the Confirm step for calendar writes will
  depend on choices. So the choice is a messaging concept, not a channel feature.
  `messaging/choices.py`:
  - `Choice(prompt, options)`, where `Option(id, label, aliases)` has at most 9 options, so
    every reply is a single digit.
  - `render_text(choice, language) -> str` produces the prompt, numbered options and
    "Reply with a number" (or its Hebrew text). It is sent through the existing `send_once`.
    **`OutboundChannel` does not change.**
  - `resolve(choice, row) -> Option | None` tries, in order: `row.reply_id == option.id` (a
    Meta button, or a GOWA selection); then the text as a digit (`1`, `1.`, `1)`, surrounding
    spaces ignored); then the text as a label or alias, case-folded (`yes`, `כן`). Anything
    else is `None`, and the caller decides between re-prompting and treating it as free text.
  - **The caller owns which choice is open:** `tenants.onboarding_step` now, and
    `pending_actions` later. A choice is rebuilt deterministically from that state, so no
    choice is ever stored.
  - Native rendering (Meta buttons, GOWA polls) is a later `send_choice` on the channels that
    can do it, chosen by the day-1 checks. It is not built here.
- **D10: onboarding is two steps: `zone` → `connect`.**
  - On creation, the tenant gets a language (D11). Its zone is *guessed* from the country
    code whenever the step is rendered (a small table in code: `+972 → Asia/Jerusalem`, the
    common EU codes; no guess for multi-zone codes such as `+1`). The guess is never stored.
    `tenants.timezone` is written only when the user confirms.
  - **`zone`:** a welcome line plus `Choice("Your time zone is Asia/Jerusalem?", 1 Correct,
    2 Change)`. With no guess, or after "Change", it asks "Which city are you in?". The free
    text is matched against `zoneinfo.available_timezones()`, by full name or last segment,
    case-insensitively, plus a few Hebrew aliases (ירושלים, תל אביב, ישראל). Anything it cannot
    match gets a re-prompt with an example.
  - **`connect`:** a signed, single-use link, bound to the tenant, valid for
    `ONBOARDING__LINK_TTL_S`. Any later message in this step re-sends the latest unused,
    unexpired link, and a fresh one only when there is none.
  - **Callback success** sets the tenant to `active`, clears `onboarding_step`, and sends
    "You're all set" (D6 key). Done through the `onboarding:connected` task, which carries ids
    only (ADR 0001). That message answers no inbound row, so it goes out on the channel of the
    tenant's latest inbound message (`messages.channel`, D7), to its identity's `phone`. This
    is the first instance of ADR 0004's "proactive sends need a channel choice" seam;
    reminders reuse it.
  - **Dropped from v7:** the age gate, backup email, language step (D11) and proactive
    opt-in. The opt-in is asked the first time a briefing would be sent.
- **D11: language is detected, not asked.** Any Hebrew letter (U+0590–U+05FF) in the first
  message means `he`, otherwise `en`. It is stored, and the agent can change it later. All
  onboarding strings live in a two-language table, `messaging/onboarding_text.py`.
- **D12: identities are keyed by network, not by gateway.** `tenant_identities.network` is
  `whatsapp` for both the `gowa` and the Meta `whatsapp` channel, so one person writing to
  either number is one tenant. This matches ADR 0004's "one person, one key". `external_id` is
  the `SenderRef.key` (`tel:+…`, or `uid:…` from Meta). `resolve_tenant` tries `uid:` first,
  then `tel:`. When a Meta BSUID later arrives for a phone-keyed tenant, the worker adds the
  `uid:` row rather than creating a second tenant. A future Telegram channel is the network
  `telegram`.

## Schema (migration 0004)

| Table | Columns (beyond `id` and timestamps) | Notes |
|---|---|---|
| `tenants` | `status` (`onboarding` / `active` / `suspended`), `onboarding_step` (`zone` / `connect` / NULL), `language` (`he` / `en`), `timezone` (IANA, NULL until confirmed) | Policy on `id`. A `TenantRoot` marker puts it in the RLS invariant test. |
| `tenant_identities` | `tenant_id`, `network`, `external_id`, `phone` (nullable) | `UNIQUE (network, external_id)` (D12) |
| `calendar_connections` | `tenant_id`, `composio_user_id`, `connected_account_id` (unique), `auth_config_id`, `status` (`active` / `revoked` / `failed`), `connected_at` | At most one `active` per tenant (partial unique index) |
| `onboarding_links` | `tenant_id`, `nonce` (unique), `expires_at`, `used_at` | The signature proves we issued it; this row makes it single-use |
| `messages` | `tenant_id`, `direction`, `channel`, `inbox_id` (FK, `SET NULL`), `message_type`, `body`, `sent_at` | D7 |
| `channel_outbox` | `+ idempotency_key` (nullable, unique) | D6. Not a tenant table. |

Composio's `user_id` is the tenant UUID, never a phone number.
**Dropped vs the first plan:** `audit_log`, `usage_daily` (arrives with token caps in the
agent iteration), and the columns `working_hours`, `briefing_time`, `opted_in_at`,
`age_confirmed_at` and `backup_email`.

## PR stack

Four PRs, `iteration-03/<n>-<name>`, each green on its own.

1. **`2-schema`**: the models; migration 0004 through `alembic/rls.py`; `app_definer` in
   `bootstrap.sql` and `DatabaseRole`; the two definer functions; D3 policies; the outbox key.
   - The `test_rls.py` (`TenantRoot`) and `test_roles.py` (D1) extensions.
   - The downgrade round-trips.
   - `db/repositories/` behind one `scoped(stmt, Model)` helper.
   - `tests/db/test_isolation.py`. It seeds tenants A and B in every tenant table and checks
     four things: A sees no B rows; writing B's id under A fails `WITH CHECK`; no GUC means
     zero rows; the definer functions return an id and nothing else. A `TenantMixin` table
     without a seed factory fails the suite.
2. **`3-onboarding-chat`**: `messaging/choices.py` (D9); the D2 gate in `handle_inbound`; the
   `zone` step and onboarding text (D10, D11); D7; `send_once` with `idempotency_key` (D6).
   Iteration 02's `test_handle_inbound.py` stays green for strangers.
3. **`4-connect`**:
   - The `connect` step, `/connect/{token}` GET/POST and `/connect/callback`: jinja2,
     autoescaped, no external assets, D4 headers, mounted only when Composio is enabled.
   - itsdangerous for the link and state; `providers/calendar/composio.py` for `link()` and the
     account fetch (a sync SDK, so `anyio.to_thread` with a timeout); the `onboarding:connected`
     task.
   - Tests with a fake Composio: forged state, replayed link, wrong `user_id`, wrong auth
     config, a non-ACTIVE account, and a double callback that sends one message.
4. **`5-health-docs`**:
   - **GOWA health check:** a maintenance task every 5 minutes, on workers with GOWA enabled.
     It calls the gateway's status endpoint (the logic `po-gowa status` uses) and logs at error
     level while the gateway is unreachable or not logged in. Sentry groups those into one
     issue, which is the alert. A linked device that gets logged out must never be silent.
   - **Docs:** `docs/runbook-iteration-03.md` (Google app publishing status, Composio custom
     auth config, env vars, the staging end-to-end, failure table). **ADR 0005** covers D1, D4,
     D5 and D12: tenant resolution and the connect flow, the decisions that are hard to undo.
   - **Fixes:** the README; `runbook-iteration-02.md` Phase B step 1 (Meta's "Connect with
     customers through WhatsApp" use case); and the `ComposioSettings` docstring, which still
     describes managed auth and a cutover.

## Not in this iteration

- Calendar reads, `CalendarProvider`, Composio usage metering, and reconnect on auth errors:
  all in the calendar-read iteration.
- `pending_actions`, `scheduled_jobs` (D8). Token and message budgets, `usage_daily`.
- Native buttons and polls (`send_choice`): chosen by the day-1 checks.
- Tenant deletion and data export, and DB-backed invites. Before the circle grows past a
  handful of people.
- **Dropped for good** (2026-10-07 review): the age gate and ToS flow, backup email, Google
  verification and the cutover campaign, templates.

## Ops checklist (adopted from the review; not code)

- [ ] Pause or remove the production services until the first invitee; run on staging only.
- [ ] When production comes back: Postgres backups enabled before any real data.
- [ ] Google OAuth app published "In production" (Prerequisite 1). **On day 8 after
      connecting, confirm the calendar still works**, which proves the refresh token survived.
- [ ] Day-1 GOWA checks (Prerequisite 4); record the results in ADR 0004's Consequences.
- [ ] Before the agent iteration: Anthropic deprecations page and current model ids (v7's
      Haiku 4.5 date is Oct 15, 2026).

## Risks and open questions

- **Composio with a custom, unverified auth config.** Confirm on day 1 that the Connect Link
  flow passes Google's warning screen and returns `ACTIVE`. Also confirm the callback's exact
  query parameters, what a tenant reconnecting does (`ComposioMultipleConnectedAccountsError`
  expected: mark the old row `revoked`), and whether the SDK sends telemetry or logs request
  bodies that bypass our redaction.
- **Numbered replies are only choices while a choice is open.** During `zone`, "1" is an
  answer. For an active tenant it is ordinary text. D9's "the caller owns the open choice" is
  what keeps this unambiguous.
- **City matching** is deliberately small. A user it cannot place picks again with an example
  ("Europe/London" or "London"). No geocoding.
- **Schedule.** If it slips, the first thing to shrink is D10's free-text city matching (keep
  "Correct" plus IANA names only). Nothing in RLS, D4 or D5 is cut.
