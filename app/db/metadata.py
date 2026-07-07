"""Shared PostgreSQL pool for app metadata: auth, connections, and analytics feedback."""

from __future__ import annotations

import asyncpg
import logfire

from app.config import analytics_settings, auth_settings
from app.db.dialects import normalize_asyncpg_url

_metadata_pool: asyncpg.Pool | None = None

AUTH_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS users (
    id UUID PRIMARY KEY,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS auth_sessions (
    id UUID PRIMARY KEY,
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_hash TEXT NOT NULL UNIQUE,
    expires_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS auth_sessions_token_hash_idx ON auth_sessions (token_hash);
CREATE INDEX IF NOT EXISTS auth_sessions_user_id_idx ON auth_sessions (user_id);

CREATE TABLE IF NOT EXISTS user_connections (
    id UUID PRIMARY KEY,
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    label TEXT,
    encrypted_payload BYTEA NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS user_connections_user_id_idx ON user_connections (user_id);
"""

ANALYTICS_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS execution_feedback (
    execution_id TEXT PRIMARY KEY,
    session_id TEXT,
    connection_id TEXT,
    pipeline_mode TEXT,
    sql_dialect TEXT,
    execution_status TEXT,
    execution_error TEXT,
    execution_latency_ms INTEGER,
    user_query TEXT NOT NULL,
    sql_query TEXT,
    sample_row JSONB,
    analyzer_output JSONB,
    validator_is_valid BOOLEAN,
    validator_efficiency_score NUMERIC,
    user_feedback TEXT NOT NULL CHECK (user_feedback IN ('positive','negative')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS execution_feedback_session_idx
    ON execution_feedback (session_id);

CREATE INDEX IF NOT EXISTS execution_feedback_user_feedback_idx
    ON execution_feedback (user_feedback);

ALTER TABLE execution_feedback
    ADD COLUMN IF NOT EXISTS execution_latency_ms INTEGER;
"""


async def init_metadata_db() -> None:
    """Initialize the shared metadata database connection and all app tables."""
    global _metadata_pool
    if _metadata_pool is not None:
        return

    database_url = analytics_settings.analytics_database_url
    if not database_url:
        if auth_settings.auth_disabled:
            logfire.info("ANALYTICS_DATABASE_URL not configured; metadata DB skipped (auth disabled)")
            return
        logfire.warning("ANALYTICS_DATABASE_URL not configured; auth and feedback persistence disabled")
        return

    try:
        database_url = normalize_asyncpg_url(database_url)
        _metadata_pool = await asyncpg.create_pool(database_url, min_size=1, max_size=5, command_timeout=30)
        async with _metadata_pool.acquire() as conn:
            await conn.execute(AUTH_SCHEMA_SQL)
            await conn.execute(ANALYTICS_SCHEMA_SQL)
        logfire.info("Metadata database initialized (auth + analytics)")
    except Exception as e:
        if _metadata_pool is not None:
            await _metadata_pool.close()
            _metadata_pool = None
        logfire.warning("Could not initialize metadata database", error=str(e))
        if not auth_settings.auth_disabled:
            raise


async def close_metadata_db() -> None:
    """Close the shared metadata connection pool."""
    global _metadata_pool
    if _metadata_pool is not None:
        await _metadata_pool.close()
        _metadata_pool = None


def get_metadata_pool() -> asyncpg.Pool | None:
    return _metadata_pool


def is_metadata_db_ready() -> bool:
    return _metadata_pool is not None
