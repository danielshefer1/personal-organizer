"""Signed, timestamped tokens for the connect link and the OAuth ``state`` (D4, D5).

``itsdangerous.URLSafeTimedSerializer`` signs ``{"t": tenant_id, "n": nonce}`` with HMAC under
``ONBOARDING__LINK_SECRET``. The **salt separates the two uses**: a link token is never
accepted as callback state, or the other way round, even though one secret signs both.

The signature proves that we issued the token and when. It does not make the token
single-use. That is the ``onboarding_links`` row keyed on ``nonce`` (``consume_link``). The
row's ``expires_at`` is the authority on expiry. ``max_age_s`` here is a second, cheaper
bound, checked before the database is touched. The connect flow also verifies with a much
longer one, to recognise an expired token as ours and pick the page an active tenant sees
(``onboarding.connect.RECOGNISED_MAX_AGE_S``); such a token is never used for anything else.

The payload is signed, not encrypted: anyone holding the link can read the tenant id. It is
a random UUID that identifies nobody outside our database, and the link is a bearer
credential anyway.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final
from uuid import UUID

from itsdangerous import BadData, URLSafeTimedSerializer

LINK_SALT: Final = "po.onboarding.link"
STATE_SALT: Final = "po.onboarding.state"
MAX_TOKEN_LENGTH: Final = 1024  # real tokens are ~150 characters


@dataclass(frozen=True, slots=True)
class TokenPayload:
    tenant_id: UUID
    nonce: str


def _serializer(secret: str, salt: str) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(secret, salt=salt)


def sign(payload: TokenPayload, *, secret: str, salt: str) -> str:
    return _serializer(secret, salt).dumps({"t": str(payload.tenant_id), "n": payload.nonce})


def verify(token: str, *, secret: str, salt: str, max_age_s: int) -> TokenPayload | None:
    """The payload of a token we signed under ``salt`` within ``max_age_s``, else ``None``."""
    if not isinstance(token, str) or len(token) > MAX_TOKEN_LENGTH or not token.isascii():
        return None  # never decode or HMAC what cannot be ours; never raise
    try:
        data = _serializer(secret, salt).loads(token, max_age=max_age_s)
    except BadData, ValueError:  # bad signature, expired, undecodable: all the same to the caller
        return None
    if not isinstance(data, dict):
        return None
    tenant, nonce = data.get("t"), data.get("n")
    if not isinstance(tenant, str) or not isinstance(nonce, str) or not nonce:
        return None
    try:
        tenant_id = UUID(tenant)
    except ValueError:
        return None
    return TokenPayload(tenant_id=tenant_id, nonce=nonce)


__all__ = ["LINK_SALT", "MAX_TOKEN_LENGTH", "STATE_SALT", "TokenPayload", "sign", "verify"]
