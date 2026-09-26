# Runbook — Iteration 01: first deploy to Railway

Iteration 01 is done when **a push to `main` deploys to staging with no manual steps, and the
logs contain no raw PII**. The repository side of that is finished and verified locally. What
remains needs credentials, so it is yours to run: Railway, Sentry, and GitHub branch protection.

Work through this once per environment. Do `staging` first and completely; `production` is the
same list with different secret values and a different watched branch.

---

## 0. Before you touch Railway

Two accounts have to exist first, because **a staging service will not boot without them**.
`Settings._enforce_deployed_invariants` raises when `APP__ENV` is `staging` or `production` and
any of these is missing, and `api/lifespan.py` checks the database eagerly on top of that. A
service that cannot construct its settings fails the healthcheck, and Railway rolls the deploy
back — which looks like a platform problem and is not one.

1. **Sentry project.** `SENTRY__DSN` is required, not optional. Create the project before the
   first deploy and keep the DSN to hand.
2. **Langfuse project on the EU host** (`https://cloud.eu.langfuse.com` — the default in
   `settings.py`, chosen for data residency). Optional for booting; do it now anyway.

Generate every secret with:

```sh
uv run python -c "import secrets; print(secrets.token_hex(32))"
```

Four per environment: the `app_owner` password, the `app_user` password, `LOGGING__PII_PEPPER`
and `APP__INTERNAL_TOKEN`. **Different values in staging and production** — a shared pepper
means a hash from one environment correlates with the other, which defeats the point of having
a pepper at all.

---

## 1. Railway project and services

One project. Region **`europe-west4-drams3a`** (EU West, Amsterdam) on every service *and* on
the Postgres volume — the volume region is set separately and is easy to miss.

Environments `staging` and `production`. In each:

| Service | Config as code | Watches |
|---|---|---|
| `api` | dashboard: pre-deploy + healthcheck (below) | `main` (staging) / `production` (production) |
| `worker` | none — the image's CMD dispatches | same |
| Postgres | — | pin the image to `postgres-ssl:16` |

Pin the Postgres tag explicitly. Default provisioning may hand over a newer major, and the
vendored Procrastinate schema and pgvector floor are checked against 16.

### Config as Code is dead — do not plan around it

The original plan called for `railway.api.json` and `railway.worker.json`, one per service. Both
files have been **deleted**, because they cannot work. Railway says:

> Config as Code is deprecated. Prefer Infrastructure as Code. Existing config files keep working
> until 2026-12-01. Starting 2026-08-28, services that have never used Config as Code cannot opt
> in.

Every service here was created after that cutoff and never used the mechanism, so it cannot be
enabled for them. Setting the path in the dashboard is accepted by the form and then **silently
ignored** — no error, and the only symptom is that none of the config applies.

The replacement, Infrastructure as Code (`.railway/railway.ts` + `railway config plan|apply`),
needs the Railway TypeScript SDK from npm — a Node toolchain and a `node_modules` in a uv
project. Worth revisiting if this grows; not worth it for two services.

So deployment config now comes from two places.

**In the image — the start command.** The `CMD` dispatches on `APP__COMPONENT`, which already
exists to say which component a process is. `exec`, so the process is PID 1 and receives SIGTERM
rather than the shell swallowing it; and `${PORT:-8000}`, so Railway's assigned port is honoured
without breaking a local `docker run -p 8000:8000`. The `docker` CI job asserts both roles,
because a mistake here silently turns the worker into a second api: both start, both look
healthy, and the queue is never drained.

**In the dashboard — the two things an image cannot express.** On the **api** service only:

| Setting | Value |
|---|---|
| Pre-deploy command | `po-db bootstrap && alembic upgrade head` |
| Healthcheck path | `/health` |

Per service, per environment. The worker gets neither: no pre-deploy, because two services racing
`alembic upgrade head` is a real failure mode; no healthcheck, because it serves no HTTP and one
would fail it.

Both failures are indirect, so know the symptoms:

- No `preDeployCommand`, so `po-db bootstrap` and `alembic upgrade head` never run. `app_owner`
  and `app_user` are never created, and the api dies in its lifespan on
  `password authentication failed for user "app_user"` — which is what PostgreSQL says for a role
  that *does not exist*, since it deliberately does not distinguish the two. It reads like a
  wrong password and is not one.
- No healthcheck path, so Railway reports a deploy as SUCCESS on process start. A crash-looping
  service therefore shows green — this happened twice while setting staging up. Read the deploy
  logs, not the badge.

