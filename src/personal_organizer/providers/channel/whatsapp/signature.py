"""``X-Hub-Signature-256`` verification.

Meta sends ``sha256=<hex>``: an HMAC-SHA256 of the **exact bytes** it posted, keyed with the
app secret. Two things make this easy to get subtly wrong:

- It must be computed over the raw body, never a re-serialised parse -- which is why the
  webhook router uses ``RawBodyRoute`` and nothing above it may rewrite bodies.
- The comparison must be on bytes. ``hmac.compare_digest`` raises ``TypeError`` for a
  non-ASCII ``str``, and Starlette decodes headers as latin-1, so a crafted header would
  turn a clean 401 into a 500 (and a Sentry event per request).
"""

from __future__ import annotations

import hashlib
import hmac
from typing import Final

SIGNATURE_HEADER: Final = "x-hub-signature-256"
_PREFIX: Final = "sha256="
_HEX_DIGITS: Final = frozenset("0123456789abcdef")


def sign(secret: bytes, raw_body: bytes) -> str:
    """The header value Meta would send for ``raw_body``. Used by tests and the simulator."""
    return _PREFIX + hmac.new(secret, raw_body, hashlib.sha256).hexdigest()


def verify_signature(secret: bytes | None, raw_body: bytes, header: str | None) -> bool:
    """True only if ``header`` is a valid signature of ``raw_body`` under ``secret``.

    False -- never an exception -- for a missing secret, a missing or malformed header, and
    a mismatch. A missing secret failing closed is deliberate defence in depth: settings
    already refuse to enable WhatsApp without one.
    """
    if not secret or not header or not header.startswith(_PREFIX):
        return False
    supplied = header.removeprefix(_PREFIX).strip().lower()
    if len(supplied) != hashlib.sha256().digest_size * 2 or not set(supplied) <= _HEX_DIGITS:
        return False
    expected = hmac.new(secret, raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(supplied.encode(), expected.encode())


def rejection_reason(header: str | None) -> str:
    """Why a signature failed, for the log line. Never includes the header value."""
    if not header:
        return "missing"
    if not header.startswith(_PREFIX):
        return "malformed"
    return "mismatch"


__all__ = ["SIGNATURE_HEADER", "rejection_reason", "sign", "verify_signature"]
