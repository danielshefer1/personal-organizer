"""Task registration.

Each module exposes ``register(app)`` and is listed here. Tasks are attached per App
instance rather than to a shared module-level Blueprint -- see the note in ``system.py``.
"""

from __future__ import annotations

from collections.abc import Callable

import procrastinate

from personal_organizer.worker.tasks import system

REGISTRARS: list[Callable[[procrastinate.App], None]] = [system.register]

__all__ = ["REGISTRARS"]
