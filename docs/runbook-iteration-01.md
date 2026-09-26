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
| `api` | `railway.api.json` | `main` (staging) / `production` (production) |
| `worker` | `railway.worker.json` | same |
| Postgres | — | pin the image to `postgres-ssl:16` |

Pin the Postgres tag explicitly. Default provisioning may hand over a newer major, and the
vendored Procrastinate schema and pgvector floor are checked against 16.

**Set each service's config-as-code path in its settings — this is mandatory, not tidiness.**
Railway auto-detects only `railway.json` / `railway.toml` at the repo root. A custom filename
must be selected per service, in the dashboard: Settings → Config-as-code path
(`railway.api.json` for the api, `railway.worker.json` for the worker). There is no environment
variable for it and the CLI does not expose it, so it is a manual step per service per
environment.

Until it is set, **both files are inert** and the failure is indirect rather than obvious:

- No `preDeployCommand`, so `po-db bootstrap` and `alembic upgrade head` never run. `app_owner`
  and `app_user` are never created, and the api dies in its lifespan on
  `password authentication failed for user "app_user"` — which is what PostgreSQL says for a role
  that *does not exist*, since it deliberately does not distinguish the two. It reads like a
  wrong password and is not one.
- No `startCommand`, so the worker runs the Dockerfile's `CMD` — uvicorn. You get two api
  containers and an empty queue, with nothing in the logs to say so.
- No `healthcheckPath`, so Railway reports a deploy as SUCCESS on process start. A crash-looping
  service can therefore show green; check the deploy logs, not the badge.

A single root `railway.json` shared by both services does not solve this: `startCommand` and
`preDeployCommand` could dispatch on `APP__COMPONENT`, but `healthcheckPath` is static and would
be applied to the worker too, which serves no HTTP and would fail it. Per-service paths are the
only correct shape.

Then:

- Enable **Wait for CI** on the staging services. This is what reconciles "deploys on merge"
  with "CI gates the merge", and it does it without putting a deploy token in GitHub.
- Create the `production` branch — it does not exist yet — and point production's services at
  it. Promotion is a fast-forward from `main`, never a separate build.

The worker deliberately has **no** `preDeployCommand`: two services racing
`alembic upgrade head` is a real failure mode. The trade-off is that the worker can briefly
start against the old schema; it is the same commit and `restartPolicyType: ALWAYS` recovers.
If that ever bites, the fix is a dedicated migrate service, not a second pre-deploy hook.

---

## 2. Variables

Set these on **both** `api` and `worker`, per environment.

### Role passwords live inside the DSNs

There are no `APP_OWNER_PASSWORD` / `APP_USER_PASSWORD` variables. `po-db bootstrap` parses the
role name *and* password out of each DSN (`db/bootstrap.py`, `_credentials`) and applies them
through transaction-local GUCs so the password never appears in a statement string
(`alembic/bootstrap.sql`). So you embed the generated passwords in the owner and app URLs, and
bootstrap makes the cluster match.

Use `RAILWAY_PRIVATE_DOMAIN` — the internal network, which costs no egress. `sslmode=disable`
is correct there, and `db/dsn.py` handles the scheme and driver rewriting either way.

| Variable | Value |
|---|---|
| `APP__ENV` | `staging` / `production` |
| `APP__COMPONENT` | `api` on the api service, `worker` on the worker |
| `APP__RELEASE` | `${{RAILWAY_GIT_COMMIT_SHA}}` — **see the trap below** |
| `APP__DEBUG` | `false` |
| `APP__INTERNAL_TOKEN` | generated secret — **required in staging** |
| `DATABASE__BOOTSTRAP_URL` | the Postgres service's superuser DSN over the private domain |
| `DATABASE__OWNER_URL` | `postgresql://app_owner:<generated>@${{Postgres.RAILWAY_PRIVATE_DOMAIN}}:5432/railway` |
| `DATABASE__APP_URL` | `postgresql://app_user:<generated>@${{Postgres.RAILWAY_PRIVATE_DOMAIN}}:5432/railway` |
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
   `railway.api.json` runs `po-db bootstrap && alembic upgrade head` before every deploy, and
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

`/ready` is **not** the platform healthcheck, and that is on purpose. `railway.api.json` points
the healthcheck at `/health`, which does no I/O. Gating deploys on `/ready` would let a
five-second Postgres blip during a routine restart roll back a good deploy, or kill a running
one. Monitoring should see that blip; the deployment pipeline should not react to it.
