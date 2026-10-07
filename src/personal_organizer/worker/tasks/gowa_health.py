"""``system:gowa_health`` -- the alarm for a linked device that has stopped working.

A gateway whose device was logged out still answers its REST API. Nothing fails loudly:
inbound messages simply stop arriving, and replies fail as transient 401s. So every five
minutes the worker asks the gateway what ``po-gowa status`` asks, and while the answer is
bad it says so twice: an error-level ``gowa.unhealthy`` log line, and a Sentry event.

The Sentry event is explicit because nothing else would send it: ``init_sentry`` switches
the logging integration's events off (``event_level=None``), so no log line, at any level,
becomes a Sentry event by itself. Every report carries one fixed fingerprint, so Sentry
groups them all into a single issue -- *that issue is the alert* -- with the reason as a
tag. Resolve it once the gateway is fixed, and the next outage raises it again.

Registered only on a worker with ``GOWA__ENABLED`` (``worker.app``). It keeps no state:
each run reports what it sees.
"""

from __future__ import annotations

from typing import Final

import httpx
import procrastinate
import sentry_sdk
import structlog

from personal_organizer.providers.channel.gowa.status import (
    GatewayHealth,
    Unhealthy,
    check_status,
)
from personal_organizer.settings import GowaSettings
from personal_organizer.worker.queues import Queue

log = structlog.get_logger(__name__)

GOWA_HEALTH_TASK: Final = "system:gowa_health"
GOWA_HEALTH_CRON: Final = "*/5 * * * *"
#: The Sentry grouping key: every report, whatever its reason, lands in one issue.
SENTRY_FINGERPRINT: Final = ("gowa.unhealthy",)
#: Far inside the five-minute period, so a hung gateway cannot make runs overlap.
GOWA_HEALTH_TIMEOUT: Final = httpx.Timeout(10.0, connect=5.0)


def _report(reason: Unhealthy, health: GatewayHealth) -> None:
    with sentry_sdk.new_scope() as scope:
        scope.fingerprint = list(SENTRY_FINGERPRINT)
        scope.set_tag("reason", reason.value)
        if health.status_code is not None:
            scope.set_tag("status_code", str(health.status_code))
        sentry_sdk.capture_message("gowa.unhealthy", level="error")


async def _ask(gowa: GowaSettings, client: httpx.AsyncClient) -> GatewayHealth:
    try:
        return await check_status(client, gowa)
    except (httpx.InvalidURL, ValueError) as exc:
        # A malformed base URL or non-Latin-1 credentials: httpx raises before sending
        # anything. The gateway cannot be asked, which is as bad as it being down. Record
        # the class only: ``InvalidURL``'s message quotes the URL.
        return GatewayHealth(Unhealthy.BAD_RESPONSE, error_type=type(exc).__name__)


async def run_health_check(gowa: GowaSettings, client: httpx.AsyncClient) -> GatewayHealth:
    """Ask once; log, and report to Sentry when unhealthy. Returns what it found."""
    health = await _ask(gowa, client)
    reason = health.reason
    if reason is None:
        log.debug("gowa.healthy")
        return health
    log.error(
        "gowa.unhealthy",
        reason=reason.value,
        status_code=health.status_code,
        error_type=health.error_type,
    )
    _report(reason, health)
    return health


def register(
    app: procrastinate.App,
    gowa: GowaSettings,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> None:
    """Attach the periodic check. ``transport`` exists for tests (``httpx.MockTransport``)."""

    @app.periodic(cron=GOWA_HEALTH_CRON)
    @app.task(
        queue=Queue.MAINTENANCE.value,
        name=GOWA_HEALTH_TASK,
        queueing_lock=GOWA_HEALTH_TASK,
    )
    async def gowa_health(
        timestamp: int,  # noqa: ARG001 - periodic tasks receive their schedule time
    ) -> None:
        async with httpx.AsyncClient(timeout=GOWA_HEALTH_TIMEOUT, transport=transport) as client:
            await run_health_check(gowa, client)


__all__ = [
    "GOWA_HEALTH_CRON",
    "GOWA_HEALTH_TASK",
    "GOWA_HEALTH_TIMEOUT",
    "SENTRY_FINGERPRINT",
    "register",
    "run_health_check",
]
