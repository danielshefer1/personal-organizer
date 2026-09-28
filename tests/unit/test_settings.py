from __future__ import annotations

import json
import os
from typing import Any

import pytest
from pydantic import ValidationError

from personal_organizer.core.errors import MissingDatabaseRoleError
from personal_organizer.db.roles import DatabaseRole
from personal_organizer.settings import Settings


class TestParsing:
    def test_nested_env_vars(self, settings: Settings) -> None:
        assert settings.models.chat_model == "gpt-5-mini"
        assert settings.database.app_url.get_secret_value().startswith("postgresql://")

    def test_dsn_for_role(self, settings: Settings) -> None:
        assert "app_user" in settings.dsn_for(DatabaseRole.APP)
        assert "app_owner" in settings.dsn_for(DatabaseRole.OWNER)

    def test_missing_role_raises(self, settings_factory: Any, monkeypatch: Any) -> None:
        monkeypatch.delenv("DATABASE__BOOTSTRAP_URL", raising=False)
        cfg = settings_factory()
        object.__setattr__(cfg.database, "bootstrap_url", None)
        with pytest.raises(MissingDatabaseRoleError):
            cfg.dsn_for(DatabaseRole.BOOTSTRAP)


class TestSecretsNeverRender:
    """An exception traceback that renders the settings object is a classic leak."""

    @pytest.mark.parametrize("render", [repr, str, lambda c: c.model_dump_json()])
    def test_secret_values_are_hidden(self, settings: Settings, render: Any) -> None:
        text = render(settings)
        assert "pw@localhost" not in text
        assert "test-pepper" not in text

    def test_json_dump_is_still_valid_json(self, settings: Settings) -> None:
        assert isinstance(json.loads(settings.model_dump_json()), dict)


class TestDeployedInvariants:
    """A deployed service that could leak PII must refuse to boot."""

    @pytest.mark.parametrize(
        "override",
        [
            {"LOGGING__ALLOW_RAW_PII": "true"},
            {"LOGGING__RENDERER": "console"},
            {"APP__DEBUG": "true"},
        ],
    )
    def test_unsafe_settings_are_rejected(
        self, settings_factory: Any, override: dict[str, str]
    ) -> None:
        with pytest.raises(ValidationError):
            settings_factory(
                APP__ENV="production",
                SENTRY__DSN="https://k@o.ingest.sentry.io/1",
                **override,
            )

    def test_placeholder_pepper_is_rejected(self, settings_factory: Any, monkeypatch: Any) -> None:
        monkeypatch.delenv("LOGGING__PII_PEPPER", raising=False)
        with pytest.raises(ValidationError, match="PII_PEPPER"):
            settings_factory(
                APP__ENV="staging",
                SENTRY__DSN="https://k@o.ingest.sentry.io/1",
                LOGGING__PII_PEPPER="local-dev-pepper-not-secret",
            )

    def test_missing_sentry_dsn_is_rejected(self, settings_factory: Any) -> None:
        with pytest.raises(ValidationError, match="SENTRY__DSN"):
            settings_factory(APP__ENV="production")

    def test_staging_without_an_internal_token_is_rejected(self, settings_factory: Any) -> None:
        """The /internal router is mounted whenever env != production, so staging serves it on
        a public URL. Refusing to boot is what keeps it from being open there."""
        with pytest.raises(ValidationError, match="APP__INTERNAL_TOKEN"):
            settings_factory(
                APP__ENV="staging",
                SENTRY__DSN="https://k@o.ingest.sentry.io/1",
                LOGGING__PII_PEPPER="a-real-secret",
            )

    def test_production_needs_no_internal_token(self, settings_factory: Any) -> None:
        """Because the router is not mounted there at all."""
        cfg = settings_factory(
            APP__ENV="production",
            SENTRY__DSN="https://k@o.ingest.sentry.io/1",
            LOGGING__PII_PEPPER="a-real-secret",
        )
        assert cfg.app.internal_token is None

    def test_valid_production_config_boots(self, settings_factory: Any) -> None:
        cfg = settings_factory(
            APP__ENV="production",
            SENTRY__DSN="https://k@o.ingest.sentry.io/1",
            LOGGING__PII_PEPPER="a-real-secret",
        )
        assert cfg.is_deployed

    def test_local_is_permissive(self, settings_factory: Any) -> None:
        cfg = settings_factory(APP__ENV="local", LOGGING__ALLOW_RAW_PII="true")
        assert cfg.logging.allow_raw_pii
        assert not cfg.is_deployed


