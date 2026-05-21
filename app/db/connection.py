from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import asyncpg

from app.config import db_settings
from app.db.adapter import DatabaseAdapter
from app.db.dialects import (
    detect_dialect,
    normalize_postgres_url,
    parse_mysql_dsn,
    split_url_and_database,
)
from app.db.postgres_adapter import PostgresAdapter

# Postgres-only connection pool kept for callers that still want pooled access.
_connection_pool: asyncpg.Pool | None = None


async def get_connection_pool() -> asyncpg.Pool:
    """Get or create the global connection pool (PostgreSQL only)."""
    global _connection_pool
    if _connection_pool is None:
        server_dsn = db_settings.db_url
        database = db_settings.db_name
        dialect = detect_dialect(server_dsn)
        if dialect != "postgres":
            raise RuntimeError(
                "get_connection_pool only supports PostgreSQL DSNs; "
                "use database_connect for MySQL connections."
            )
        connection_string = normalize_postgres_url(server_dsn, database)
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
    """Yield a raw asyncpg connection from the pool (PostgreSQL only)."""
    pool = await get_connection_pool()
    async with pool.acquire() as conn:
        yield conn


def _resolve_endpoint(server_dsn: str | None, database: str | None) -> tuple[str, str]:
    """Resolve the server DSN and database name from arguments and config."""
    final_server_dsn = server_dsn or db_settings.db_url
    final_database = database or db_settings.db_name

    if not final_server_dsn:
        raise ValueError("No database server DSN configured")

    embedded_dsn, embedded_db = split_url_and_database(final_server_dsn)
    if embedded_db and not database:
        final_database = embedded_db
    if embedded_dsn:
        final_server_dsn = embedded_dsn

    if not final_database:
        raise ValueError("No database name configured")

    return final_server_dsn, final_database


@asynccontextmanager
async def database_connect(
    server_dsn: str | None = None, database: str | None = None
) -> AsyncGenerator[DatabaseAdapter, None]:
    """Open a database connection and yield a dialect-aware adapter.

    Supports PostgreSQL (``postgres://``, ``postgresql://``) via asyncpg and
    MySQL (``mysql://``, ``mysql+asyncmy://``) via asyncmy.
    """
    final_server_dsn, final_database = _resolve_endpoint(server_dsn, database)
    dialect = detect_dialect(final_server_dsn)

    if dialect == "postgres":
        url = normalize_postgres_url(final_server_dsn, final_database)
        conn = await asyncpg.connect(url)
        adapter: DatabaseAdapter = PostgresAdapter(conn=conn, database_name=final_database)
        try:
            yield adapter
        finally:
            await adapter.close()
        return

    # MySQL path (lazy import to avoid the dependency when only PostgreSQL is used)
    from app.db.mysql_adapter import MySQLAdapter, import_asyncmy

    asyncmy = import_asyncmy()
    kwargs = parse_mysql_dsn(final_server_dsn, final_database)
    conn = await asyncmy.connect(**kwargs)
    adapter = MySQLAdapter(conn=conn, database_name=final_database)
    try:
        yield adapter
    finally:
        await adapter.close()
