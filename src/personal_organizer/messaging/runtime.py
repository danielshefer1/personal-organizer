"""The worker's outbound channels, registered process-wide by name.

Tasks get no dependency injection, so -- exactly like :func:`~personal_organizer.db.engine.
get_database` -- the instances built in the worker's lifespan are registered here and looked
up by the task wrappers. Only the wrappers: the logic they call takes a resolver as an
argument, so tests pass fakes rather than patching this module.

Several channels may be registered at once (Meta and the GOWA gateway, say). A reply goes
out on the channel its inbound message arrived on, which the inbox row records by name.
"""

from __future__ import annotations

from typing import Final

from personal_organizer.core.errors import ChannelNotConfiguredError
from personal_organizer.interfaces.channel import OutboundChannel

#: The flag that turns each known channel on, for an error that says what to set.
_ENABLE_FLAGS: Final = {"whatsapp": "WHATSAPP__ENABLED", "gowa": "GOWA__ENABLED"}

_outbound: dict[str, OutboundChannel] = {}


def register_outbound_channel(channel: OutboundChannel) -> None:
    _outbound[channel.name] = channel


def unregister_outbound_channel(name: str) -> None:
    _outbound.pop(name, None)


def get_outbound_channel(name: str) -> OutboundChannel:
    try:
        return _outbound[name]
    except KeyError:
        flag = _ENABLE_FLAGS.get(name, "its *_ENABLED flag")
        msg = f"No outbound channel {name!r}: is {flag} set on the worker?"
        raise ChannelNotConfiguredError(msg) from None


def outbound_channel_names() -> tuple[str, ...]:
    return tuple(sorted(_outbound))


__all__ = [
    "get_outbound_channel",
    "outbound_channel_names",
    "register_outbound_channel",
    "unregister_outbound_channel",
]
