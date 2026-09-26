"""Application settings.

One ``Settings`` object, assembled from environment variables with ``__`` as the nesting
delimiter: ``DATABASE__APP_URL``, ``MODELS__CHAT_MODEL``, ``LOGGING__PII_PEPPER``.

The important design choice here is ``_enforce_deployed_invariants``: in staging and
production the settings *tighten*, and a service that is misconfigured in a way that could
leak PII refuses to boot. Railway's healthcheck then fails the deploy and rolls back, which
is far better than a service that comes up happily and writes phone numbers to stdout.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import BaseModel, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from personal_organizer.core.errors import MissingDatabaseRoleError
from personal_organizer.db.roles import DatabaseRole

Environment = Literal["local", "ci", "staging", "production"]
Component = Literal["api", "worker", "cli"]

#: Placeholder pepper; rejected by the validator outside local/ci.
_DEV_PEPPER = "local-dev-pepper-not-secret"


class AppSettings(BaseModel):
    env: Environment = "local"
    component: Component = "api"
    #: Set from ``RAILWAY_GIT_COMMIT_SHA``. Nothing reads that variable automatically, so the
    #: Railway services map it explicitly -- see ``docs/runbook-iteration-01.md``. Left unset it
    #: reports "dev", which makes every Sentry release indistinguishable.
    release: str = "dev"
    debug: bool = False
    #: Shared secret for the ``/internal`` router. That router is mounted whenever ``env`` is not
    #: ``production``, so on staging it is publicly reachable and enqueues work per call.
    internal_token: SecretStr | None = None


class DatabaseSettings(BaseModel):
    """DSNs, one per privilege tier. See :mod:`personal_organizer.db.roles`."""

    app_url: SecretStr
    owner_url: SecretStr | None = None
    bootstrap_url: SecretStr | None = None

    pool_size: int = 5
    max_overflow: int = 5
    pool_timeout: float = 10.0
    pool_recycle_seconds: int = 1800
    connect_timeout: float = 10.0
    #: How long a starting api or worker keeps retrying its first connection. Covers a
    #: platform whose private network is not routable for the first seconds of a
    #: container's life; it is not a substitute for a DSN that points somewhere real,
    #: which fails on the first attempt regardless. See ``Database.wait_ready``.
    startup_timeout: float = 30.0
    statement_timeout_ms: int = 15_000
    # A leaked open transaction is a pooled connection stuck with a tenant GUC set, so this
    # is a correctness guard for the RLS design, not just hygiene.
    idle_in_transaction_timeout_ms: int = 30_000
    echo: bool = False


class LoggingSettings(BaseModel):
    level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    renderer: Literal["json", "console"] = "json"
    pii_pepper: SecretStr = SecretStr(_DEV_PEPPER)
    #: Escape hatch for local debugging. The validator makes it unreachable when deployed.
    allow_raw_pii: bool = False


class SentrySettings(BaseModel):
    dsn: SecretStr | None = None
    traces_sample_rate: float = 0.1
    profiles_sample_rate: float = 0.0


class LangfuseSettings(BaseModel):
    public_key: SecretStr | None = None
    secret_key: SecretStr | None = None
    #: EU host by default -- the data inventory for this product is squarely personal.
    host: str = "https://cloud.eu.langfuse.com"

    @property
    def enabled(self) -> bool:
        return self.public_key is not None and self.secret_key is not None


class ModelSettings(BaseModel):
    """Model IDs are settings, per the spec, so a deprecation is a config change.

    ``embedding_dimensions`` is the exception that proves the rule: pgvector fixes
    ``vector(N)`` at DDL time, so changing it is a migration plus a re-embed backfill,
    not a config flip.
    """

    chat_model: str
    fallback_model: str
    embedding_model: str
    embedding_dimensions: int = 1536
    transcription_model: str
    request_timeout_s: float = 60.0
    max_retries: int = 3


class WorkerSettings(BaseModel):
    concurrency: int = 8
    queues: list[str] = Field(default_factory=lambda: ["default", "webhooks", "maintenance"])
    shutdown_graceful_timeout_s: int = 30
    pool_min_size: int = 1
    pool_max_size: int = 4


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_nested_delimiter="__",
        extra="ignore",
        frozen=True,
    )

    app: AppSettings = AppSettings()
    database: DatabaseSettings
    logging: LoggingSettings = LoggingSettings()
    sentry: SentrySettings = SentrySettings()
    langfuse: LangfuseSettings = LangfuseSettings()
    models: ModelSettings
    worker: WorkerSettings = WorkerSettings()
    flags: dict[str, bool] = Field(default_factory=dict)

    @property
    def is_deployed(self) -> bool:
        return self.app.env in ("staging", "production")

    def dsn_for(self, role: DatabaseRole) -> str:
        """Return the DSN for ``role``, or raise if it was never configured."""
        value: SecretStr | None = getattr(self.database, f"{role.value}_url", None)
        if value is None:
            raise MissingDatabaseRoleError(role.value)
        return value.get_secret_value()

    @model_validator(mode="after")
    def _enforce_deployed_invariants(self) -> Settings:
        if not self.is_deployed:
            return self
        problems: list[str] = []
        if self.logging.allow_raw_pii:
            problems.append("LOGGING__ALLOW_RAW_PII must be false outside local/ci")
        if self.logging.renderer != "json":
            problems.append("LOGGING__RENDERER must be 'json' when deployed")
        if self.sentry.dsn is None:
            problems.append("SENTRY__DSN is required when deployed")
        if self.app.debug:
            problems.append("APP__DEBUG must be false when deployed")
        if self.logging.pii_pepper.get_secret_value() == _DEV_PEPPER:
            problems.append("LOGGING__PII_PEPPER must be set to a real secret when deployed")
        if self.database.owner_url is None:
            problems.append("DATABASE__OWNER_URL is required when deployed (Alembic)")
        # The /internal router is mounted whenever env != "production", so staging serves it on a
        # public URL. Refusing to boot without a token is what keeps it from being open.
        if self.app.env == "staging" and self.app.internal_token is None:
            problems.append("APP__INTERNAL_TOKEN is required in staging (/internal is mounted)")
        if problems:
            msg = "Invalid settings for a deployed environment: " + "; ".join(problems)
            raise ValueError(msg)
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # values come from the environment


__all__ = [
    "AppSettings",
    "DatabaseSettings",
    "LangfuseSettings",
    "LoggingSettings",
    "ModelSettings",
    "SentrySettings",
    "Settings",
    "WorkerSettings",
    "get_settings",
]
