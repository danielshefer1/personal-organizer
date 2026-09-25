"""Messaging channel interface.

WhatsApp now; Iteration 13 requires contract tests so a Telegram adapter can be added in
one to two days after the beta. Nothing above this Protocol may assume WhatsApp.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class OutboundMessage:
    recipient: str
    body: str


@runtime_checkable
class Channel(Protocol):
    async def send(self, message: OutboundMessage) -> str: ...

    def verify_signature(self, raw_body: bytes, signature: str) -> bool: ...


__all__ = ["Channel", "OutboundMessage"]
