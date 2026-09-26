from __future__ import annotations

import json
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
