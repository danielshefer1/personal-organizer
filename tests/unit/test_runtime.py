from __future__ import annotations

from collections.abc import Iterator

import pytest

from personal_organizer.core.errors import ChannelNotConfiguredError
from personal_organizer.interfaces.channel import OutboundMessage
from personal_organizer.messaging import runtime


class _Channel:
    def __init__(self, name: str) -> None:
        self.name = name

    async def send_text(self, message: OutboundMessage) -> str:  # pragma: no cover
        return "id"

    async def mark_read(self, provider_message_id: str) -> None:  # pragma: no cover
        return None


@pytest.fixture(autouse=True)
def _empty_registry() -> Iterator[None]:
    for name in runtime.outbound_channel_names():
        runtime.unregister_outbound_channel(name)
    yield
    for name in runtime.outbound_channel_names():
        runtime.unregister_outbound_channel(name)


def test_channels_are_looked_up_by_name() -> None:
    meta, gowa = _Channel("whatsapp"), _Channel("gowa")
    runtime.register_outbound_channel(meta)
    runtime.register_outbound_channel(gowa)

    assert runtime.get_outbound_channel("whatsapp") is meta
    assert runtime.get_outbound_channel("gowa") is gowa
    assert runtime.outbound_channel_names() == ("gowa", "whatsapp")


def test_unregistering_removes_only_that_channel() -> None:
    runtime.register_outbound_channel(_Channel("whatsapp"))
    runtime.register_outbound_channel(_Channel("gowa"))

    runtime.unregister_outbound_channel("gowa")
    runtime.unregister_outbound_channel("gowa")  # idempotent

    assert runtime.outbound_channel_names() == ("whatsapp",)


@pytest.mark.parametrize(
    ("name", "flag"),
    [("gowa", "GOWA__ENABLED"), ("whatsapp", "WHATSAPP__ENABLED"), ("telegram", "_ENABLED")],
)
def test_a_missing_channel_names_the_flag_to_set(name: str, flag: str) -> None:
    with pytest.raises(ChannelNotConfiguredError, match=flag):
        runtime.get_outbound_channel(name)
