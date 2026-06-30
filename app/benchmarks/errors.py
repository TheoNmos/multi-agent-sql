"""Benchmark and pipeline error classification for user-facing messages."""

from __future__ import annotations


def pipeline_result_error(
    *,
    status: str,
    predicted_sql: str,
    error: str | None = None,
    feedback: str | None = None,
) -> str | None:
    """
    Return a user-facing system error only when the pipeline failed or produced no SQL.

    Validator rejection with executable SQL is not a system error.
    """
    sql = predicted_sql.strip()
    if not sql:
        if status == "ERROR":
            return error or "Pipeline failed"
        if status == "REJECTED":
            return error or feedback or "No SQL generated"
        return "No SQL generated"
    if status == "ERROR":
        return error or "Pipeline failed"
    return None


def is_blocking_benchmark_error(
    *,
    error: str | None,
    execution_error: str | None,
    predicted_sql: str,
) -> bool:
    """True when the benchmark case failed to produce or execute SQL."""
    if error:
        return True
    if execution_error and not predicted_sql.strip():
        return True
    if execution_error:
        return True
    return False
