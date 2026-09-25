"""Queue names.

The full set is declared now and only a subset is subscribed to, which costs nothing and
avoids a rename later. The separation that matters is ``agent`` from ``webhooks``: once
Iteration 04 lands, a slow LLM turn sharing a queue with ingress would starve webhook
acknowledgement, and Meta retries anything it does not see acked.

``exports`` is separate for a different reason -- Iterations 18 and 20 build files in memory
in the worker, so that queue eventually wants its own service with more memory and a
concurrency of 1.
"""

from enum import StrEnum


class Queue(StrEnum):
    DEFAULT = "default"
    WEBHOOKS = "webhooks"
    AGENT = "agent"
    EXPORTS = "exports"
    MAINTENANCE = "maintenance"


__all__ = ["Queue"]
