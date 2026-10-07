# ADR 0005 — Tenants are resolved through a narrow definer door, and the connect flow trusts only what it signed or fetched

**Status:** accepted (Iteration 03)

## Context

From Iteration 03 every table that holds a person's data is a tenant table: RLS enabled and
*forced*, with one policy reading `NULLIF(current_setting('app.tenant_id', true), '')::uuid`.
The api and worker connect as `app_user`, which owns nothing and cannot bypass RLS, and
`Database.tenant_session(tid)` sets `app.tenant_id` on every transaction. That is the point:
a missing `WHERE tenant_id = …` returns nothing rather than someone else's rows.

It leaves four problems, and each one is easy to solve in a way that quietly undoes the
point:

1. **The worker must find the tenant before it knows the tenant.** An inbound message carries
   a sender key (`tel:+…`, or `uid:…` from Meta), not a tenant id. Mapping one to the other
   means reading `tenant_identities`, which RLS hides until `app.tenant_id` is set, and it
   cannot be set yet. Creating a tenant for a new invitee has the same problem.
2. **One person can reach us on two gateways.** The GOWA gateway and Meta's Cloud API are two
   channels (`gowa`, `whatsapp`) for one network. ADR 0004 already decided that a person has
   one key across them.
3. **WhatsApp opens links before people do.** To build a preview, it fetches every URL in a
   message, from the sender's phone or from WhatsApp's servers. A connect link that does
   something on GET is used up, or acted on, by a robot.
4. **Composio's callback arrives as a browser redirect.** Its query string (the connected account
   id, our `state`) passes through the user's browser, so anyone can type it. A callback that
   believed it would let a person bind someone else's Google account to their tenant, or
   bind a failed connection.

## Decision

1. **Tenant resolution goes through a narrow `SECURITY DEFINER` door (D1).**
   - Bootstrap creates `app_definer`: `NOLOGIN`, `BYPASSRLS`, granted to `app_owner` so
     that migration 0004 can hand it functions, and **not** granted to `app_user`.
   - It owns exactly two functions, each with `SET search_path = public, pg_temp`, `EXECUTE`
     revoked from `PUBLIC` and granted to `app_user` only:
     - `resolve_tenant(network, external_id) → uuid | NULL`
     - `create_tenant(network, external_id, phone, language) → uuid`, atomic and idempotent
       on the identity key: a concurrent second call returns the first call's tenant. If the
       winner's row is not visible to the loser, the function raises `serialization_failure`
       (`create_tenant: identity vanished`) rather than return NULL, and the Python wrapper
       raises `RuntimeError` if a NULL ever reaches it, so a `None` can never become a
       `tenant_session(None)`.
   - The definer holds only what they need: `USAGE, CREATE` on `public` (bootstrap, for
     `ALTER FUNCTION … OWNER TO`) and `SELECT, INSERT` on `tenants` and `tenant_identities`
     (migration 0004). `app_user`'s `EXECUTE` comes from bootstrap's default privileges on
     functions; a test pins that nobody else has it.
   - They return an id and nothing else. Everything after that runs in
     `tenant_session(tid)`, under RLS like the rest of the code. The two calls themselves run
     in `Database.system_session()`, as `app_user`.
   - `DatabaseRole.DEFINER` exists so that code can name the role, and it has no DSN:
     `Settings` refuses to return one (`MissingDatabaseRoleError`). `tests/db/test_roles.py`
     pins that `app_user` cannot become `app_definer`, that `app_definer` cannot log in, and
     that the definer-owned function set is exactly these two.
