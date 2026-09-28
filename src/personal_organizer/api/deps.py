"""FastAPI dependencies.

Everything here is request-scoped and reads off ``request.app.state``, which is populated by
:func:`personal_organizer.api.app.create_app` and the lifespan. ``SettingsDep`` deliberately
does *not* call :func:`~personal_organizer.settings.get_settings`: that returns an
``lru_cache``d process global, so an app built by ``create_app(settings)`` would report a
different ``env`` and ``release`` than it was constructed with.
"""

from __future__ import annotations

from typing import Annotated

import procrastinate
from fastapi import Depends, Request

from personal_organizer.db.engine import Database
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


def get_inbound_channel(request: Request) -> InboundChannel:
    channel: InboundChannel = request.app.state.inbound_channel
    return channel


DbDep = Annotated[Database, Depends(get_db)]
ProcrastinateDep = Annotated[procrastinate.App, Depends(get_procrastinate)]
SettingsDep = Annotated[Settings, Depends(get_app_settings)]
IngressStoreDep = Annotated[IngressStore, Depends(get_ingress_store)]
InboundChannelDep = Annotated[InboundChannel, Depends(get_inbound_channel)]

__all__ = [
    "DbDep",
    "InboundChannelDep",
    "IngressStoreDep",
    "ProcrastinateDep",
    "SettingsDep",
    "get_app_settings",
    "get_db",
    "get_inbound_channel",
    "get_ingress_store",
    "get_procrastinate",
]
