from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from urllib.parse import urlparse, urlunparse

import asyncpg

from app.config import db_settings

# Global connection pool
_connection_pool: asyncpg.Pool | None = None


def _normalize_connection_string(server_dsn: str, database: str) -> str:
    """
    Normalize PostgreSQL connection string for asyncpg.

    Converts postgresql:// to postgres:// and properly constructs
    the full connection string with database name.
    """
    # Parse the server DSN
    parsed = urlparse(server_dsn)

    # Normalize scheme: asyncpg prefers postgres:// over postgresql://
    if parsed.scheme == "postgresql":
        scheme = "postgres"
    elif parsed.scheme == "postgres":
        scheme = "postgres"
    else:
        raise ValueError(f"Unsupported database scheme: {parsed.scheme}. Must be 'postgres' or 'postgresql'")

    # Construct the connection string with database name
    # Format: postgres://user:password@host:port/database
    connection_string = urlunparse(
        (
            scheme,
            parsed.netloc,  # user:password@host:port
            f"/{database}",  # database name in path
            parsed.params,
            parsed.query,
            parsed.fragment,
        )
    )

    return connection_string


async def get_connection_pool() -> asyncpg.Pool:
    """Get or create the global connection pool."""
    global _connection_pool
    if _connection_pool is None:
        server_dsn = db_settings.db_url
        database = db_settings.db_name
        connection_string = _normalize_connection_string(server_dsn, database)
        _connection_pool = await asyncpg.create_pool(
            connection_string,
            min_size=1,
            max_size=10,
            command_timeout=60,
        )
    return _connection_pool


async def close_connection_pool() -> None:
    """Close the global connection pool."""
    global _connection_pool
    if _connection_pool is not None:
        await _connection_pool.close()
        _connection_pool = None


@asynccontextmanager
async def database_pool_connection() -> AsyncGenerator[asyncpg.Connection, None]:
    """
    Async context manager that yields a connection from the pool.
    This allows multiple concurrent operations.
    """
    pool = await get_connection_pool()
    async with pool.acquire() as conn:
        yield conn


@asynccontextmanager
async def database_connect(
    server_dsn: str | None = None, database: str | None = None
) -> AsyncGenerator[asyncpg.Connection, None]:
    """
    Async context manager to ensure database exists and yield a live connection.

    - Connects to server DSN without DB, creates DB if missing
    - Connects to target DB and yields a connection
    """
    server_dsn = server_dsn or db_settings.db_url
    database = database or db_settings.db_name

    conn = await asyncpg.connect(f"{server_dsn}/{database}")
    try:
        yield conn
    finally:
        await conn.close()