2. **Identities are keyed by network, not by gateway (D12).** `tenant_identities.network` is
   `NETWORK_WHATSAPP` (`whatsapp`) for both the `gowa` and the `whatsapp` channel, with
   `UNIQUE (network, external_id)`. `external_id` is `tel:+…` or `uid:…` (a Meta BSUID).
   - `resolve_sender` tries the keys strongest first (`uid:`, then `tel:`), and links every
     key of the message not yet recorded to the tenant it found.
   - `enrol` creates the tenant on the sender's **`tel:` key** whenever there is a phone (an
     invited sender always has one), then links the other keys. The gateway and Meta see one
     person under different first keys; if each created on its own first key, two first
     messages racing on the two numbers would both win and split the person into two tenants.
     Creating on the shared `tel:` key makes both calls meet at the same `create_tenant`,
     whose race-safety is on one key.
   - A key already owned by another tenant is never reassigned. It is skipped and logged as
     `tenant.identity_conflict` (tenant id and channel only, no key).
   - A future Telegram channel is the network `telegram`.
3. **The connect link is consumed on POST, never on GET (D4).**
   - `GET /connect/{token}` changes nothing. It renders a page with one button.
   - `POST /connect/{token}` verifies the signature (itsdangerous, `ONBOARDING__LINK_SECRET`),
     then spends the link with one atomic
     `UPDATE onboarding_links SET used_at = now WHERE used_at IS NULL AND expires_at > now`,
     then answers 303 to Composio. The signature proves we issued the link; `used_at` makes it
     single-use; `ONBOARDING__LINK_TTL_S` bounds how long it can be spent.
   - GET checks only the signature and the tenant's status. POST (`consume_link`) is the
     authority on use and expiry, and an active tenant's POST does not spend the link.
   - POST spends the link *before* calling Composio. If Composio then fails, the user gets the
     503 `unavailable_new_link` page and must ask the bot for a new link. That is deliberate:
     a double tap produces one redirect, not two.
   - A token stays *recognised* for 30 days (`RECOGNISED_MAX_AGE_S`), but that changes the
     outcome only for a tenant who is already active, who sees "already connected". For anyone
     else, an expired link gets the same 410 `link_unusable` page as a forged one. The callback
     `state` is recognised for 30 days in the same way (`_late`), so a connected tenant
     reloading an old callback tab sees "connected".
   - The page sends `Referrer-Policy: no-referrer`, `Cache-Control: no-store` and
     `Content-Security-Policy: default-src 'none'` (an inline stylesheet, by hash, and a form
     action are allowed back in; no external asset, no script).
   - Logs and Sentry scrub `/connect/<token>` paths and `state=` values.
4. **The callback trusts Composio's API, not its query string (D5).** `GET /connect/callback`
   first verifies our own signed `state` (tenant id and link nonce; `STATE_SALT`, one hour).
   It then fetches the connected account from Composio by id and requires three things:
   `user_id == str(tenant_id)`, `auth_config_id ==` our configured one, and status
   `ACCOUNT_ACTIVE` (`ACTIVE`). `INITIATED` and `INITIALIZING` give a "not ready, reload" page;
   `FAILED`, `EXPIRED`, `INACTIVE`, `REVOKED` and anything else give "failed". Only an
   `ACTIVE` account is bound, together with activating the tenant, in one tenant transaction,
   and `onboarding:connected` is deferred (ids only). It sends "You're all set" once
   (idempotency key `connected:<connection_id>`). Composio's `user_id` is always the tenant
   UUID, never a phone number.
   - An account already bound to another tenant gives a 409 page, writes and defers nothing,
     and logs `connect.account_conflict` with a Sentry message. Two concurrent binds for one
     tenant give a 503 "try again" page (the partial unique index
     `uq_calendar_connections_tenant_id_active`).

## Alternatives rejected

- **A `BYPASSRLS` runtime role, or a worker connection as `app_owner`, for the lookup.** The
  worker would hold a key to every tenant's data for the sake of one query, and a bug
  anywhere in it would use that key. `app_owner` could also `ALTER` or `DISABLE` the policies.
- **Leaving `tenant_identities` without RLS.** The lookup becomes a plain query, but the table
  of who-is-which-phone becomes readable by every query, by any code path.
- **A policy escape hatch, such as `OR current_setting('app.resolving') = 'on'`.** `app_user`
  can set any custom GUC, so the hatch is open to every query.
