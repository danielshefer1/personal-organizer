"""PII redaction for logs and for Sentry.

**Allowlist, not denylist.** A denylist over free-form ``**kwargs`` dies the first time
someone writes ``log.info("received", payload=msg)``. So any key not in :data:`SAFE_KEYS`
has its value replaced by a shape summary. The friction is the point: it is what makes
"logs contain no raw PII" an assertion rather than an aspiration.

Four layers, in order:

1. **Secret key names** (:data:`SECRET_KEYS`) -- non-overridable, wins over everything.
   This is "never store passwords, one-time codes or payment details", mechanised.
2. **Correlatable identifiers** (:data:`HASH_KEYS`) -- HMAC'd with a pepper and re-emitted
   under a ``*_hash`` key, so you keep "same user across two lines" without keeping the
   identifier.
3. **Allowlist** -- everything else is replaced.
4. **Value scrubbing** -- applied *even to allowlisted values*, because ``event``, ``path``
   and exception strings are allowlisted and routinely carry PII. The one exception is
   :data:`OPAQUE_ID_KEYS`: correlation ids are long alphanumeric strings, so the token rule
   would otherwise replace every one of them with a marker and leave nothing to correlate.
   Those keys skip text scrubbing only when the value matches an identifier shape exactly.

The same :func:`scrub_text` backs the Sentry ``before_send`` hook. One scrubber, two
consumers; wiring Sentry through a separate implementation is how the two drift apart.
"""

from __future__ import annotations

import hmac
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from hashlib import sha256
from typing import Any, Final

MAX_STR: Final = 512
MAX_DEPTH: Final = 4
MAX_ITEMS: Final = 50

REDACTED: Final = "<redacted>"
REDACTED_SECRET: Final = "<redacted:secret>"  # noqa: S105 - a marker, not a credential

#: Never logged under any circumstances, at any nesting depth.
SECRET_KEYS: Final = frozenset(
    {
        "password",
        "passwd",
        "secret",
        "token",
        "api_key",
        "apikey",
        "authorization",
        "auth",
        "otp",
        "pin",
        "cvv",
        "card",
        "pan",
        "iban",
        "cookie",
        "credential",
        "private_key",
        "dsn",
        "access_token",
        "refresh_token",
        "client_secret",
    }
)

#: Identifiers that are useful for correlation but must not be stored in the clear.
HASH_KEYS: Final = frozenset(
    {
        "wa_id",
        "bsuid",
        "phone",
        "msisdn",
        "sender",
        "recipient",
        "email",
        "backup_email",
        "attendee_email",
        "from_",
        "to",
    }
)

#: Free-text fields that carry user content. Never logged, never hashed.
CONTENT_KEYS: Final = frozenset(
    {
        "message",
        "body",
        "text",
        "content",
        "transcript",
        "title",
        "description",
        "summary",
        "note",
        "memory",
        "prompt",
        "completion",
        "payload",
    }
)

#: Everything permitted through. Anything absent here is replaced with a shape summary.
SAFE_KEYS: Final = frozenset(
    {
        # structlog / stdlib
        "event",
        "level",
        "logger",
        "timestamp",
        "exc_info",
        "exception",
        "stack",
        # service identity
        "service",
        "env",
        "component",
        "release",
        "hostname",
        # correlation
        "request_id",
        "trace_id",
        "span_id",
        # Sentry's id for an event, so a log line can point at the Sentry issue and back.
        "event_id",
        "tenant_id",
        "job_id",
        "session_id_hash",
        # http
        "method",
        "path",
        "route",
        "status_code",
        "duration_ms",
        "client_ip_hash",
        # jobs
        "task_name",
        "queue",
        "attempt",
        "priority",
        "lock",
        # db
        "db_role",
        "table",
        "rows",
        # llm
        "model",
        "provider",
        "tokens_in",
        "tokens_out",
        "latency_ms",
        "cost_usd",
        "finish_reason",
        "tool_name",
        # errors -- type and code only, never the message
        "error_type",
        "error_code",
        "error_count",
    }
)

