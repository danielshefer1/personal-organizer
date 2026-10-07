"""``po-admin`` pieces that need no database."""

from __future__ import annotations

import pytest

from personal_organizer.admin import cli


def test_the_link_has_no_prefilled_text() -> None:
    """D11: a prefilled "Hi" would make every invitee English."""
    assert cli.wa_link("+972531112222") == "https://wa.me/972531112222"


def test_a_missing_argument_is_a_usage_error() -> None:
    """argparse refuses before any settings are read or any connection is made."""
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["invite"])
    assert excinfo.value.code == 2
