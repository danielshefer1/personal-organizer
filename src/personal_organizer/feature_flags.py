"""Feature flags.

Gates 10 and 15 put groups and Google Tasks behind flags. The env-backed implementation
below is synchronous, but the Protocol is ``async`` from day one: the later per-tenant and
per-group implementations read the database, and retrofitting ``await`` across every call
site is exactly the kind of churn this avoids.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import Protocol, runtime_checkable

from personal_organizer.core.types import TenantId


class Flag(StrEnum):
    GROUPS = "groups"
    TASKS = "tasks"
    EXPORTS = "exports"
    MEMORY = "memory"


@runtime_checkable
class FeatureFlags(Protocol):
    async def is_enabled(self, flag: Flag, *, tenant_id: TenantId | None = None) -> bool: ...


class EnvFeatureFlags:
    """Flags from settings. Unknown flags are off."""

    def __init__(self, values: Mapping[str, bool]) -> None:
        self._values = dict(values)

    async def is_enabled(self, flag: Flag, *, tenant_id: TenantId | None = None) -> bool:
        del tenant_id  # env flags are global; per-tenant overrides arrive with the DB impl
        return self._values.get(flag.value, False)


__all__ = ["EnvFeatureFlags", "FeatureFlags", "Flag"]
