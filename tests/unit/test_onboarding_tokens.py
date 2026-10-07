from __future__ import annotations

from uuid import uuid4

import pytest
from itsdangerous import URLSafeTimedSerializer

from personal_organizer.onboarding.tokens import (
    LINK_SALT,
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


def test_tampered() -> None:
    token = sign(PAYLOAD, secret=SECRET, salt=LINK_SALT)
    flipped = token[:-1] + ("A" if token[-1] != "A" else "B")
    assert verify(flipped, secret=SECRET, salt=LINK_SALT, max_age_s=900) is None


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
