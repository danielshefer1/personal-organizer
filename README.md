# personal-organizer

A WhatsApp AI personal organizer: calendar, reminders and long-term memory, reachable over
WhatsApp. This repository is the implementation of the v7 plan. **Iteration 01 (walking
skeleton)**, **Iteration 02 (WhatsApp webhook, queue and access controls)** and **Iteration
03 (tenants under RLS, onboarding and the Google Calendar connect)** exist. A sender invited
with `po-admin invite` (or on `WHATSAPP__ALLOWED_PHONES`) is onboarded, in Hebrew or English:
they confirm a time zone with a numbered reply, then tap one link to connect Google Calendar.
After that they get a fixed acknowledgement. Everyone else gets a one-line "invite-only"
reply. There is no agent yet.

WhatsApp arrives through either of two channels, separately switched and able to run side
by side: Meta's Cloud API (`whatsapp`), and the GOWA QR-code gateway (`gowa`), which links a
dedicated SIM as a WhatsApp Web device while Meta's business verification is pending
(docs/adr/0004). A reply always goes out on the channel its message came in on.

## What is here

| | |
|---|---|
| `src/personal_organizer/api/` | FastAPI service. `/health` (liveness), `/ready` (readiness, monitoring only), `/webhooks/whatsapp` and `/webhooks/gowa` (each when enabled), `/connect/*` (when Composio is enabled). |
| `src/personal_organizer/worker/` | Procrastinate worker and task registry, including `system:gowa_health`, the gateway health alarm (a failed check logs `gowa.unhealthy` and reports it to Sentry as one issue). |
| `src/personal_organizer/db/` | Async engines per role, tenant-scoped sessions, bootstrap, `po-db`, ORM models, and `repositories/`: tenant-scoped queries, each also filtered through `scoped()`. |
| `src/personal_organizer/messaging/` | Provider-neutral ingress (persist-then-ack), the invite and onboarding gate, numbered choices, at-most-once replies. |
| `src/personal_organizer/admin/` | `po-admin`: invite, revoke, suspend, unsuspend and list the circle (docs/adr/0006). |
| `src/personal_organizer/onboarding/` | Signed, single-use connect links and the /connect page texts. |
| `src/personal_organizer/providers/calendar/` | Composio: the connect link and the connected-account check. |
| `src/personal_organizer/providers/channel/whatsapp/` | Meta Cloud API: payload parser, Graph sends, `po-whatsapp`. |
| `src/personal_organizer/providers/channel/gowa/` | The GOWA gateway: payload parser, REST sends, `po-gowa`. |
| `src/personal_organizer/providers/channel/hmac_sha256.py` | `X-Hub-Signature-256` verification, which both sign with. |
| `src/personal_organizer/observability/` | structlog + PII redaction, Sentry, Langfuse. |
| `src/personal_organizer/interfaces/` | `LLMProvider`, `CalendarProvider`, `MemoryStore`, `InboundChannel`/`OutboundChannel`, `TasksProvider`, `ExportService`. |
| `alembic/` | Migrations, `bootstrap.sql`, `rls.py` (the one way a tenant table gets its policy), vendored Procrastinate schema. |
| `docs/adr/` | Decisions that are not obvious from the code, including 0005 (`docs/adr/0005-tenant-resolution-and-the-connect-flow.md`: the definer door and the connect flow). |
| `docs/runbook-*.md` | Setup and operations: iteration 01 (Railway), 02 (Meta), `runbook-whatsapp-gateway.md`, and `docs/runbook-iteration-03.md` (Google OAuth app, Composio, onboarding variables, staging end to end). |
| `scripts/promote` | Fast-forwards `production` onto a green commit of `main`. |

## Getting started

Prerequisites: `uv`, and a container runtime for the local database. On NixOS, apply
`docs/nixos-docker.patch` to `~/.dotfiles` and rebuild — see the header of that file.

```sh
cp .env.example .env          # defaults point at the compose database
docker compose up -d          # PostgreSQL 16 + pgvector on localhost:5433
uv sync --all-groups
uv run pre-commit install

uv run po-db bootstrap        # extension, roles, ownership, default privileges; re-run after pulls
uv run alembic upgrade head   # runs as app_owner
uv run po-db check            # asserts app_user is correctly constrained

uv run pytest
```

Run the two services:

```sh
uv run uvicorn personal_organizer.api.main:app --reload --no-access-log
uv run po-worker
```

Then prove the skeleton end to end — api, database, queue, worker:

```sh
# The token is APP__INTERNAL_TOKEN from .env. /internal answers 404 without it.
curl -X POST -H "X-Internal-Token: local-dev-internal-token" \
  localhost:8000/internal/ping               # worker logs {"event": "ping.ok", ...}
```

### WhatsApp without a Meta app

Set `WHATSAPP__ENABLED=true` in `.env` with self-made credentials (the block at the bottom of
`.env.example`), allowlist your own number, restart both services, and play Meta:

```sh
uv run po-whatsapp simulate --url http://localhost:8000/webhooks/whatsapp \
  --from +31612345678 --replay 10         # ten POSTs, one inbox row, one job
```

