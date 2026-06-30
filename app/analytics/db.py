from __future__ import annotations

import asyncpg
import logfire

from app.config import analytics_settings
from app.db.dialects import normalize_asyncpg_url

_analytics_pool: asyncpg.Pool | None = None


SCHEMA_SQL = """
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


async def init_analytics() -> None:
    """Initialize the external analytics database connection and schema."""
    global _analytics_pool
    if _analytics_pool is not None:
        return

    database_url = analytics_settings.analytics_database_url
    if not database_url:
        logfire.info("Analytics database URL not configured; feedback persistence disabled")
        return

    try:
        database_url = normalize_asyncpg_url(database_url)
        _analytics_pool = await asyncpg.create_pool(database_url, min_size=1, max_size=3, command_timeout=30)
        async with _analytics_pool.acquire() as conn:
            await conn.execute(SCHEMA_SQL)
        logfire.info("Analytics database initialized")
    except Exception as e:
        if _analytics_pool is not None:
            await _analytics_pool.close()
            _analytics_pool = None
        logfire.warning("Could not initialize analytics database; feedback persistence disabled", error=str(e))


async def close_analytics() -> None:
    """Close the analytics connection pool."""
    global _analytics_pool
    if _analytics_pool is not None:
        await _analytics_pool.close()
        _analytics_pool = None


def get_analytics_pool() -> asyncpg.Pool | None:
    """Return the analytics pool if initialized."""
    return _analytics_pool


def is_analytics_ready() -> bool:
    """Whether feedback persistence can currently write to analytics Postgres."""
    return _analytics_pool is not None
