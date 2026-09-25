"""FastAPI dependencies."""

from __future__ import annotations

from typing import Annotated

import procrastinate
from fastapi import Depends, Request

from personal_organizer.db.engine import Database
from personal_organizer.settings import Settings, get_settings


def get_db(request: Request) -> Database:
    database: Database = request.app.state.db
    return database


def get_procrastinate(request: Request) -> procrastinate.App:
    app: procrastinate.App = request.app.state.procrastinate
    return app


DbDep = Annotated[Database, Depends(get_db)]
ProcrastinateDep = Annotated[procrastinate.App, Depends(get_procrastinate)]
SettingsDep = Annotated[Settings, Depends(get_settings)]

__all__ = ["DbDep", "ProcrastinateDep", "SettingsDep", "get_db", "get_procrastinate"]
