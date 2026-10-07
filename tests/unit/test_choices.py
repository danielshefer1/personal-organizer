"""Done-When: "a choice works on every channel". A numbered reply, a label or alias reply, a
Meta button's ``reply_id`` and a GOWA selection's ``selected_id`` all resolve the same way."""

from __future__ import annotations

import pytest

from personal_organizer.messaging.choices import (
    MAX_OPTIONS,
    Choice,
    Option,
    render_text,
    resolve,
)

YES = Option("zone:correct", "Correct", ("yes", "כן"))
NO = Option("zone:change", "Change", ("no", "לא"))
CHOICE = Choice("Is your time zone Asia/Jerusalem?", (YES, NO))


def _options(count: int) -> tuple[Option, ...]:
    return tuple(Option(f"o{n}", f"Option {n}") for n in range(1, count + 1))


class TestNumberedReplies:
    @pytest.mark.parametrize("text", ["1", "1.", "1)", " 1 ", "\t1.\n", "1 )", "‏1"])
    def test_the_first_option(self, text: str) -> None:
        assert resolve(CHOICE, reply_id=None, text=text) is YES

    @pytest.mark.parametrize("text", ["2", "2.", " 2) "])
    def test_the_second_option(self, text: str) -> None:
        assert resolve(CHOICE, reply_id=None, text=text) is NO

    @pytest.mark.parametrize("text", ["3", "9", "0", "12", "1.5", "-1", "#1", "1).", "one"])
    def test_out_of_range_or_not_a_single_digit(self, text: str) -> None:
        assert resolve(CHOICE, reply_id=None, text=text) is None


class TestWordReplies:
    @pytest.mark.parametrize(
        "text", ["Correct", "correct", "CORRECT", "  correct  ", "Yes!", "yes", "YES.", "כן", "‏כן!"]
    )
    def test_the_label_or_an_alias_case_folded(self, text: str) -> None:
        assert resolve(CHOICE, reply_id=None, text=text) is YES

    @pytest.mark.parametrize("text", ["Change", "no", "NO!", "לא"])
    def test_the_other_option_by_word(self, text: str) -> None:
        assert resolve(CHOICE, reply_id=None, text=text) is NO

    def test_a_hebrew_label(self) -> None:
        right, change = Option("a", "נכון"), Option("b", "לשנות")
        choice = Choice("האם אזור הזמן שלך הוא Asia/Jerusalem?", (right, change))
        assert resolve(choice, reply_id=None, text="‏נכון!") is right
        assert resolve(choice, reply_id=None, text="לשנות") is change

    def test_casefold_not_lower(self) -> None:
        street = Option("s", "Straße")
        assert resolve(Choice("Where?", (street,)), reply_id=None, text="STRASSE") is street

    @pytest.mark.parametrize("text", [None, "", "   ", "maybe", "yes please", "Corrected"])
    def test_anything_else_is_none(self, text: str | None) -> None:
        assert resolve(CHOICE, reply_id=None, text=text) is None


class TestReplyIds:
    def test_a_meta_button(self) -> None:
        assert resolve(CHOICE, reply_id="zone:change", text="Change") is NO

    def test_a_gowa_selection(self) -> None:
        # GOWA passes the row's own text alongside ``selected_id``; the id decides.
        assert resolve(CHOICE, reply_id="zone:correct", text="1. Correct") is YES

    def test_the_id_wins_over_the_text(self) -> None:
        assert resolve(CHOICE, reply_id="zone:change", text="1") is NO

    def test_an_unknown_id_falls_back_to_the_text(self) -> None:
        assert resolve(CHOICE, reply_id="stale-button", text="2") is NO

    def test_an_unknown_id_and_unknown_text(self) -> None:
        assert resolve(CHOICE, reply_id="stale-button", text="hmm") is None


class TestValidation:
    @pytest.mark.parametrize("count", [0, MAX_OPTIONS + 1])
    def test_one_to_nine_options(self, count: int) -> None:
        with pytest.raises(ValueError, match="1 to 9"):
            Choice("?", _options(count))

    @pytest.mark.parametrize("count", [1, MAX_OPTIONS])
    def test_the_bounds_are_allowed(self, count: int) -> None:
        assert len(Choice("?", _options(count)).options) == count

    def test_option_ids_are_distinct(self) -> None:
        with pytest.raises(ValueError, match="distinct"):
            Choice("?", (Option("x", "A"), Option("x", "B")))


class TestRenderText:
    def test_english(self) -> None:
        assert render_text(CHOICE, "en") == (
            "Is your time zone Asia/Jerusalem?\n\n1. Correct\n2. Change\n\nReply with a number."
        )

    def test_hebrew(self) -> None:
        choice = Choice(
            "האם אזור הזמן שלך הוא Asia/Jerusalem?", (Option("a", "נכון"), Option("b", "לשנות"))
        )
        assert render_text(choice, "he") == (
            "האם אזור הזמן שלך הוא Asia/Jerusalem?\n\n1. נכון\n2. לשנות\n\nנא להשיב במספר."
        )

    def test_an_unknown_language_gets_the_english_hint(self) -> None:
        assert render_text(CHOICE, "fr").endswith("Reply with a number.")

    @pytest.mark.parametrize("count", range(1, MAX_OPTIONS + 1))
    def test_every_rendered_number_resolves_to_its_option(self, count: int) -> None:
        choice = Choice("?", _options(count))
        rendered = render_text(choice, "en")
        for number, option in enumerate(choice.options, start=1):
            assert f"{number}. {option.label}" in rendered
            assert resolve(choice, reply_id=None, text=str(number)) is option
