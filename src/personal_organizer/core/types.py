"""Domain newtypes.

`TenantId` is assumed to be a UUID. The spec does not state the type; if it turns out to be
a bigint or a natural key, this module and the RLS policy casts are the only places to change.
"""

from typing import NewType
from uuid import UUID

TenantId = NewType("TenantId", UUID)
RequestId = NewType("RequestId", str)

__all__ = ["RequestId", "TenantId"]
