from __future__ import annotations

import ssl

import pytest

from personal_organizer.db.dsn import Target, describe, normalise

RAILWAY = "postgres://app_user:pw@shinkansen.proxy.rlwy.net:5432/railway?sslmode=require"
PRIVATE = "postgresql://app_user:pw@postgres.railway.internal:5432/railway?sslmode=disable"


class TestSchemeAndDriver:
    def test_railway_postgres_scheme_is_rewritten(self) -> None:
        url, _ = normalise(RAILWAY, "asyncpg")
        assert url.startswith("postgresql+asyncpg://")

    def test_psycopg_driver(self) -> None:
        url, _ = normalise(RAILWAY, "psycopg")
        assert url.startswith("postgresql+psycopg://")

    def test_libpq_has_no_driver_suffix(self) -> None:
        url, args = normalise(RAILWAY, "libpq")
        assert url.startswith("postgresql://")
        assert args == {}

    def test_existing_driver_suffix_is_replaced(self) -> None:
        url, _ = normalise("postgresql+psycopg://u:p@h/db", "asyncpg")
        assert url.startswith("postgresql+asyncpg://")

    def test_non_postgres_dsn_rejected(self) -> None:
        with pytest.raises(ValueError, match="Not a PostgreSQL DSN"):
            normalise("mysql://u:p@h/db", "asyncpg")


class TestLibpqParameterLifting:
    """asyncpg raises on libpq's query parameters, so they must leave the URL."""

    def test_sslmode_is_lifted_out_of_the_url(self) -> None:
        url, args = normalise(RAILWAY, "asyncpg")
        assert "sslmode" not in url
        assert isinstance(args["ssl"], ssl.SSLContext)

    def test_sslmode_require_does_not_verify(self) -> None:
        _, args = normalise(RAILWAY, "asyncpg")
        assert args["ssl"].verify_mode == ssl.CERT_NONE

    def test_sslmode_disable_on_private_network(self) -> None:
        _, args = normalise(PRIVATE, "asyncpg")
        assert args["ssl"] is False

    def test_verify_full_verifies(self) -> None:
        _, args = normalise("postgres://u:p@h/db?sslmode=verify-full", "asyncpg")
        assert args["ssl"].verify_mode == ssl.CERT_REQUIRED
        assert args["ssl"].check_hostname

    def test_connect_timeout_becomes_timeout(self) -> None:
        _, args = normalise("postgres://u:p@h/db?connect_timeout=7", "asyncpg")
        assert args["timeout"] == 7.0

    def test_psycopg_keeps_sslmode_in_the_url(self) -> None:
        url, args = normalise(RAILWAY, "psycopg")
        assert "sslmode=require" in url
        assert args == {}

    def test_unknown_sslmode_rejected(self) -> None:
        with pytest.raises(ValueError, match="Unsupported sslmode"):
            normalise("postgres://u:p@h/db?sslmode=banana", "asyncpg")

    def test_non_libpq_params_are_preserved(self) -> None:
        url, _ = normalise("postgres://u:p@h/db?application_name=x&foo=bar", "asyncpg")
        assert "foo=bar" in url
        assert "application_name" not in url


class TestDescribe:
    """What a connection failure is allowed to say about where it was pointed."""

    def test_reports_host_port_and_database(self) -> None:
        assert describe(PRIVATE) == Target("postgres.railway.internal", 5432, "railway")

    def test_never_carries_the_password(self) -> None:
        assert "pw" not in "".join(str(part) for part in describe(RAILWAY))

    def test_a_missing_port_is_none_rather_than_a_guess(self) -> None:
        assert describe("postgresql://u:p@h/db").port is None

    def test_an_unresolved_platform_reference_is_visible_rather_than_fatal(self) -> None:
        """`${{Postgres.PGHOST}}` resolving to nothing is the failure being reported."""
        assert describe("postgresql://u:p@/railway").host == ""

    def test_a_malformed_port_does_not_raise_over_the_error_being_reported(self) -> None:
        assert describe("postgresql://u:p@h:not-a-port/db").port is None