Then:

- Enable **Wait for CI** on the staging services. This is what reconciles "deploys on merge"
  with "CI gates the merge", and it does it without putting a deploy token in GitHub.
- Create the `production` branch — it does not exist yet — and point production's services at
  it. Promotion is a fast-forward from `main`, never a separate build.

The trade-off of keeping migrations off the worker is that it can briefly start against the old
schema. Same commit, and a restart recovers. If that ever bites, the fix is a dedicated migrate
service, not a second pre-deploy hook.

---

## 2. Variables

Set these on **both** `api` and `worker`, per environment.

### Role passwords live inside the DSNs

There are no `APP_OWNER_PASSWORD` / `APP_USER_PASSWORD` variables. `po-db bootstrap` parses the
role name *and* password out of each DSN (`db/bootstrap.py`, `_credentials`) and applies them
through transaction-local GUCs so the password never appears in a statement string
(`alembic/bootstrap.sql`). So you embed the generated passwords in the owner and app URLs, and
bootstrap makes the cluster match.

Assemble the DSNs from the Postgres service's own variables rather than its `DATABASE_URL`:
the owner and app URLs need `app_owner`/`app_user` credentials, not the superuser's. `PGHOST`
is the internal private-network host, so this costs no egress, and `db/dsn.py` handles the
scheme and driver rewriting. Verified resolving to `postgres.railway.internal:5432/railway`.

| Variable | Value |
|---|---|
| `APP__ENV` | `staging` / `production` |
| `APP__COMPONENT` | `api` on the api service, `worker` on the worker |
| `APP__RELEASE` | **unresolved** — the obvious reference yields `""`; see the trap below |
| `APP__DEBUG` | `false` |
| `APP__INTERNAL_TOKEN` | generated secret — **required in staging** |
| `DATABASE__BOOTSTRAP_URL` | `postgresql://${{Postgres.PGUSER}}:${{Postgres.PGPASSWORD}}@${{Postgres.PGHOST}}:${{Postgres.PGPORT}}/${{Postgres.PGDATABASE}}` |
| `DATABASE__OWNER_URL` | `postgresql://app_owner:<generated>@${{Postgres.PGHOST}}:${{Postgres.PGPORT}}/${{Postgres.PGDATABASE}}` |
| `DATABASE__APP_URL` | `postgresql://app_user:<generated>@${{Postgres.PGHOST}}:${{Postgres.PGPORT}}/${{Postgres.PGDATABASE}}` |
| `LOGGING__RENDERER` | `json` — anything else is rejected when deployed |
| `LOGGING__PII_PEPPER` | generated secret, different per environment |
| `SENTRY__DSN` | from step 0 — **required** |
| `MODELS__CHAT_MODEL` | `gpt-5-mini` |
| `MODELS__FALLBACK_MODEL` | `claude-haiku-4-5` |
| `MODELS__EMBEDDING_MODEL` | `text-embedding-3-small` |
| `MODELS__TRANSCRIPTION_MODEL` | `gpt-4o-mini-transcribe` |
| `LANGFUSE__PUBLIC_KEY`, `LANGFUSE__SECRET_KEY` | optional |
| `FLAGS__GROUPS`, `FLAGS__TASKS` | `false` |

Do **not** set `LOGGING__ALLOW_RAW_PII`. It exists for local debugging and the validator makes
it unreachable when deployed.

### Three traps worth reading twice

1. **`APP__RELEASE` is not wired automatically, and the obvious fix does not work either.**
   `settings.py` documents it as coming from `RAILWAY_GIT_COMMIT_SHA`, but nothing in the code
   reads that variable and neither `railway.*.json` maps it. Setting
   `APP__RELEASE=${{RAILWAY_GIT_COMMIT_SHA}}` was tried and **resolves to an empty string** — and
   empty is worse than absent, because `""` overrides the `"dev"` default rather than falling back
   to it. Check `/health` after any change here; if `release` is blank or `dev`, Sentry cannot tell
   you which commit an error came from. Unresolved; treat it as open.
2. **`APP__INTERNAL_TOKEN` is required in staging, not in production.** `/internal/ping` is
   mounted whenever `APP__ENV != "production"`, so staging serves it on a public URL where it
   enqueues a job and hits the database per call. It is gated on an `X-Internal-Token` header,
   and a staging service with no token set refuses to boot. Production does not mount the router
   at all, so it needs no token.
