from __future__ import annotations

import hashlib
import hmac

import pytest

from personal_organizer.providers.channel.hmac_sha256 import (
    rejection_reason,
    sign,
    verify_signature,
)

SECRET = b"app-secret"
BODY = b'{"object":"whatsapp_business_account","entry":[]}'


class TestVerifySignature:
    def test_a_known_vector(self) -> None:
        """Computed independently of ``sign`` so the two cannot agree on the same mistake."""
        header = "sha256=" + hmac.new(SECRET, BODY, hashlib.sha256).hexdigest()
        assert verify_signature(SECRET, BODY, header)

    def test_hex_case_does_not_matter(self) -> None:
        upper = "sha256=" + sign(SECRET, BODY).removeprefix("sha256=").upper()
        assert verify_signature(SECRET, BODY, upper)

    @pytest.mark.parametrize(
        "header",
        [
            pytest.param(None, id="missing"),
            pytest.param("", id="empty"),
            pytest.param(sign(SECRET, BODY).removeprefix("sha256="), id="no-prefix"),
            pytest.param("sha1=" + "0" * 40, id="sha1"),
            pytest.param("sha256=" + "0" * 64, id="wrong-hex"),
            pytest.param("sha256=zz", id="not-hex"),
            pytest.param(sign(SECRET, BODY)[:-2], id="truncated"),
            pytest.param(sign(b"other-secret", BODY), id="other-secret"),
            pytest.param(sign(SECRET, BODY + b" "), id="other-body"),
            pytest.param("sha256=caf\xe9" + "0" * 60, id="non-ascii"),
        ],
    )
    def test_everything_else_is_false_never_an_exception(self, header: str | None) -> None:
        assert verify_signature(SECRET, BODY, header) is False

    def test_a_flipped_byte_in_the_body_fails(self) -> None:
        header = sign(SECRET, BODY)
        tampered = bytearray(BODY)
        tampered[5] ^= 0x01
        assert verify_signature(SECRET, bytes(tampered), header) is False

    @pytest.mark.parametrize("secret", [None, b""])
    def test_no_secret_fails_closed(self, secret: bytes | None) -> None:
        """Even a signature over the right body with an empty key must not pass."""
        assert verify_signature(secret, BODY, sign(b"", BODY)) is False


class TestRejectionReason:
    @pytest.mark.parametrize(
        ("header", "reason"),
        [(None, "missing"), ("", "missing"), ("abc", "malformed"), ("sha256=00", "mismatch")],
    )
    def test_reasons(self, header: str | None, reason: str) -> None:
        assert rejection_reason(header) == reason
