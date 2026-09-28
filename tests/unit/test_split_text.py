from __future__ import annotations

import pytest

from personal_organizer.messaging.text import WHATSAPP_TEXT_LIMIT, split_text


class TestSplitText:
    def test_a_short_body_is_one_chunk(self) -> None:
        assert split_text("hello") == ["hello"]

    def test_an_empty_body_is_one_empty_chunk(self) -> None:
        assert split_text("") == [""]

    def test_exactly_the_limit_is_one_chunk(self) -> None:
        assert split_text("a" * WHATSAPP_TEXT_LIMIT) == ["a" * WHATSAPP_TEXT_LIMIT]

    @pytest.mark.parametrize(
        ("body", "expected"),
        [
            pytest.param("aaaa\n\nbbbb cc", ["aaaa\n\n", "bbbb cc"], id="paragraph"),
            pytest.param("aaaa\nbbbb cc", ["aaaa\n", "bbbb cc"], id="line"),
            pytest.param("aa. bbbb cc", ["aa. ", "bbbb cc"], id="sentence"),
            pytest.param("aaaa bbbbb", ["aaaa ", "bbbbb"], id="word"),
            pytest.param("abcdefghij", ["abcdefg", "hij"], id="hard-cut"),
        ],
    )
    def test_the_best_boundary_that_fits_wins(self, body: str, expected: list[str]) -> None:
        assert split_text(body, limit=7) == expected

    @pytest.mark.parametrize("limit", [1, 5, 50, WHATSAPP_TEXT_LIMIT])
    def test_no_chunk_exceeds_the_limit_and_nothing_is_lost(self, limit: int) -> None:
        body = ("Remind me about the dentist.\n\nThen call mum about the results. " * 400)[:20_000]
        chunks = split_text(body, limit=limit)
        assert all(len(chunk) <= limit for chunk in chunks)
        assert "".join(chunks) == body

    def test_a_zero_limit_is_refused(self) -> None:
        with pytest.raises(ValueError, match="positive"):
            split_text("x", limit=0)
