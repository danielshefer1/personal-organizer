from __future__ import annotations

import pytest

from personal_organizer.core.phone import normalise_e164


class TestNormaliseE164:
    @pytest.mark.parametrize(
        "raw",
        [
            pytest.param("31612345678", id="meta-bare-digits"),
            pytest.param("+31612345678", id="plus"),
            pytest.param("0031612345678", id="double-zero"),
            pytest.param("+31 6 1234 5678", id="spaces"),
            pytest.param("+31 (6) 12-34.56.78", id="punctuation"),
            pytest.param(31612345678, id="int"),
        ],
    )
    def test_every_spelling_of_one_number_normalises_the_same(self, raw: object) -> None:
        """The allowlist and the log hash both key on this; two spellings of one number
        producing two values is how an allowlisted user gets the invite-only reply."""
        assert normalise_e164(raw) == "+31612345678"

    @pytest.mark.parametrize(
        "raw",
        [
            pytest.param("0612345678", id="national-trunk-prefix"),
            pytest.param("+1234567", id="too-short"),
            pytest.param("+1234567890123456", id="too-long"),
            pytest.param("+31abc12345678", id="letters"),
            pytest.param("", id="empty"),
            pytest.param("+", id="plus-only"),
            pytest.param(None, id="none"),
            pytest.param(True, id="bool"),
            pytest.param(["31612345678"], id="list"),
            pytest.param("user.name", id="username"),
        ],
    )
    def test_anything_else_is_none_never_a_guess(self, raw: object) -> None:
        assert normalise_e164(raw) is None
