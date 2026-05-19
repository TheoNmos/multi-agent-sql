from __future__ import annotations

import json
from decimal import ROUND_DOWN, Decimal, InvalidOperation
from typing import Any, Literal

from app.analytics.db import get_analytics_pool
from app.redis_orm import QueryExecution

FeedbackValue = Literal["positive", "negative"]
ArchitecturePreference = Literal["pipeline", "single"]


def _json_dump(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, default=str)


def _validator_efficiency_score(validator_output: dict[str, Any] | None) -> Decimal | None:
    if not validator_output:
        return None
    score = validator_output.get("efficiency_score")
    if score is None:
        return None
    try:
        decimal_score = Decimal(str(score))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return decimal_score.quantize(Decimal("0.01"), rounding=ROUND_DOWN)


def extract_feedback_snapshot(execution: QueryExecution, value: FeedbackValue) -> dict[str, Any]:
    """Build the analytics row from a terminal QueryExecution snapshot."""
    sample_row = execution.query_result[0] if execution.query_result else None
    validator_output = execution.validator_output or {}

    return {
        "execution_id": execution.id,
        "session_id": execution.session_id,
        "connection_id": execution.connection_id,
        "pipeline_mode": execution.pipeline_mode,
        "sql_dialect": None,
        "execution_status": execution.status,
        "execution_error": execution.error,
        "execution_latency_ms": execution.latency_ms,
        "user_query": execution.user_query,
        "sql_query": execution.sql_query,
        "sample_row": sample_row,
        "analyzer_output": execution.analyzer_output,
        "validator_is_valid": validator_output.get("is_valid") if validator_output else None,
        "validator_efficiency_score": _validator_efficiency_score(validator_output),
        "user_feedback": value,
    }


async def save_feedback(snapshot: dict[str, Any]) -> None:
    """Persist feedback for one execution without changing existing votes."""
    pool = get_analytics_pool()
    if pool is None:
        raise RuntimeError("Analytics database is not configured")

    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO execution_feedback (
                execution_id,
                session_id,
                connection_id,
                pipeline_mode,
                sql_dialect,
                execution_status,
                execution_error,
                execution_latency_ms,
                user_query,
                sql_query,
                sample_row,
                analyzer_output,
                validator_is_valid,
                validator_efficiency_score,
                user_feedback
            )
            VALUES (
                $1, $2, $3, $4, $5, $6, $7, $8, $9, $10,
                $11::jsonb, $12::jsonb, $13, $14, $15
            )
            ON CONFLICT (execution_id) DO NOTHING
            """,
            snapshot["execution_id"],
            snapshot.get("session_id"),
            snapshot.get("connection_id"),
            snapshot.get("pipeline_mode"),
            snapshot.get("sql_dialect"),
            snapshot.get("execution_status"),
            snapshot.get("execution_error"),
            snapshot.get("execution_latency_ms"),
            snapshot["user_query"],
            snapshot.get("sql_query"),
            _json_dump(snapshot.get("sample_row")),
            _json_dump(snapshot.get("analyzer_output")),
            snapshot.get("validator_is_valid"),
            snapshot.get("validator_efficiency_score"),
            snapshot["user_feedback"],
        )


async def get_feedback(execution_id: str) -> FeedbackValue | None:
    """Return the current feedback vote for an execution, if any."""
    pool = get_analytics_pool()
    if pool is None:
        return None

    async with pool.acquire() as conn:
        value = await conn.fetchval(
            "SELECT user_feedback FROM execution_feedback WHERE execution_id = $1",
            execution_id,
        )

    if value in ("positive", "negative"):
        return value
    return None


async def get_versus_feedback(
    pipeline_execution_id: str, single_execution_id: str
) -> ArchitecturePreference | None:
    """Return which versus architecture was preferred, if feedback exists."""
    pool = get_analytics_pool()
    if pool is None:
        return None

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT execution_id, user_feedback
            FROM execution_feedback
            WHERE execution_id = ANY($1::text[])
            """,
            [pipeline_execution_id, single_execution_id],
        )

    values = {row["execution_id"]: row["user_feedback"] for row in rows}
    if (
        values.get(pipeline_execution_id) == "positive"
        and values.get(single_execution_id) == "negative"
    ):
        return "pipeline"
    if (
        values.get(single_execution_id) == "positive"
        and values.get(pipeline_execution_id) == "negative"
    ):
        return "single"
    return None