The worker then tries to reply through the Graph API and, with a made-up token, logs
`whatsapp.graph_error error_code: 190` — expected. Point `WHATSAPP__GRAPH_BASE_URL` at a
local stub to see the whole happy path. Connecting a real Meta app is
`docs/runbook-iteration-02.md`.

The gateway channel works the same way, and needs no Meta at all: set `GOWA__ENABLED=true`
with self-made credentials, then

```sh
uv run po-gowa simulate --url http://localhost:8000/webhooks/gowa --from +31612345678 --replay 10
```

```sh
uv run po-admin invite +31612345678 --note "me"   # then simulate a message from that number
uv run po-admin list
```

To link a real phone, `docker compose --profile gateway up -d gowa` runs the gateway
locally. Both, and staging, are `docs/runbook-whatsapp-gateway.md`.

### Onboarding and the Google connect

Onboarding runs only with `COMPOSIO__ENABLED=true`. Off (the default), invited senders get
the fixed acknowledgement, as in Iteration 02. On, a first message from an invited number
creates its tenant and starts two steps: the time zone, as a numbered choice, then a signed,
single-use link to connect Google Calendar through Composio. The link is consumed on POST,
never on GET, because WhatsApp fetches links to build previews. Composio's callback is
believed only after a fetch of the account from Composio's API (docs/adr/0005). The Google
OAuth app, the Composio auth config and every variable are in
`docs/runbook-iteration-03.md`.

### The local database holds nothing real

Staging and production data live only on Railway. The local container exists so the test
suite can run offline; the database tests truncate between cases, so they must never point
at a deployed database. CI uses its own throwaway service container for the same reason.

Without a database, `uv run pytest` still runs — the database tests skip.

## Three things worth knowing before changing this code

**Database roles.** There are four, not two: a superuser for bootstrap only; `app_owner`,
which owns everything and runs Alembic; `app_user`, which the api and worker connect as; and
`app_definer`, which cannot log in and owns exactly two `SECURITY DEFINER` functions, the
only way to map a sender to a tenant before RLS knows the tenant (docs/adr/0005).
`app_owner` is deliberately *not* a superuser, because superusers bypass RLS and would make
`FORCE ROW LEVEL SECURITY` inert. Bootstrap creates the roles, so run it before migrating;
the api pre-deploy does the same. See `src/personal_organizer/db/roles.py`.

**Logging is an allowlist.** Any key not in `SAFE_KEYS` is replaced with a shape summary, so
`log.info("x", payload=msg)` cannot leak. Adding a field to `SAFE_KEYS` to "fix" a redacted
log line is the wrong fix. See `src/personal_organizer/observability/redaction.py` and the
acceptance test `tests/api/test_no_pii_in_logs.py`.

**Task arguments carry identifiers, never content.** Procrastinate logs the full repr of
every task kwarg into its own log messages. See `docs/adr/0001`.

**Request text is scrubbed in bounded time.** Connect links and OAuth `state=` values are
redacted from logs and Sentry events, but only inside a bounded window with linear rules, so
a huge path or header cannot stall the scrubber. `X-Request-ID` is accepted only if it matches
`[A-Za-z0-9._:-]{1,128}`. See `src/personal_organizer/observability/redaction.py` and `src/personal_organizer/api/middleware.py`.

## Deployment

Railway, EU West (`europe-west4-drams3a`), two environments. `main` deploys to staging; the
`production` branch deploys to production. Migrations run as a pre-deploy command, never at
app startup.

`production` is a pointer into `main`, moved forward by `scripts/promote` — never a parallel
line of development, and never moved backwards. `docs/promotion.md` is the model, the rules the
branch rulesets enforce, and what to do instead of a rollback.

There are no `railway.*.json` files: Railway's Config as Code is deprecated and cannot be
enabled for a service created after 2026-08-28, so the start command lives in the Dockerfile
`CMD` (dispatching on `APP__COMPONENT`) and the api's pre-deploy command and healthcheck path
are set per service in the dashboard.

Setting an environment up the first time — every variable, and the ways it bites if you miss
one — is `docs/runbook-iteration-01.md`. The short version: a deployed service **refuses to
boot** without `SENTRY__DSN`, a real `LOGGING__PII_PEPPER`, `DATABASE__OWNER_URL`, and (in
staging only) `APP__INTERNAL_TOKEN`, so the Sentry project has to exist before the first deploy.

`APP__ENV` is the one to get right first, because every check in that list is gated on it and
it defaults to `local`. Omitting it used to switch all of them off in silence rather than
failing; a deployed container that does not declare its environment now refuses to start.

WhatsApp is off unless `WHATSAPP__ENABLED=true` or `GOWA__ENABLED=true` (or both), and then
each one's credentials are required. Connecting Meta — app, test number, webhook
registration, and what each failure looks like — is `docs/runbook-iteration-02.md`;
connecting the gateway is `docs/runbook-whatsapp-gateway.md`.

Onboarding and the Google connect are off unless `COMPOSIO__ENABLED=true`, and then
`COMPOSIO__API_KEY`, `COMPOSIO__CALENDAR_AUTH_CONFIG_ID`, `APP__PUBLIC_BASE_URL` and
`ONBOARDING__LINK_SECRET` are required on both services. The Google OAuth app, the Composio
auth config, those variables and the staging end to end are `docs/runbook-iteration-03.md`.