3. **`DATABASE__BOOTSTRAP_URL` is only needed by the pre-deploy command**, but it *is* needed:
   the api's pre-deploy command runs `po-db bootstrap && alembic upgrade head` before every
   deploy, and
   bootstrap is the only tier that may `CREATE EXTENSION` and `CREATE ROLE`.
4. **`railway add -d postgres` provisions the wrong thing.** It ignores both the project region
   and any version pin, giving the latest major in Railway's default US region — observed as
   `postgres-ssl:18` in `sfo`, with the 500 MB volume there too. Both matter: PG18 is a different
   major from the PG16 that `docker-compose.yml` and CI run, and a US volume puts EU personal data
   in the wrong jurisdiction. Fix the image and *both* regions (service and volume) in the
   dashboard before bootstrapping, or create the database there in the first place. `railway
   service scale` moves a stateless service between regions but **adds** a replica rather than
   moving it — pass the old region explicitly at zero, e.g.
   `railway service scale --service worker europe-west4-drams3a=1 sfo=0`.

### What the CLI can and cannot do

Useful when scripting this. Variables work non-interactively; deploy settings do not.

| Task | CLI |
|---|---|
| Set variables | `railway variables --service S --environment E --skip-deploys --set 'K=V'` — works |
| Create a service from the repo | `railway add -s NAME -r owner/repo --branch main` — works, prompts but honours the flags |
| Region of a stateless service | `railway service scale` — works, see the caveat above |
| Duplicate an environment | `railway environment new staging --duplicate production` — works |
| Start command, healthcheck, pre-deploy | `railway environment edit --service-config` — **silently no-ops** non-interactively |
| Config-as-code path | not exposed at all — dashboard only |
| Postgres image / volume region | not exposed at all — dashboard only |

Redirect stdin from `/dev/null`; several subcommands open a prompt and then proceed with the
flags they were given.

---

## 3. GitHub

```sh
gh auth login
```

Then branch protection on `main`: require the `lint`, `types` and `test` checks, require a pull
request, disallow force-pushes.

**Do this after the first CI run has gone green**, not before. A required check that has never
passed blocks the very merge that would make it pass.

---

## 4. Verify — this is the Done-When, not a formality

Merge to `main`, then **touch nothing**. CI runs, goes green, and Railway deploys on its own.
Any manual step you have to take here means the iteration is not done.

```sh
railway login
railway link                                              # pick the project

# The runtime role is still correctly constrained on the real database
railway run --environment staging uv run po-db check

curl https://<staging-api>/health
# {"status":"ok","version":"0.1.0","env":"staging","release":"<the commit sha, not "dev">"}

curl -i https://<staging-api>/ready
# 200, {"status":"ok","checks":{"database":"ok"}}

# The walking skeleton, end to end, on the deployed stack
curl -X POST -H "X-Internal-Token: <staging token>" https://<staging-api>/internal/ping
# {"deferred":true,"job_id":N}
railway logs --service worker      # must show ping.ok with that job_id

# And the gate is really a gate
curl -o /dev/null -w '%{http_code}\n' -X POST https://<staging-api>/internal/ping   # 404
```

Then the second half of the criterion. Read the staging logs and confirm there is no raw phone
number, email address or message body in them. `tests/api/test_no_pii_in_logs.py` mechanises
this against the real processor chain, and `tests/unit/test_sentry_scrubbing.py` does the same
for the Sentry path, but the plan asks for the deployed check and it is worth doing once by eye.

### If the first deploy fails

Check in this order, because these are the likely causes and the error surfaces identically —
as a failed healthcheck and a rollback:

1. A missing `SENTRY__DSN`, a placeholder `LOGGING__PII_PEPPER`, a missing `DATABASE__OWNER_URL`
   or a missing `APP__INTERNAL_TOKEN`. The deploy log has the exact list: settings collects every
   problem and raises once, so you get all of them in one message rather than one per retry.
2. `po-db bootstrap` failing in pre-deploy — almost always `DATABASE__BOOTSTRAP_URL` pointing at
   a non-superuser, or pgvector below 0.5 because the image tag was not pinned.
3. The eager `database.check()` in `api/lifespan.py` — a DSN that resolves but whose role does not
   exist yet, which means bootstrap did not actually run.

---

## What is deliberately not in this iteration

`/ready` is **not** the platform healthcheck, and that is on purpose. The api's healthcheck path
is `/health`, which does no I/O. Gating deploys on `/ready` would let a
five-second Postgres blip during a routine restart roll back a good deploy, or kill a running
one. Monitoring should see that blip; the deployment pipeline should not react to it.
