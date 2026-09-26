"""pgvector availability -- the spec's Day-1 gate, automated.

The spec says to confirm pgvector on Day 1 and switch images if it is missing. Asserting it
here (and in `po-db bootstrap`) means the failure surfaces at deploy time with a clear
message, rather than in Iteration 11 when the memory store tries to build an HNSW index.
"""

from __future__ import annotations

from typing import Any

import pytest

pytestmark = pytest.mark.db


async def test_pgvector_is_installed(app_conn: Any) -> None:
    version = await app_conn.fetchval(
        "SELECT extversion FROM pg_extension WHERE extname = 'vector'"
    )
    assert version is not None, "pgvector is not installed; see alembic/bootstrap.sql"


async def test_pgvector_supports_hnsw(app_conn: Any) -> None:
    """Iteration 11 needs HNSW, which arrived in pgvector 0.5."""
    version: str = await app_conn.fetchval(
        "SELECT extversion FROM pg_extension WHERE extname = 'vector'"
    )
    assert tuple(int(part) for part in version.split(".")) >= (0, 5, 0)


async def test_an_hnsw_index_actually_builds(owner_conn: Any) -> None:
    """Version numbers are necessary but not sufficient -- build one for real."""
    async with owner_conn.transaction():
        await owner_conn.execute(
            "CREATE TEMP TABLE hnsw_probe (id serial primary key, embedding vector(3))"
        )
        await owner_conn.execute(
            "CREATE INDEX hnsw_probe_idx ON hnsw_probe USING hnsw (embedding vector_cosine_ops)"
        )
