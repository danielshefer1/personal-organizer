"""structlog configuration.

Everything -- our own loggers *and* third-party stdlib loggers -- is funnelled through one
renderer chain whose last step before rendering is the redactor. That matters more than it
looks: Procrastinate logs job kwargs through the stdlib logger, and from Iteration 02 those
kwargs carry WhatsApp message bodies. A structlog-only redaction chain would miss them
entirely.
"""

from __future__ import annotations

import logging
import sys
from typing import Any, TextIO

import structlog

from personal_organizer.observability.redaction import make_redactor
from personal_organizer.settings import Settings

#: Third-party loggers that are noisy or that duplicate our own instrumentation.
_QUIET_LOGGERS = {
    "uvicorn.access": logging.WARNING,  # we emit our own, with the route template
    "sqlalchemy.engine": logging.WARNING,
    "httpx": logging.WARNING,
    "httpcore": logging.WARNING,
}


def _service_context(settings: Settings) -> Any:
    def processor(_logger: Any, _method: str, event_dict: dict[str, Any]) -> dict[str, Any]:
        event_dict.setdefault("service", "personal-organizer")
        event_dict.setdefault("env", settings.app.env)
        event_dict.setdefault("component", settings.app.component)
        event_dict.setdefault("release", settings.app.release)
        return event_dict

    return processor


def configure_logging(settings: Settings, stream: TextIO | None = None) -> None:
    """Configure structlog and the stdlib root logger. Idempotent.

    ``stream`` exists so tests can assert on the bytes the handler actually emits. Binding
    ``sys.stdout`` at call time otherwise makes capture-based assertions depend on when the
    configuration ran relative to the capture, which is how a redaction test passes
    vacuously.
    """
    timestamper = structlog.processors.TimeStamper(fmt="iso", utc=True)

    shared: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        timestamper,
        structlog.processors.StackInfoRenderer(),
        # Structured tracebacks, so the redactor can recurse into frames and scrub
        # exception values. A pre-rendered traceback string is far harder to scrub.
        structlog.processors.dict_tracebacks,
        _service_context(settings),
    ]

    renderer: Any = (
        structlog.processors.JSONRenderer(sort_keys=False)
        if settings.logging.renderer == "json"
        else structlog.dev.ConsoleRenderer(colors=True)
    )

    structlog.configure(
        processors=[*shared, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            # Last step before rendering, so nothing can be added after redaction.
            make_redactor(
                pepper=settings.logging.pii_pepper.get_secret_value(),
                allow_raw=settings.logging.allow_raw_pii,
            ),
            renderer,
        ],
    )

    handler = logging.StreamHandler(stream if stream is not None else sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(settings.logging.level)

    for name, level in _QUIET_LOGGERS.items():
        logging.getLogger(name).setLevel(level)


__all__ = ["configure_logging"]
