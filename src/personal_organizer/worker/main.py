"""``po-worker`` -- the Procrastinate worker entrypoint.

Deliberately not the ``procrastinate`` CLI: that would give unstructured stdlib logs and no
Sentry, whereas this initialises logging, Sentry and settings exactly as the api does. Since
:func:`configure_logging` installs the redaction chain on the root logger, Procrastinate's
own logging -- which records job kwargs, and would log a message body if one were ever passed
as one (docs/adr/0001 forbids it) -- is redacted too.
"""

from __future__ import annotations

import asyncio
import sys
from contextlib import AsyncExitStack

import httpx
import structlog

from personal_organizer.db.engine import Database, set_database
from personal_organizer.messaging.runtime import (
    outbound_channel_names,
    register_outbound_channel,
    unregister_outbound_channel,
)
from personal_organizer.observability.langfuse import flush_langfuse, init_langfuse
from personal_organizer.observability.logging import configure_logging
from personal_organizer.observability.sentry import init_sentry
from personal_organizer.providers.channel.gowa.outbound import GowaOutbound
from personal_organizer.providers.channel.whatsapp.outbound import WhatsAppOutbound
from personal_organizer.settings import Settings, get_settings
from personal_organizer.worker.app import build_procrastinate_app

log = structlog.get_logger(__name__)


async def _open_whatsapp(settings: Settings, stack: AsyncExitStack) -> None:
    """One HTTP client for the process, closed with it. Tasks reach it through the runtime
    registry, like the database."""
    whatsapp = settings.whatsapp
    http = await stack.enter_async_context(
        httpx.AsyncClient(
            base_url=f"{whatsapp.graph_base_url.rstrip('/')}/{whatsapp.graph_api_version}/",
            timeout=httpx.Timeout(whatsapp.send_timeout_s, connect=5.0),
        )
    )
    channel = WhatsAppOutbound.from_settings(whatsapp, http)
    register_outbound_channel(channel)
    stack.callback(unregister_outbound_channel, channel.name)


async def _open_gowa(settings: Settings, stack: AsyncExitStack) -> None:
    """The gateway's client: basic auth on every request, and room for the typing pause."""
    gowa = settings.gowa
    if gowa.basic_auth_user is None or gowa.basic_auth_password is None:  # pragma: no cover
        msg = "GOWA sending needs GOWA__BASIC_AUTH_USER and GOWA__BASIC_AUTH_PASSWORD"
        raise ValueError(msg)
    http = await stack.enter_async_context(
        httpx.AsyncClient(
            base_url=f"{gowa.base_url}/",
            auth=httpx.BasicAuth(gowa.basic_auth_user, gowa.basic_auth_password.get_secret_value()),
            timeout=httpx.Timeout(gowa.send_timeout_s, connect=5.0),
        )
    )
    channel = GowaOutbound.from_settings(gowa, http)
    register_outbound_channel(channel)
    stack.callback(unregister_outbound_channel, channel.name)


async def run(settings: Settings) -> None:
    async with AsyncExitStack() as stack:
        database = Database(settings)
        stack.push_async_callback(database.dispose)
        await database.wait_ready()
        # Tasks get no dependency injection, so the instance is registered process-wide.
        set_database(database)

        init_langfuse(settings)
        stack.callback(flush_langfuse)

        if settings.whatsapp.enabled:
            await _open_whatsapp(settings, stack)
        if settings.gowa.enabled:
            await _open_gowa(settings, stack)

        app = build_procrastinate_app(settings)
        await stack.enter_async_context(app.open_async())

        log.info(
            "worker.started",
            env=settings.app.env,
            release=settings.app.release,
            whatsapp_enabled=settings.whatsapp.enabled,
            gowa_enabled=settings.gowa.enabled,
            channels=list(outbound_channel_names()),
            allowlist_size=len(settings.whatsapp.allowlist),
        )
        await app.run_worker_async(
            queues=settings.worker.queues,
            concurrency=settings.worker.concurrency,
            install_signal_handlers=True,
            shutdown_graceful_timeout=settings.worker.shutdown_graceful_timeout_s,
        )


def main() -> int:
    settings = get_settings()
    configure_logging(settings)
    init_sentry(settings)
    try:
        asyncio.run(run(settings))
    except KeyboardInterrupt:  # pragma: no cover
        log.info("worker.interrupted")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
