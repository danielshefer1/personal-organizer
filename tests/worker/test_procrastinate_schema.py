"""Drift guard for the vendored Procrastinate schema.

alembic/versions/0002 applies a verbatim copy of Procrastinate's schema.sql. If the package
is upgraded without re-vendoring, the database and the library disagree -- and the symptom
would otherwise be a confusing runtime failure in the worker, in production. This fails the
build instead.

To upgrade Procrastinate: bump the pin in pyproject.toml, copy the new schema.sql over
alembic/vendor/, add a migration applying the package's new sql/migrations/*.sql files in
filename order, then update the constants below.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import procrastinate

VENDORED = Path(__file__).resolve().parents[2] / "alembic" / "vendor" / "procrastinate_3.10.sql"

EXPECTED_VERSION = "3.10.0"
EXPECTED_SHA256 = "c70ec4b400a60ad9592787653aae5ae77ae41bf801d56b5bd2712751a07f2009"


def _installed_schema() -> Path:
    return Path(procrastinate.__file__).parent / "sql" / "schema.sql"


def test_installed_version_matches_the_vendored_copy() -> None:
    assert procrastinate.__version__ == EXPECTED_VERSION, (
        "Procrastinate was upgraded but alembic/vendor/ was not re-vendored. "
        "See this module's docstring for the procedure."
    )


def test_vendored_sql_is_byte_identical_to_the_package() -> None:
    assert VENDORED.read_bytes() == _installed_schema().read_bytes()


def test_vendored_sql_matches_the_recorded_digest() -> None:
    digest = hashlib.sha256(VENDORED.read_bytes()).hexdigest()
    assert digest == EXPECTED_SHA256
