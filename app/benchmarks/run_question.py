"""Pluggable interface for running questions through a text-to-SQL system."""

import time

from app.agents.llm_timeout import format_model_error
from app.benchmarks.errors import pipeline_result_error
from app.config import db_settings

from .types import RunQuestionResult


async def run_question(question: str, db_name: str) -> RunQuestionResult:
    """
    Run a natural language question through a text-to-SQL system.

    Args:
        question: Natural language question to convert to SQL
        db_name: Name of the database to query

    Returns:
        RunQuestionResult with predicted SQL, trace, and metadata
    """
    start_time = time.time()

    try:
        # Always use the new multi-agent pipeline
        from app.agents.workflow import run_new_pipeline

        result = await run_new_pipeline(question, server_dsn=db_settings.db_url, database=db_name)

        latency_ms = int((time.time() - start_time) * 1000)

        # Extract SQL from result
        predicted_sql = result.sql or ""

        error = pipeline_result_error(
            status=result.status,
            predicted_sql=predicted_sql,
            error=result.error,
            feedback=result.feedback,
        )

        # Convert PipelineResult to dict for trace (includes all outputs)
        trace = result.model_dump()

        return RunQuestionResult(
            predicted_sql=predicted_sql,
            latency_ms=latency_ms,
            tokens_in=None,  # TODO: extract from agent run results if available
            tokens_out=None,  # TODO: extract from agent run results if available
            model_name=None,  # TODO: extract from agent run results if available
            error=error,
            trace=trace,
        )

    except Exception as e:
        latency_ms = int((time.time() - start_time) * 1000)
        return RunQuestionResult(
            predicted_sql="",
            latency_ms=latency_ms,
            tokens_in=None,
            tokens_out=None,
            model_name=None,
            error=format_model_error(e),
            trace=None,
        )
