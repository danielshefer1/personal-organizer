from __future__ import annotations

import string
from typing import Any
from uuid import uuid4

import pytest
from itsdangerous import URLSafeTimedSerializer

from personal_organizer.onboarding.tokens import (
    LINK_SALT,
    MAX_TOKEN_LENGTH,
    STATE_SALT,
    TokenPayload,
    sign,
    verify,
)

SECRET = "s" * 40
PAYLOAD = TokenPayload(tenant_id=uuid4(), nonce="n0nce-value")


def test_round_trip() -> None:
    token = sign(PAYLOAD, secret=SECRET, salt=LINK_SALT)
    assert verify(token, secret=SECRET, salt=LINK_SALT, max_age_s=900) == PAYLOAD


def test_url_safe() -> None:
    token = sign(PAYLOAD, secret=SECRET, salt=LINK_SALT)
    assert all(char.isalnum() or char in "-_." for char in token)


def test_a_link_token_is_not_a_state_token() -> None:
    token = sign(PAYLOAD, secret=SECRET, salt=LINK_SALT)
    assert verify(token, secret=SECRET, salt=STATE_SALT, max_age_s=900) is None


def test_another_secret_fails() -> None:
    token = sign(PAYLOAD, secret=SECRET, salt=LINK_SALT)
    assert verify(token, secret="t" * 40, salt=LINK_SALT, max_age_s=900) is None


def test_expired() -> None:
    token = sign(PAYLOAD, secret=SECRET, salt=LINK_SALT)
    assert verify(token, secret=SECRET, salt=LINK_SALT, max_age_s=-1) is None


@pytest.mark.parametrize("token", ["", "garbage", "a.b.c", "eyJ0IjoiYSJ9.AAAA.BBBB"])
def test_garbage(token: str) -> None:
    assert verify(token, secret=SECRET, salt=LINK_SALT, max_age_s=900) is None


_B64 = string.ascii_uppercase + string.ascii_lowercase + string.digits + "-_"


def _flip(token: str, segment: int) -> str:
    """Change the middle character of one dot-separated segment to a different 6-bit value.

    XOR with 0b100000 changes a high bit of the base64 index, so the decoded bytes always
    differ (unlike the last character of a segment, whose low bits may be padding).
    """
    parts = token.split(".")
    seg = parts[segment]
    mid = len(seg) // 2
    parts[segment] = seg[:mid] + _B64[_B64.index(seg[mid]) ^ 0b100000] + seg[mid + 1 :]
    return ".".join(parts)


@pytest.mark.parametrize("segment", [0, 1, 2])
def test_tampered(segment: int) -> None:
    for _ in range(200):
        payload = TokenPayload(tenant_id=uuid4(), nonce="n0nce-value")
        token = sign(payload, secret=SECRET, salt=LINK_SALT)
        flipped = _flip(token, segment)
        assert flipped != token
        assert verify(flipped, secret=SECRET, salt=LINK_SALT, max_age_s=900) is None


@pytest.mark.parametrize("token", [None, 123, b"abc", ["x"]])
def test_non_string_token_is_none(token: Any) -> None:
    assert verify(token, secret=SECRET, salt=LINK_SALT, max_age_s=900) is None


@pytest.mark.parametrize("token", ["\ud800.a.b", "\u00e9.a.b"])
def test_non_ascii_token_is_none(token: str) -> None:
    assert verify(token, secret=SECRET, salt=LINK_SALT, max_age_s=900) is None


def test_oversized_token_is_none() -> None:
    token = "a" * (MAX_TOKEN_LENGTH + 1)
    assert verify(token, secret=SECRET, salt=LINK_SALT, max_age_s=900) is None


def test_cap_length_garbage_is_none_and_real_tokens_are_far_below_it() -> None:
    token = sign(PAYLOAD, secret=SECRET, salt=LINK_SALT)
    assert len(token) < MAX_TOKEN_LENGTH // 4
    at_cap = "a" * MAX_TOKEN_LENGTH
    assert verify(at_cap, secret=SECRET, salt=LINK_SALT, max_age_s=900) is None  # fails sig


@pytest.mark.parametrize(
    "data",
    [
        ["not", "a", "dict"],
        {"t": "not-a-uuid", "n": "x"},
        {"t": str(uuid4())},
        {"t": str(uuid4()), "n": ""},
        {"t": str(uuid4()), "n": 7},
    ],
)
def test_a_validly_signed_bad_shape(data: object) -> None:
    token = URLSafeTimedSerializer(SECRET, salt=LINK_SALT).dumps(data)
    assert verify(token, secret=SECRET, salt=LINK_SALT, max_age_s=900) is None
