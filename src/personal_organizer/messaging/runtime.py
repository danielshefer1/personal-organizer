"""The worker's outbound channel, registered process-wide.

Tasks get no dependency injection, so -- exactly like :func:`~personal_organizer.db.engine.
get_database` -- the instance built in the worker's lifespan is registered here and looked up
by the task wrappers. Only the wrappers: the logic they call takes the channel as an argument,
so tests pass a fake rather than patching this module.
"""

from __future__ import annotations

from personal_organizer.core.errors import ChannelNotConfiguredError
from personal_organizer.interfaces.channel import OutboundChannel

_outbound: OutboundChannel | None = None


def set_outbound_channel(channel: OutboundChannel | None) -> None:
    global _outbound
    _outbound = channel


def get_outbound_channel() -> OutboundChannel:
    if _outbound is None:
        msg = "No outbound channel: is WHATSAPP__ENABLED set on the worker?"
        raise ChannelNotConfiguredError(msg)
    return _outbound


__all__ = ["get_outbound_channel", "set_outbound_channel"]
