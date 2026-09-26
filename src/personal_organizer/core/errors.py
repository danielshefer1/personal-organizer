"""Application error hierarchy.

Exception *messages* are never surfaced to users or to logs: database drivers embed the
offending values in them (`Key (phone)=(+3161...) already exists`), which is exactly the PII
the redaction layer exists to keep out. Handlers log `error_type` and `code`, never `str(exc)`.
"""


class AppError(Exception):
    """Base class for errors this application raises deliberately."""

    code: str = "app_error"


class ConfigError(AppError):
    """Settings are missing or internally inconsistent."""

    code = "config_error"


class MissingDatabaseRoleError(ConfigError):
    """No DSN was configured for the requested database role."""

    code = "missing_database_role"

    def __init__(self, role: str) -> None:
        super().__init__(f"No DSN configured for database role {role!r}")
        self.role = role


class BootstrapError(AppError):
    """The database bootstrap step failed a precondition."""

    code = "bootstrap_error"


__all__ = ["AppError", "BootstrapError", "ConfigError", "MissingDatabaseRoleError"]
