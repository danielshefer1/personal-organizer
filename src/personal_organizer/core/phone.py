"""Phone number normalisation.

One canonical form, E.164 with a leading ``+``, used everywhere a phone number is compared,
hashed or stored. Meta sends ``wa_id`` and ``from`` as bare digits (``31612345678``) while a
human writes the same number as ``+31 6 1234 5678``; without one normaliser in front of both,
the allowlist silently misses and ``hash_identifier`` gives the same person two hashes.

Deliberately not a full numbering-plan validator (that is ``phonenumbers``, a large dependency
for a check this code does not need). It enforces the shape E.164 guarantees -- a non-zero
leading digit and 8 to 15 digits in total -- which is enough to reject a national-format
number (``0612345678``) that would otherwise be compared as if it were international.
"""

from __future__ import annotations

import re
from typing import Final

_SEPARATORS: Final = re.compile(r"[\s().-]")
_E164_DIGITS: Final = re.compile(r"[1-9]\d{7,14}")


def normalise_e164(raw: object) -> str | None:
    """Return ``raw`` as ``+<digits>``, or ``None`` if it is not an international number.

    Accepts a leading ``+`` or ``00`` international prefix, or bare digits as Meta sends
    them. Spaces, dots, dashes and parentheses are ignored. Anything else -- letters, a
    national trunk prefix, too few or too many digits -- is ``None``, never a guess.
    """
    if not isinstance(raw, str | int) or isinstance(raw, bool):
        return None
    candidate = _SEPARATORS.sub("", str(raw))
    if candidate.startswith("+"):
        candidate = candidate[1:]
    elif candidate.startswith("00"):
        candidate = candidate[2:]
    if not _E164_DIGITS.fullmatch(candidate):
        return None
    return f"+{candidate}"


__all__ = ["normalise_e164"]
