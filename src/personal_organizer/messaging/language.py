"""The tenant's language, detected rather than asked (D11).

Onboarding has no language step: the first message decides. Any Hebrew letter (U+0590 to
U+05FF) means Hebrew, otherwise English. A one-word "שלום" is unmistakable, and a mixed
"hi שלום" comes from someone who reads Hebrew. The result is stored on the tenant, and the
agent can change it later. Two languages, because that is who the circle is. A third is a
table column in ``onboarding_text``, not a new mechanism.

Also here: :func:`strip_bidi`. Hebrew keyboards and some WhatsApp clients insert invisible
directional marks around words and digits. Unremoved, "\\u200f1" is not "1", and a Hebrew
"yes" behind one is not that "yes" either. Every parser of user replies strips them first.
"""

from __future__ import annotations

from typing import Final

HEBREW: Final = "he"
ENGLISH: Final = "en"

_HEBREW_FIRST: Final = "֐"
_HEBREW_LAST: Final = "׿"

#: LRM, RLM, the embeddings and overrides, and the isolates.
_BIDI_MARKS: Final = str.maketrans("", "", "‎‏‪‫‬‭‮⁦⁧⁨⁩")


def detect_language(text: str | None) -> str:
    """``"he"`` if ``text`` holds any Hebrew-block character, else ``"en"``."""
    if text and any(_HEBREW_FIRST <= char <= _HEBREW_LAST for char in text):
        return HEBREW
    return ENGLISH


def strip_bidi(text: str) -> str:
    """``text`` without Unicode directional marks, which change no meaning but break matching."""
    return text.translate(_BIDI_MARKS)


__all__ = ["ENGLISH", "HEBREW", "detect_language", "strip_bidi"]
