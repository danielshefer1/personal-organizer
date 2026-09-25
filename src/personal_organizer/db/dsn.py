"""DSN normalisation.

Railway hands out URLs shaped like ``postgres://user:pw@host:5432/railway?sslmode=require``.
Three things are wrong with feeding that to our consumers directly:

1. SQLAlchemy rejects the ``postgres://`` scheme; it wants ``postgresql://``.
2. **asyncpg does not understand libpq's query parameters.** ``sslmode``, ``connect_timeout``
   and friends arrive as unexpected keyword arguments and raise at connect time. They have to
   be lifted out of the URL and translated into ``connect_args``.
3. Each consumer needs a different driver: asyncpg for the SQLAlchemy app engine, psycopg for
   Alembic (sync), and a bare libpq conninfo for Procrastinate -- which ships no asyncpg
   connector, so the queue and the ORM genuinely do run on different drivers.
"""

from __future__ import annotations

import ssl
from typing import Any, Literal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

Driver = Literal["asyncpg", "psycopg", "libpq"]

#: Query parameters libpq accepts but asyncpg does not. Lifted out of the URL for asyncpg.
LIBPQ_ONLY_PARAMS = frozenset(
    {
        "sslmode",
        "sslrootcert",
        "sslcert",
        "sslkey",
        "connect_timeout",
        "target_session_attrs",
        "application_name",
        "options",
    }
)

_DRIVER_SUFFIX: dict[Driver, str] = {
    "asyncpg": "+asyncpg",
    "psycopg": "+psycopg",
    "libpq": "",
}


def _ssl_context_for(sslmode: str) -> ssl.SSLContext | bool:
    """Translate a libpq ``sslmode`` into what asyncpg's ``ssl`` argument expects.

    libpq's ``require`` means "encrypt, but do not verify" -- which is the correct setting on
    Railway's private network, where the hostname is internal and there is no public CA chain.
    """
    match sslmode:
        case "disable" | "allow" | "prefer":
            return False
        case "require":
            context = ssl.create_default_context()
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE  # libpq `require` = encrypt, do not verify
            return context
        case "verify-ca":
            context = ssl.create_default_context()
            context.check_hostname = False
            return context
        case "verify-full":
            return ssl.create_default_context()
        case _:
            msg = f"Unsupported sslmode: {sslmode!r}"
            raise ValueError(msg)


def normalise(raw: str, driver: Driver) -> tuple[str, dict[str, Any]]:
    """Return ``(url, connect_args)`` for the given driver.

    For ``libpq`` the URL is returned with every parameter intact -- Procrastinate passes it
    straight to psycopg, which speaks libpq natively.
    """
    parts = urlsplit(raw.strip())

    scheme = parts.scheme.split("+", 1)[0]
    if scheme == "postgres":
        scheme = "postgresql"
    if scheme != "postgresql":
        msg = f"Not a PostgreSQL DSN: {parts.scheme!r}"
        raise ValueError(msg)

    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    connect_args: dict[str, Any] = {}

    if driver == "asyncpg":
        libpq_params = {k: query.pop(k) for k in list(query) if k in LIBPQ_ONLY_PARAMS}
        if (sslmode := libpq_params.get("sslmode")) is not None:
            connect_args["ssl"] = _ssl_context_for(sslmode)
        if (timeout := libpq_params.get("connect_timeout")) is not None:
            connect_args["timeout"] = float(timeout)

    url = urlunsplit(
        (
            f"{scheme}{_DRIVER_SUFFIX[driver]}",
            parts.netloc,
            parts.path,
            urlencode(query),
            parts.fragment,
        )
    )
    return url, connect_args


__all__ = ["LIBPQ_ONLY_PARAMS", "Driver", "normalise"]
