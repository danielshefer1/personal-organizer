"""``po-db`` -- database bootstrap and verification.

Invoked by Railway's pre-deploy command (``po-db bootstrap && alembic upgrade head``) and by
the runbook after a deploy (``po-db check``).
"""

from __future__ import annotations

import argparse
import asyncio
import sys

import structlog

from personal_organizer.db.bootstrap import check_database, run_bootstrap
from personal_organizer.observability.logging import configure_logging
from personal_organizer.settings import get_settings

log = structlog.get_logger(__name__)


async def _bootstrap() -> int:
    await run_bootstrap(get_settings())
    return 0


async def _check() -> int:
    problems = await check_database(get_settings())
    if problems:
        for problem in problems:
            log.error("db.check.failed", error_code=problem)
        return 1
    log.info("db.check.ok")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="po-db")
    parser.add_argument("command", choices=["bootstrap", "check"])
    args = parser.parse_args(argv)

    settings = get_settings()
    configure_logging(settings)

    code = asyncio.run(_bootstrap() if args.command == "bootstrap" else _check())
    if code:
        sys.exit(code)
    return code


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
