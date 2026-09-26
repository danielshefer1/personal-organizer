# personal-organizer

A WhatsApp AI personal organizer: calendar, reminders and long-term memory, reachable over
WhatsApp. This repository is the implementation of the v7 plan; **Iteration 01 (walking
skeleton)** is what currently exists.

## What is here

| | |
|---|---|
| `src/personal_organizer/api/` | FastAPI service. `/health` (liveness), `/ready` (readiness, monitoring only). |
| `src/personal_organizer/worker/` | Procrastinate worker and task registry. |
| `src/personal_organizer/db/` | Async engines per role, tenant-scoped sessions, bootstrap, `po-db`. |
| `src/personal_organizer/observability/` | structlog + PII redaction, Sentry, Langfuse. |
| `src/personal_organizer/interfaces/` | `LLMProvider`, `CalendarProvider`, `MemoryStore`, `Channel`, `TasksProvider`, `ExportService`. |
| `alembic/` | Migrations, `bootstrap.sql`, vendored Procrastinate schema. |
| `docs/adr/` | Decisions that are not obvious from the code. |
| `scripts/promote` | Fast-forwards `production` onto a green commit of `main`. |

## Getting started

Prerequisites: `uv`, and a container runtime for the local database. On NixOS, apply
`docs/nixos-docker.patch` to `~/.dotfiles` and rebuild — see the header of that file.

```sh
cp .env.example .env          # defaults point at the compose database
docker compose up -d          # PostgreSQL 16 + pgvector on localhost:5433
uv sync --all-groups
uv run pre-commit install

uv run po-db bootstrap        # extension, roles, ownership, default privileges
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

### The local database holds nothing real

Staging and production data live only on Railway. The local container exists so the test
suite can run offline; the database tests truncate between cases, so they must never point
at a deployed database. CI uses its own throwaway service container for the same reason.

Without a database, `uv run pytest` still runs — the database tests skip.

## Three things worth knowing before changing this code

**Database roles.** There are three, not two: a superuser for bootstrap only, `app_owner`
which owns everything and runs Alembic, and `app_user` which the api and worker connect as.
`app_owner` is deliberately *not* a superuser, because superusers bypass RLS and would make
Iteration 03's `FORCE ROW LEVEL SECURITY` inert. See `src/personal_organizer/db/roles.py`.

**Logging is an allowlist.** Any key not in `SAFE_KEYS` is replaced with a shape summary, so
`log.info("x", payload=msg)` cannot leak. Adding a field to `SAFE_KEYS` to "fix" a redacted
log line is the wrong fix. See `src/personal_organizer/observability/redaction.py` and the
acceptance test `tests/api/test_no_pii_in_logs.py`.

**Task arguments carry identifiers, never content.** Procrastinate logs the full repr of
every task kwarg into its own log messages. See `docs/adr/0001`.

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

Setting an environment up the first time — every variable, and the three ways it bites if you
miss one — is `docs/runbook-iteration-01.md`. The short version: a deployed service **refuses to
boot** without `SENTRY__DSN`, a real `LOGGING__PII_PEPPER`, `DATABASE__OWNER_URL`, and (in
staging only) `APP__INTERNAL_TOKEN`, so the Sentry project has to exist before the first deploy.
