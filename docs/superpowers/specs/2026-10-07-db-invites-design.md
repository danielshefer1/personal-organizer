# DB-backed invites and `po-admin` — design

Date: 2026-10-07. Status: approved in conversation, awaiting spec review.

## Why

The organizer is a tool for the owner and their circle (≤ ~50 people). Today the invite list
is `WHATSAPP__ALLOWED_PHONES`: inviting one person means editing a Railway variable and
redeploying the api and the worker. `docs/plan-iteration-03.md` deferred "DB-backed invites"
until "before the circle grows past a handful of people". This is that item.

**Done when:** on staging, `po-admin invite +<number>` prints a `wa.me` link, that person
writes to the bot and onboards with no redeploy, `po-admin list` shows the invite as used,
and `po-admin suspend` / `unsuspend` turn them away and back again.

## What the owner asked for

- An `invites` table: the phone, who invited them, when it was used or revoked. Outside
  per-tenant RLS, like the inbox log, because an invite exists before its tenant does.
- An admin CLI: `po-admin invite`, `revoke`, `suspend` (plus `unsuspend` and `list`).
- The gate checks the table, with `WHATSAPP__ALLOWED_PHONES` kept as a fallback, so the
  owner's own number can never be locked out.
- Later, not now: invitations from inside the chat.

## Decisions

- **I1: revoke and suspend are separate.** The gate already lets identity win over the
  invite list: an onboarded person stays served after their number leaves the list
  (`messaging/inbound.py`). So `revoke` only cancels an **unused** invite, and refuses a used
  one, pointing at `suspend`. `suspend` sets `tenants.status = 'suspended'`, which the gate
  already turns away. `unsuspend` reverses it.
- **I2: the bot never writes first.** `po-admin invite` prints a `wa.me` link the owner
  forwards. An unsolicited first message is what gets a QR-gateway number banned, and Meta
  requires a template for it, which this project dropped.
- **I3: the gate reads the table as `app_user` (approach A).** No new role, no change to the
  `SECURITY DEFINER` functions. Rejected: enforcing the invite inside `create_tenant` (B) —
  stronger, but the env fallback would have to be passed in as a parameter anyway, and it
  means a function migration for a 50-person circle; editing the Railway variable from the
  CLI (C) — every invite would still redeploy.
- **I4: no prefilled text in the link.** The tenant's language is decided by their first
  message (D11 in `docs/plan-iteration-03.md`); a prefilled "Hi" would make every invitee
  English.

## Data: `invites`

Migration `0005_invites`, model `Invite` in `src/personal_organizer/db/models/invite.py`.

| Column | Type | Notes |
|---|---|---|
| `id` | `uuid` PK | `gen_random_uuid()` |
| `phone` | `text NOT NULL` | E.164, normalised with `core.phone.normalise_e164` before it is stored |
| `note` | `text NULL` | The owner's label, e.g. "Mom". Shown by `list` only |
| `invited_by_tenant_id` | `uuid NULL` FK `tenants(id) ON DELETE SET NULL` | `NULL` = the owner, from the CLI. Chat invitations fill it later |
| `created_at` | `timestamptz NOT NULL DEFAULT now()` | |
| `used_at` | `timestamptz NULL` | Set when `enrol` creates their tenant |
| `revoked_at` | `timestamptz NULL` | Set by `po-admin revoke` |

- **Check** `ck_invites_used_or_revoked`: `NOT (used_at IS NOT NULL AND revoked_at IS NOT NULL)`.
- **Partial unique index** `uq_invites_open_phone` on `(phone) WHERE used_at IS NULL AND
  revoked_at IS NULL`: at most one open invite per number. Used and revoked rows stay as
  history.
- **No RLS.** Not a `TenantMixin`/`TenantRoot` model. The FK check on `invited_by_tenant_id`
  is a referential-integrity check, which RLS does not filter.
- **Grants.** Bootstrap's default privileges give `app_user` `SELECT, INSERT, UPDATE, DELETE`
  on every new table. The migration runs `REVOKE DELETE ON invites FROM app_user`: invites are
  history, never deleted by the app.
- The phone is stored in the clear, as `channel_inbox.sender_phone` is: the gate must look it
  up and `list` must show it. It never reaches a log line.

## The gate

SQL lives in a new repository, `src/personal_organizer/db/repositories/invites.py`, taking a
session like the others: `has_open_invite`, `create_invite`, `revoke_invite`,
`list_invites`, and `mark_used(session, phone) -> bool` — `UPDATE invites SET used_at = now()
WHERE phone = :phone AND used_at IS NULL AND revoked_at IS NULL`, returning whether a row
changed. Because it takes the caller's session, it commits with the caller's transaction.

New module `src/personal_organizer/messaging/invites.py`:

- `async def is_invited(db, phone: str | None, env_list: frozenset[str]) -> bool` — `False`
  for `None`; `True` if `phone in env_list` (no query); otherwise `has_open_invite`, read in
  a `system_session`.

Changes to `messaging/inbound.py`:

- `_tenant_gate`: `invited` becomes `await is_invited(db, row.sender_phone, allowlist)`,
  evaluated only where it matters today — no tenant resolved and the row not stale. A sender
  who resolves to a tenant never triggers the query.
- `_allowlist_gate` (Composio off): `allowed` uses the same function, so DB invites work on
  both gates. It never marks an invite used; that gate creates no tenant.

Changes to `messaging/tenancy.py`:

