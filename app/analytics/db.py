from __future__ import annotations

import asyncpg

from app.db.metadata import (
    close_metadata_db,
    get_metadata_pool,
    init_metadata_db,
    is_metadata_db_ready,
)


async def init_analytics() -> None:
    """Initialize the analytics tables on the shared metadata database."""
    await init_metadata_db()


async def close_analytics() -> None:
    """Close the shared metadata connection pool."""
    await close_metadata_db()


def get_analytics_pool() -> asyncpg.Pool | None:
    """Return the metadata pool if initialized."""
    return get_metadata_pool()


def is_analytics_ready() -> bool:
    """Whether feedback persistence can currently write to Postgres."""
    return is_metadata_db_ready()