#: Allowlisted keys holding system-generated opaque identifiers -- UUIDs, hex digests,
#: release SHAs. They are allowlisted *for* correlation, but they are long and alphanumeric,
#: so ``_LONG_TOKEN`` (and, for a digit-heavy id, ``_DIGITS_RUN``) would otherwise rewrite
#: every one of them and correlate nothing with nothing. Values here skip text scrubbing
#: only when they genuinely look like an identifier -- see :func:`is_opaque_id` -- because
#: ``request_id`` is taken from an inbound header when one is present, so an allowlisted key
#: is not a trusted value.
OPAQUE_ID_KEYS: Final = frozenset(
    {
        "request_id",
        "trace_id",
        "span_id",
        # Sentry's own id for an event. Preserved for the same reason as the rest: it is the
        # handle that ties a Sentry issue back to the log line that produced it.
        "event_id",
        "tenant_id",
        "job_id",
        "session_id_hash",
        "client_ip_hash",
        "release",
    }
)

#: Keys that must never be allowlisted. Asserted disjoint from SAFE_KEYS in the test suite.
FORBIDDEN_KEYS: Final = SECRET_KEYS | HASH_KEYS | CONTENT_KEYS

_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}")
_EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_IBAN = re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{8,26}\b")
_DIGITS_RUN = re.compile(r"(?<![\w.])[+(]?\d[\d\s().-]{6,}\d\b")
_LONG_TOKEN = re.compile(r"\b[A-Za-z0-9+/_-]{32,}={0,2}\b")
# Opaque-identifier shapes, anchored end to end: a dashed UUID, or a bare hex digest --
# uuid4().hex, a W3C trace or span id, a release commit SHA, a hash_identifier() digest.
_UUID_SHAPE = re.compile(r"\A[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}\Z")
_HEX_SHAPE = re.compile(r"\A[0-9a-fA-F]{16,64}\Z")
# Procrastinate interpolates Job.call_string -- "task_name[id](kwarg=<repr>, ...)" --
# straight into its log messages at INFO and ERROR. Those kwargs carry user content, and
# no content-detecting regex can save us there, so the argument list is dropped wholesale.
# The architectural counterpart is in docs/adr/0001: task kwargs carry ids, never content.
_CALL_STRING = re.compile(r"([\w.:-]+\[\d+\])\([^)]*\)")


def _luhn_ok(digits: str) -> bool:
    total, parity = 0, len(digits) % 2
    for index, char in enumerate(digits):
        value = int(char)
        if index % 2 == parity:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10 == 0


def _sub_digit_run(match: re.Match[str]) -> str:
    raw = match.group(0)
    digits = re.sub(r"\D", "", raw)
    if 13 <= len(digits) <= 19 and _luhn_ok(digits):
        return "<redacted:pan>"
    if len(digits) >= 8:
        return "<redacted:phone>"
    return raw


def scrub_text(value: str) -> str:
    """Remove PII-shaped substrings and cap the length.

    The length cap is load-bearing on its own: it means a transcript or a file body cannot
    land in a log line even if some future key is allowlisted by mistake.
    """
    value = _CALL_STRING.sub(r"\1(<redacted:args>)", value)
    value = _JWT.sub("<redacted:jwt>", value)
    value = _EMAIL.sub("<redacted:email>", value)
    value = _IBAN.sub("<redacted:iban>", value)
    value = _DIGITS_RUN.sub(_sub_digit_run, value)
    value = _LONG_TOKEN.sub("<redacted:token>", value)
    if len(value) > MAX_STR:
        value = value[:MAX_STR] + "...<truncated>"
    return value


@dataclass(frozen=True)
class Unredacted:
    """Wrapper that bypasses redaction. Honoured only when ``logging.allow_raw_pii`` is set,
    which the settings validator forbids in staging and production."""

    value: object


def hash_identifier(value: object, pepper: str) -> str:
    normalised = re.sub(r"\s+", "", str(value)).casefold()
    digest = hmac.new(pepper.encode(), normalised.encode(), sha256).hexdigest()
    return digest[:16]


def _shape(value: object) -> str:
    if isinstance(value, str):
        return f"<redacted:str[{len(value)}]>"
    if isinstance(value, Mapping):
        return f"<redacted:dict[{len(value)}]>"
    if isinstance(value, Sequence) and not isinstance(value, str | bytes):
        return f"<redacted:list[{len(value)}]>"
    return f"<redacted:{type(value).__name__}>"