VERIFY_TOKEN = "v" * 64

_WHATSAPP_CREDENTIALS = {
    "WHATSAPP__APP_SECRET": "app-secret",
    "WHATSAPP__VERIFY_TOKEN": VERIFY_TOKEN,
    "WHATSAPP__ACCESS_TOKEN": "graph-token",
    "WHATSAPP__PHONE_NUMBER_ID": "1234567890",
}

_DEPLOYED = {
    "SENTRY__DSN": "https://k@o.ingest.sentry.io/1",
    "LOGGING__PII_PEPPER": "a-real-secret",
    "APP__INTERNAL_TOKEN": "t",
}


class TestWhatsApp:
    """Off by default and all-or-nothing when on, so that staging can deploy before the Meta
    app exists and a half-configured channel refuses to boot rather than half-working."""

    def test_disabled_by_default_with_nothing_required(self, settings: Settings) -> None:
        assert settings.whatsapp.enabled is False
        assert settings.whatsapp.app_secret is None

    @pytest.mark.parametrize("env", ["staging", "production"])
    def test_a_deployed_service_boots_with_whatsapp_disabled(
        self, settings_factory: Any, env: str
    ) -> None:
        """What lets Iteration 02 merge and deploy before the Meta credentials exist."""
        cfg = settings_factory(APP__ENV=env, **_DEPLOYED)
        assert cfg.is_deployed
        assert not cfg.whatsapp.enabled

    def test_enabled_with_nothing_names_every_missing_credential_at_once(
        self, settings_factory: Any
    ) -> None:
        with pytest.raises(ValidationError) as excinfo:
            settings_factory(WHATSAPP__ENABLED="true")
        message = str(excinfo.value)
        for name in _WHATSAPP_CREDENTIALS:
            assert name in message

    def test_enabled_with_every_credential_boots(self, settings_factory: Any) -> None:
        cfg = settings_factory(WHATSAPP__ENABLED="true", **_WHATSAPP_CREDENTIALS)
        assert cfg.whatsapp.enabled
        assert cfg.whatsapp.phone_number_id == "1234567890"

    def test_enabled_in_staging_boots(self, settings_factory: Any) -> None:
        cfg = settings_factory(
            APP__ENV="staging", WHATSAPP__ENABLED="true", **_DEPLOYED, **_WHATSAPP_CREDENTIALS
        )
        assert cfg.whatsapp.enabled

    def test_a_short_verify_token_is_rejected(self, settings_factory: Any) -> None:
        """It travels in a query string; below 32 characters the token scrubber misses it."""
        with pytest.raises(ValidationError, match="VERIFY_TOKEN"):
            settings_factory(WHATSAPP__VERIFY_TOKEN="short")

    def test_plain_http_graph_url_is_rejected_when_deployed(self, settings_factory: Any) -> None:
        """The bearer token goes to this URL on every reply."""
        with pytest.raises(ValidationError, match="GRAPH_BASE_URL"):
            settings_factory(
                APP__ENV="production",
                WHATSAPP__ENABLED="true",
                WHATSAPP__GRAPH_BASE_URL="http://graph.facebook.com",
                **_DEPLOYED,
                **_WHATSAPP_CREDENTIALS,
            )

    def test_the_allowlist_is_comma_separated_and_normalised(self, settings_factory: Any) -> None:
        cfg = settings_factory(WHATSAPP__ALLOWED_PHONES=" +31 6 1234 5678, 0044 7700 900123 ,")
        assert cfg.whatsapp.allowlist == frozenset({"+31612345678", "+447700900123"})

    def test_an_invalid_allowlist_entry_fails_boot_without_echoing_it(
        self, settings_factory: Any
    ) -> None:
        """Silently dropping a typo'd number would leave its owner locked out with no
        explanation; echoing it would put a phone number in the deploy log."""
        with pytest.raises(ValidationError, match="ALLOWED_PHONES") as excinfo:
            settings_factory(WHATSAPP__ALLOWED_PHONES="+31612345678,0612345678")
        assert "0612345678" not in str(excinfo.value)
        assert "31612345678" not in str(excinfo.value)

    def test_a_boot_failure_never_repeats_a_secret(self, settings_factory: Any) -> None:
        """Pydantic echoes the input in its error by default, and boot failures are printed
        straight into the platform's deploy log."""
        with pytest.raises(ValidationError) as excinfo:
            settings_factory(APP__ENV="production", LOGGING__PII_PEPPER="a-real-secret")
        assert "a-real-secret" not in str(excinfo.value)
        assert "pw@localhost" not in str(excinfo.value)

    def test_an_empty_allowlist_is_empty(self, settings: Settings) -> None:
        assert settings.whatsapp.allowlist == frozenset()

    def test_credentials_never_render(self, settings_factory: Any) -> None:
        cfg = settings_factory(WHATSAPP__ENABLED="true", **_WHATSAPP_CREDENTIALS)
        rendered = repr(cfg) + cfg.model_dump_json()
        for secret in ("app-secret", VERIFY_TOKEN, "graph-token"):
            assert secret not in rendered


