"""FastAPI dependencies.

Everything here is request-scoped and reads off ``request.app.state``, which is populated by
:func:`personal_organizer.api.app.create_app` and the lifespan. ``SettingsDep`` deliberately
does *not* call :func:`~personal_organizer.settings.get_settings`: that returns an
``lru_cache``d process global, so an app built by ``create_app(settings)`` would report a
different ``env`` and ``release`` than it was constructed with.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated

import procrastinate
from fastapi import Depends, Request

from personal_organizer.db.engine import Database
from personal_organizer.interfaces.calendar import ConnectLinker
from personal_organizer.interfaces.channel import InboundChannel
from personal_organizer.messaging.ingress import IngressStore
from personal_organizer.settings import Settings


def get_db(request: Request) -> Database:
    database: Database = request.app.state.db
    return database


def get_procrastinate(request: Request) -> procrastinate.App:
    app: procrastinate.App = request.app.state.procrastinate
    return app


def get_app_settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def get_ingress_store(request: Request) -> IngressStore:
    store: IngressStore = request.app.state.ingress_store
    return store


def get_connect_linker(request: Request) -> ConnectLinker:
    """Composio, built by the lifespan when ``COMPOSIO__ENABLED``. Tests set a fake."""
    linker: ConnectLinker = request.app.state.connect_linker
    return linker


def inbound_channel(name: str) -> Callable[[Request], InboundChannel]:
    """A dependency resolving the inbound channel ``name``, as mounted by ``create_app``.

    One per webhook route: several channels can be live at once, each on its own path.
    """

    def get(request: Request) -> InboundChannel:
        channels: dict[str, InboundChannel] = request.app.state.inbound_channels
        return channels[name]

    return get


DbDep = Annotated[Database, Depends(get_db)]
ProcrastinateDep = Annotated[procrastinate.App, Depends(get_procrastinate)]
SettingsDep = Annotated[Settings, Depends(get_app_settings)]
IngressStoreDep = Annotated[IngressStore, Depends(get_ingress_store)]
ConnectLinkerDep = Annotated[ConnectLinker, Depends(get_connect_linker)]

__all__ = [
    "ConnectLinkerDep",
    "DbDep",
    "IngressStoreDep",
    "ProcrastinateDep",
    "SettingsDep",
    "get_app_settings",
    "get_connect_linker",
    "get_db",
    "get_ingress_store",
    "get_procrastinate",
    "inbound_channel",
]