- **Resolving the tenant at ingress, in the api.** ADR 0002 keeps ingress tenant-free: one
  transaction that stores and defers, before anything is known about the sender.
- **Identities keyed by channel (`gowa` / `whatsapp`).** The same person writing to both
  numbers would become two tenants with two calendars, and merging tenants later is far
  harder than never splitting them.
- **Keyed by phone number alone.** Meta's BSUID users may have no phone number in the
  payload.
- **Consuming the link on GET**, or a GET page that auto-submits. A preview fetch would use up
  the link, and CSP `default-src 'none'` forbids the script an auto-submit needs anyway.
- **A long-lived link with no single-use row.** A forwarded or screenshotted link could be
  replayed until it expired.
- **Trusting the callback's account id and status parameters.** They are forgeable by anyone
  who can type a URL.
- **Binding `state` to the browser with a cookie.** Google sign-in on a phone can hop to
  another browser, which would then fail a legitimate connection.
- **Composio webhooks instead of a fetch.** That is another endpoint and another secret, and
  it is asynchronous, so the page cannot say "connected". The fetch is one call, at the moment
  the user is looking.

## Consequences

- **Bootstrap must run before migration 0004.** The migration hands the functions to
  `app_definer`, which only bootstrap creates. On Railway the api's pre-deploy command runs
  `po-db bootstrap` before `alembic upgrade head`; by hand, run them in that order
  (`docs/runbook-iteration-03.md`, section 1).
- **A third definer function is an ADR-level change.** The test that pins the set is the
  tripwire. Prefer a function that returns an id, as these two do, over one that returns
  rows.
- `resolve_tenant` is an oracle for "is this sender key a tenant?" to anyone who can run SQL
  as `app_user`. That role already reads `channel_inbox`'s sender keys, so this exposes
  nothing new.
- **The connect link is a bearer credential until it is spent, and `state` binds a tenant and
  a nonce, not a browser.** If an invitee forwards the Composio redirect they were handed to
  someone else, that person's Google account is bound to the invitee's tenant. We accept this:
  the invitee is a person we invited, the harm is to themselves, and a cookie binding costs
  real users a failed sign-in (rejected above). Forwarding the *link* before the tap just
  means the first tap wins.
- Composio never sees a phone number: its `user_id` is our tenant UUID. Deleting a tenant
  (a later iteration) will have to delete its Composio connected accounts by that id.
- Rotating `ONBOARDING__LINK_SECRET` invalidates every outstanding link and in-flight
  callback. That costs a user one fresh link, and nothing else.
- **Superseded and orphaned Composio accounts stay `ACTIVE`.** We create links with
  `allow_multiple=True`, because Composio's default refuses a user who already has an `ACTIVE`
  account, and a tenant whose first attempt reached `ACTIVE` at Composio but whose callback
  never arrived would be stuck for good. The common result is an **unbound** `ACTIVE` account
  at Composio, holding Google tokens, with no `calendar_connections` row at all. A revoked row
  is the rare case: there is no reconnect flow yet (an active tenant's link gets "already
  connected"), so the only revoke today is a second account bound by a callback arriving
  within the state's hour, when `bind_connection` marks the old row `revoked`. Either way a
  tenant has at most one active row, but Composio may hold more accounts than we do.
  **The calendar-read iteration must name the bound `connected_account_id` on every call,
  never "the user's account", and must disable or delete superseded or orphaned accounts
  (revoking one of our rows should do so too).**
- Merging two tenants (a person on WhatsApp and, later, on Telegram) is not supported. It
  will need its own linking flow, not a change to the identity key.

### Day-1 results (Composio with our own Google client)

Recorded from `docs/runbook-iteration-03.md` section 6 once they are run: whether the
connect link passes Google's unverified-app screen and returns `ACTIVE`; the names of the
callback's query parameters (`connected_account_id` or `connectedAccountId`) and that `state`
survives the round trip; what a second connect attempt does; and whether the SDK logs request bodies or
sends telemetry.