def _scrub_value(value: object, depth: int = 0) -> object:
    """Recursively scrub a value that has already been allowed through by key.

    The allowlist is *not* re-applied at depth: structures like structlog's
    ``dict_tracebacks`` output have arbitrary internal keys that we want to keep. Secret and
    identifier key names are still honoured at every depth.
    """
    if depth >= MAX_DEPTH:
        return _shape(value)
    if isinstance(value, str):
        return scrub_text(value)
    if isinstance(value, bool | int | float | type(None)):
        return value
    if isinstance(value, Mapping):
        out: dict[str, object] = {}
        for raw_key, item in list(value.items())[:MAX_ITEMS]:
            key = str(raw_key).casefold()
            if key in SECRET_KEYS:
                out[str(raw_key)] = REDACTED_SECRET
            elif key in CONTENT_KEYS:
                out[str(raw_key)] = _shape(item)
            else:
                out[str(raw_key)] = _scrub_value(item, depth + 1)
        return out
    if isinstance(value, Sequence) and not isinstance(value, str | bytes):
        return [_scrub_value(item, depth + 1) for item in value[:MAX_ITEMS]]
    return scrub_text(str(value))


def _has_id_shape(value: str) -> bool:
    """True only for values that are certainly machine-generated identifiers.

    Anchored and narrow on purpose: anything that is not exactly a UUID or a hex digest
    falls through to :func:`scrub_text` and is redacted as usual, so a caller who puts a
    phone number in ``X-Request-ID`` gains nothing. All-digit values are excluded because a
    numeric run of this length is a phone or card number, not an identifier.
    """
    if value.isdigit():
        return False
    return bool(_UUID_SHAPE.match(value) or _HEX_SHAPE.match(value))


def is_opaque_id(key: str, value: object) -> bool:
    """True when ``value`` is a correlation id that must survive scrubbing intact.

    Public because :mod:`personal_organizer.observability.sentry` needs the same judgement:
    it scrubs Sentry events, which carry ``trace_id``, ``span_id``, ``event_id`` and
    ``release``, and without this it would rewrite every one of them to a marker -- exactly
    the defect this function exists to prevent on the log path.
    """
    return key.casefold() in OPAQUE_ID_KEYS and isinstance(value, str) and _has_id_shape(value)


def _scrub_allowlisted(key: str, value: object) -> object:
    """Scrub a value whose key is on the allowlist, keeping correlation ids intact."""
    if is_opaque_id(key, value):
        return value
    return _scrub_value(value)


def redact_event(
    event_dict: dict[str, Any], *, pepper: str, allow_raw: bool = False
) -> dict[str, Any]:
    """Apply the four layers to one structlog event dict."""
    out: dict[str, Any] = {}
    for raw_key, value in event_dict.items():
        key = str(raw_key).casefold()

        if allow_raw and isinstance(value, Unredacted):
            out[str(raw_key)] = value.value
            continue
        if isinstance(value, Unredacted):
            value = value.value  # unwrap, then redact normally

        if key in SECRET_KEYS:
            out[str(raw_key)] = REDACTED_SECRET
        elif key in HASH_KEYS:
            out[f"{raw_key}_hash"] = hash_identifier(value, pepper)
        elif key in SAFE_KEYS:
            out[str(raw_key)] = _scrub_allowlisted(key, value)
        else:
            out[str(raw_key)] = _shape(value)
    return out


def make_redactor(*, pepper: str, allow_raw: bool = False) -> Any:
    """Build the structlog processor."""

    def processor(_logger: Any, _method: str, event_dict: dict[str, Any]) -> dict[str, Any]:
        return redact_event(event_dict, pepper=pepper, allow_raw=allow_raw)

    return processor


__all__ = [
    "CONTENT_KEYS",
    "FORBIDDEN_KEYS",
    "HASH_KEYS",
    "MAX_STR",
    "OPAQUE_ID_KEYS",
    "SAFE_KEYS",
    "SECRET_KEYS",
    "Unredacted",
    "hash_identifier",
    "is_opaque_id",
    "make_redactor",
    "redact_event",
    "scrub_text",
]