- `enrol` calls `mark_used(session, row.sender_phone)` inside the same `system_session` that
  calls `create_tenant`, so the tenant and the used invite commit together. Racing first
  messages meet at the same tenant (`create_tenant` is race-safe on the key); the second
  `mark_used` changes nothing. A number on the env list with an open invite also has the
  invite marked used. When a row changed, log `invite.used` with `tenant_id`.

**Accepted race.** An invite revoked between the gate's `is_invited` and `enrol` still
enrols that person. The window is milliseconds; `po-admin suspend` covers it.

## `po-admin`

`src/personal_organizer/admin/cli.py`, registered in `pyproject.toml` as
`po-admin = "personal_organizer.admin.cli:main"`. Argparse subcommands, like `po-db`. Runs as
`app_user` with the normal settings. Every number argument is normalised with
`normalise_e164`; an invalid one exits 1 with `not an E.164 number` (the input is not echoed).

| Command | Behaviour | Exit |
|---|---|---|
| `invite <phone> [--note TEXT]` | If the number resolves to a tenant: refuse, `already a member (<status>)`. If an open invite exists: create nothing, print the link again. Else insert the invite and print the link. | 0, or 1 on refusal |
| `revoke <phone>` | Set `revoked_at` on the open invite. None open and a used one exists: refuse, `invite already used; use suspend`. Nothing at all: refuse, `no open invite`. | 0 / 1 |
| `suspend <phone>` | On `WHATSAPP__ALLOWED_PHONES`: refuse, `on WHATSAPP__ALLOWED_PHONES; remove it there first`. No tenant: refuse, `not a member`. Already suspended: print so, exit 0. Else `status = 'suspended'`. | 0 / 1 |
| `unsuspend <phone>` | No tenant: refuse. Not suspended: print its status, exit 0. Else status becomes `active` if `onboarding_step IS NULL` (what `activate()` leaves), otherwise `onboarding`. | 0 / 1 |
| `list` | One line per invite, newest first: phone, note, `open` / `used` / `revoked`, date. For used invites, the tenant's current status (one `resolve_tenant` + `get_tenant` per row; fine at ~50). | 0 |

- **Finding the tenant:** `resolve_tenant(network='whatsapp', external_id='tel:<phone>')`
  through the existing definer function, then `get_tenant` / the status update in a
  `tenant_session`. A new repository helper `set_status(session, tenant_id, status)` sits
  beside `activate()`.
- **The link:** new optional setting `ONBOARDING__BOT_PHONE` (E.164, validated like the
  allowlist entries; the error counts and never echoes). Printed as `https://wa.me/<digits>`,
  with no `?text=` (I4). Unset: the invite is still recorded, and the CLI prints `no
  ONBOARDING__BOT_PHONE; send them the bot's number yourself`.
- **Where it runs:** on staging, `railway ssh --service worker --environment staging`, then
  `po-admin …` in the container. `railway run` from a laptop gets the private Postgres host,
  which does not resolve outside Railway; confirm this on staging while implementing and
  document whichever works.

## Logging

- Operator output (numbers, links, the list) goes to **stdout via `print`**, never structlog.
  `railway ssh` output does not land in the service logs.
- Events, with only allowlisted keys: `admin.invite.created`, `admin.invite.revoked`,
  `admin.tenant.suspended`, `admin.tenant.unsuspended` carry `sender_hash` (via the existing
  `hash_identifier`) and `tenant_id` where there is one; `invite.used` from `enrol` carries
  `tenant_id`. Any new key goes into `SAFE_KEYS`.

## Tests

`tests/db/` (Postgres, as `app_user`):

- An open invite and a first message (Composio on) create a tenant and mark the invite used.
- A revoked invite: the invite-only reply, content purged, no tenant.
- An empty `invites` table and the number on the env list: still enrols (the fallback).
- The allowlist gate (Composio off) treats a DB-invited number as allowed and leaves the
  invite open.
- Two concurrent first messages: one tenant, the invite used once.
- The partial unique index rejects a second open invite; a new invite after a revoke is fine.
- `suspend` → the next message gets the invite-only line; `unsuspend` restores `active` or
  `onboarding` from `onboarding_step`.
- `app_user` cannot `DELETE FROM invites`.
- `test_channel_ledgers_are_not_tenant_tables` (rename to cover all non-RLS tables) includes
  `invites` with RLS off; `test_models_match_migrations` covers the new model.

CLI tests (`tests/db/` where they touch the database, `tests/unit/` otherwise): each
command's success path and every refusal in the table above, including an invalid number,
suspending an env-list number, and an unset `ONBOARDING__BOT_PHONE`. A log-capture test
asserts no phone number appears in any log line from the CLI or from `enrol`, as the GOWA
no-PII test does.

## Docs

- **ADR 0006**, `docs/adr/0006-invites-in-the-database.md`: the table outside RLS, the env
  list as the fallback, revoke vs suspend, no first message from the bot.
- **Runbook:** an "Inviting people" section (`railway ssh`, each command, what the invitee
  sees, how to cut someone off) in `docs/runbook-iteration-03.md`, and
  `ONBOARDING__BOT_PHONE` in its settings table. `WHATSAPP__ALLOWED_PHONES` is described as
  the fallback list: the owner's own number at least.
- **README:** the invite list line and the `po-admin` command.
- **`docs/plan-iteration-03.md`:** mark "DB-backed invites" as done, pointing at ADR 0006.

## Not in scope

Invitations from chat (the `invited_by_tenant_id` column leaves room), invite expiry, a cap
on invites, deleting tenants or data export, and listing tenants who never had a DB invite
(the env-list ones).
