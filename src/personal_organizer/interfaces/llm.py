"""LLM provider interface.

Kept provider-neutral so GPT -> Claude failover (Iteration 04) is configuration plus an
adapter rather than a rewrite.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True)
class Message:
    role: str
    content: str


@dataclass(frozen=True)
class Completion:
    content: str
    model: str
    tokens_in: int
    tokens_out: int
    finish_reason: str


@runtime_checkable
class LLMProvider(Protocol):
    async def complete(
        self, messages: Sequence[Message], *, tools: Sequence[dict[str, Any]] | None = None
    ) -> Completion: ...

    async def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


__all__ = ["Completion", "LLMProvider", "Message"]
