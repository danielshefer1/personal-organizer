from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from personal_organizer.db.models.tenant import NETWORK_WHATSAPP
from personal_organizer.messaging.inbox import InboxRow
from personal_organizer.messaging.tenancy import identity_keys, network_for


def _row(*, user_id: str | None, phone: str | None, channel: str = "whatsapp") -> InboxRow:
    return InboxRow(
        id=uuid4(),
        channel=channel,
        provider_message_id="wamid.X",
        sender_key=f"uid:{user_id}" if user_id else f"tel:{phone}",
        sender_user_id=user_id,
        sender_phone=phone,
        message_type="text",
        body="hi",
        reply_id=None,
        sent_at=datetime.now(UTC),
        processed_at=None,
        disposition=None,
    )


@pytest.mark.parametrize("channel", ["whatsapp", "gowa"])
def test_both_whatsapp_channels_are_one_network(channel: str) -> None:
    assert network_for(channel) == NETWORK_WHATSAPP == "whatsapp"


def test_an_unknown_channel_has_no_network() -> None:
    with pytest.raises(ValueError, match="telegram"):
        network_for("telegram")


@pytest.mark.parametrize(
    ("user_id", "phone", "keys"),
    [
        ("US.1", "+31612345678", ("uid:US.1", "tel:+31612345678")),
        (None, "+31612345678", ("tel:+31612345678",)),
        ("US.1", None, ("uid:US.1",)),
    ],
)
def test_identity_keys_strongest_first(
    user_id: str | None, phone: str | None, keys: tuple[str, ...]
) -> None:
    row = _row(user_id=user_id, phone=phone)
    assert identity_keys(row) == keys
    assert identity_keys(row)[0] == row.sender_key