class TestThePlatformGuard:
    """`APP__ENV` is what every other check is gated on, so it cannot be optional.

    Unset, it defaults to "local", and the failure is silent in the worst direction: no
    check fires, the placeholder pepper is accepted, and `/internal` mounts itself on a
    public URL with its token check disabled. So a container the platform is running is not
    allowed to claim it is a laptop.
    """

    def test_a_platform_container_without_app_env_refuses_to_boot(
        self, settings_factory: Any, monkeypatch: Any
    ) -> None:
        monkeypatch.delenv("APP__ENV", raising=False)
        monkeypatch.setenv("RAILWAY_ENVIRONMENT_NAME", "production")
        with pytest.raises(ValidationError, match="APP__ENV"):
            settings_factory(APP__ENV="local")

    def test_any_platform_variable_is_enough(self, settings_factory: Any, monkeypatch: Any) -> None:
        """Detected by prefix: the exact set the platform injects is not ours to depend on."""
        monkeypatch.setenv("RAILWAY_SERVICE_NAME", "api")
        with pytest.raises(ValidationError, match="APP__ENV"):
            settings_factory(APP__ENV="local")

    def test_ci_on_the_platform_is_rejected_too(
        self, settings_factory: Any, monkeypatch: Any
    ) -> None:
        """`ci` is not deployed either, and it is the other value that disables the checks."""
        monkeypatch.setenv("RAILWAY_PROJECT_ID", "p-1")
        with pytest.raises(ValidationError, match="APP__ENV"):
            settings_factory(APP__ENV="ci")

    def test_a_declared_environment_on_the_platform_is_fine(
        self, settings_factory: Any, monkeypatch: Any
    ) -> None:
        monkeypatch.setenv("RAILWAY_ENVIRONMENT_NAME", "production")
        cfg = settings_factory(
            APP__ENV="production",
            SENTRY__DSN="https://k@o.ingest.sentry.io/1",
            LOGGING__PII_PEPPER="a-real-secret",
        )
        assert cfg.is_deployed

    def test_off_the_platform_local_stays_permissive(
        self, settings_factory: Any, monkeypatch: Any
    ) -> None:
        """The guard must not reach a laptop or a CI runner, which have no such variables."""
        for name in [key for key in os.environ if key.startswith("RAILWAY_")]:
            monkeypatch.delenv(name, raising=False)
        cfg = settings_factory(APP__ENV="local")
        assert not cfg.is_deployed
