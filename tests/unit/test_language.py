from __future__ import annotations

import pytest

from personal_organizer.messaging.language import detect_language, strip_bidi


class TestDetectLanguage:
    @pytest.mark.parametrize(
        "text",
        ["שלום", "hi שלום", "מה נשמע?", "֐", "׿", "Meeting at 10 בבוקר"],
    )
    def test_any_hebrew_letter_means_hebrew(self, text: str) -> None:
        assert detect_language(text) == "he"

    @pytest.mark.parametrize(
        "text",
        [None, "", "   ", "hello", "Grüße", "مرحبا", "֏", "؀", "123", "👍"],
    )
    def test_anything_else_is_english(self, text: str | None) -> None:
        assert detect_language(text) == "en"


class TestStripBidi:
    def test_removes_the_marks_hebrew_keyboards_insert(self) -> None:
        assert strip_bidi("‏כן‎") == "כן"
        assert strip_bidi("a⁧b⁩c‫d‬") == "abcd"

    def test_leaves_everything_else_alone(self) -> None:
        assert strip_bidi(" Tel Aviv-Yafo 1. ") == " Tel Aviv-Yafo 1. "
