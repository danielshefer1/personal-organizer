# Dockerfile rather than Railpack: Python 3.14 is new enough that builder interpreter
# support is the likeliest cause of a Day-1 deploy failure, and a pinned uv base image
# makes CI and production identical.

FROM ghcr.io/astral-sh/uv:python3.14-bookworm-slim AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# NO BuildKit cache mounts here, and they cannot be added back. Railway's builder rejects a
# bare `--mount=type=cache`: the id must be `s/<service id>-<target>`, hardcoded, because the
# flag does not expand build args or environment variables -- and the validator requires the
# prefix to match the id of the service *currently deploying*.
#
# `api` and `worker` both build from this one file (railway.api.json, railway.worker.json), so
# any single hardcoded id satisfies exactly one of them and fails the other identically. Per
# Railway support, a Dockerfile shared by several services cannot use cache mounts at all.
#
# The cost is small. The layer below is still Docker-layer-cached on pyproject.toml + uv.lock,
# so dependencies are only re-downloaded when the lockfile actually changes -- which is the
# one case a cache mount would have helped. Splitting this into two near-identical Dockerfiles
# to win that back would be a worse trade than the download.

# Dependencies first, so a source-only change does not re-resolve the whole lockfile.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev

COPY . .
RUN uv sync --frozen --no-dev


FROM python:3.14-slim-bookworm AS runtime

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Exports are built in memory and nothing is written to disk, so the runtime needs no
# writable volume and can run unprivileged.
RUN useradd --create-home --uid 10001 app
WORKDIR /app

COPY --from=builder --chown=app:app /app /app

USER app

# One image, two roles, dispatched on APP__COMPONENT -- the setting that already exists to say
# which component a process is. This lives here rather than in a platform start command because
# Railway's Config-as-Code is deprecated (existing files work until 2026-12-01, and a service
# that never used it cannot opt in after 2026-08-28), and its replacement, Infrastructure as
# Code, needs an npm SDK this Python repo has no reason to carry. The remaining choice was
# "start command in the image" or "start command typed into a dashboard"; only one of those is
# reviewable and survives recreating a service.
#
# $PORT because Railway assigns it; the fallback keeps `docker run -p 8000:8000` working locally.
# `exec` so the process replaces the shell and receives SIGTERM -- without it the shell swallows
# the signal and both services lose their graceful drain (15s api, 40s worker).
CMD ["sh", "-c", "if [ \"$APP__COMPONENT\" = worker ]; then exec po-worker; else exec uvicorn personal_organizer.api.main:app --host 0.0.0.0 --port \"${PORT:-8000}\" --no-access-log; fi"]
