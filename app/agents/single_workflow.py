"""Single agent workflow - runs the monolithic text-to-SQL agent."""

from __future__ import annotations

import re
from typing import Any

from app.agents.llm_timeout import format_model_error
from app.agents.single_agent import run_single_agent
from app.agents.tools import clean_sql
from app.db.connection import database_connect
from app.llm_models import gpt_5_mini


def _extract_sql(text: str | None) -> str | None:
    """Extract SQL from model output (handles markdown code blocks)."""
    if not text or not text.strip():
        return None
    text = text.strip()
    match = re.search(r"```(?:sql)?\s*\n?(.*?)```", text, re.DOTALL | re.IGNORECASE)
    if match:
        return clean_sql(match.group(1).strip())
    if re.search(r"\bSELECT\b", text, re.IGNORECASE):
        return clean_sql(text)
    return clean_sql(text)


async def run_single_agent_pipeline(
    user_message: str,
    server_dsn: str | None = None,
    database: str | None = None,
    execution_id: str | None = None,
) -> tuple[str | None, list[dict[str, Any]], dict[str, Any] | None, str | None]:
    """
    Run the single agent pipeline.

    Returns:
        (sql_query, tool_calls, usage, error)
    """
    from app.config import db_settings
    from app.redis_orm import append_single_agent_tool_call, update_execution_metrics, update_execution_status

    final_server_dsn = server_dsn or db_settings.db_url
    final_database = database or db_settings.db_name

    async def on_tool_call(tool_call: dict[str, Any]) -> None:
        if execution_id:
            await append_single_agent_tool_call(execution_id, tool_call)
            await update_execution_metrics(
                execution_id,
                current_activity=f"Used tool: {tool_call.get('tool', 'unknown')}",
            )

    async def on_usage(usage_event: dict[str, Any]) -> None:
        if execution_id:
            await update_execution_metrics(
                execution_id,
                usage=usage_event.get("usage"),
                current_activity=f"Node: {usage_event.get('node', 'running')}",
            )

    async with database_connect(server_dsn=final_server_dsn, database=final_database) as conn:
        try:
            if execution_id:
                await update_execution_status(execution_id, "running")
                await update_execution_metrics(
                    execution_id,
                    model_name=gpt_5_mini.model_name,
                    current_activity="Starting single-agent run",
                )
            raw_sql, tool_calls, usage = await run_single_agent(
                question=user_message,
                database_connection=conn,
                db_name=final_database,
                execution_id=execution_id,
                on_tool_call=on_tool_call,
                on_usage=on_usage,
            )
            sql = _extract_sql(raw_sql)
            if execution_id:
                await update_execution_metrics(
                    execution_id,
                    usage=usage,
                    current_activity="Single-agent run finished",
                )
            return sql, tool_calls, usage, None
        except Exception as e:
            error_message = format_model_error(e)
            if execution_id:
                await update_execution_status(execution_id, "error", error=error_message)
            return None, [], None, error_message
