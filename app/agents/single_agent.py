"""Single Agent - One agent with all tools for text-to-SQL. Iterates freely within token limits."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

import asyncpg
import logfire
from pydantic import BaseModel, Field
from pydantic_ai import Agent, RunContext, UsageLimits

from app.agents.tools import (
    execute_sql_safe,
    get_query_plan,
    get_table_info,
    sample_values,
    search_column_values,
    search_tables,
    validate_sql_syntax,
)
from app.llm_models import gpt_5_mini
from app.toon_utils import to_toon_block


class SingleAgentState(BaseModel):
    """State for the single agent. Mutable - tool_calls appended during run."""

    model_config = {"arbitrary_types_allowed": True}

    database_connection: asyncpg.Connection | None = None
    db_name: str | None = None
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    execution_id: str | None = None
    on_tool_call: Callable[[dict[str, Any]], None] | None = None  # Optional callback to persist tool calls


def _record_tool_call(
    state: SingleAgentState, tool_name: str, args: dict, result_preview: str, timing_ms: int, error: str | None = None
):
    """Record a tool call to state and optionally persist via callback."""
    entry = {
        "tool": tool_name,
        "args": args,
        "result_preview": result_preview,
        "timing_ms": timing_ms,
        "error": error,
    }
    state.tool_calls.append(entry)
    if state.on_tool_call:
        try:
            state.on_tool_call(entry)
        except Exception:
            pass


SINGLE_AGENT_PROMPT = """You are a **Text-to-SQL Agent**. Convert natural language questions into correct PostgreSQL SQL queries.

## Your Process

1. **Understand** the question - clarify intent, aggregations, filters
2. **Explore schema** - use get_schema_preview first for full overview, then get_table_info for details on selected tables
3. **Generate SQL** - write a SELECT query
4. **Validate** - use validate_sql to check syntax, execute_sql to test, get_query_plan to check efficiency
5. **Iterate** - if validation or execution fails, fix the SQL and retry

## Tools

- **get_schema_preview**: Get full schema preparation (all tables + sample row per table). Same as multi-agent system. Call this first for a complete overview.
- **list_tables**: Get all table names. Use to discover schema.
- **get_table_info**: Get columns, types, keys, sample row for one or more tables.
- **sample_values**: Get distinct values from a column (for value encodings).
- **search_column_values**: Search for values matching a keyword in a column.
- **search_tables**: Search table names by keywords.
- **validate_sql**: Check if SQL syntax is valid.
- **execute_sql**: Run the SQL and get results. Use to verify correctness.
- **get_query_plan**: Get execution plan for efficiency analysis.

## Guidelines

- Use tools iteratively - explore schema, generate, validate, fix
- If execute_sql fails, analyze the error and fix the query
- Prefer explicit JOINs, qualify columns with table names when ambiguous
- For BIRD-style queries: single SELECT, no CTEs unless necessary
- Output ONLY the final SQL query as your response when done
"""


single_agent = Agent[SingleAgentState, str](
    name="single_text_to_sql",
    model=gpt_5_mini,
    deps_type=SingleAgentState,
    output_type=str,
)

SINGLE_AGENT_USAGE_LIMITS = UsageLimits(
    tool_calls_limit=25,
    input_tokens_limit=100000,
    output_tokens_limit=20000,
)


@single_agent.system_prompt
def system_prompt(ctx: RunContext[SingleAgentState]) -> str:
    return SINGLE_AGENT_PROMPT


@single_agent.tool
async def tool_get_schema_preview(ctx: RunContext[SingleAgentState]) -> str:
    """Get full schema preparation: all table names + one sample row per table (truncated).
    Same information the multi-agent system gets before the mapper. Use this first for a complete database overview."""
    state = ctx.deps
    if not state.database_connection:
        return "No database connection."
    start = time.time()
    try:
        conn = state.database_connection
        table_rows = await conn.fetch(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public' ORDER BY table_name"
        )
        all_tables = [r["table_name"] for r in table_rows]
        sample_rows_dict: dict[str, dict[str, Any] | None] = {}
        for table_name in all_tables:
            try:
                column_rows = await conn.fetch(
                    """
                    SELECT column_name FROM information_schema.columns
                    WHERE table_schema = 'public' AND table_name = $1 ORDER BY ordinal_position
                    """,
                    table_name,
                )
                column_names = [r["column_name"] for r in column_rows]
                if column_names:
                    quoted = ", ".join(f'"{c}"' for c in column_names)
                    rows = await conn.fetch(f'SELECT {quoted} FROM "{table_name}" LIMIT 1')
                    if rows:
                        row = dict(rows[0])
                        truncated = {}
                        for k, v in row.items():
                            if v is None:
                                truncated[k] = None
                            elif isinstance(v, str):
                                truncated[k] = v[:50] if len(v) > 50 else v
                            elif isinstance(v, (int, float, bool)):
                                truncated[k] = v
                            else:
                                s = str(v)
                                truncated[k] = s[:100] if len(s) > 100 else s
                        sample_rows_dict[table_name] = truncated
                    else:
                        sample_rows_dict[table_name] = None
                else:
                    sample_rows_dict[table_name] = None
            except Exception:
                sample_rows_dict[table_name] = None
        result = {
            "all_tables": all_tables,
            "table_count": len(all_tables),
            "sample_rows": sample_rows_dict,
        }
        timing_ms = int((time.time() - start) * 1000)
        preview = f"{len(all_tables)} tables, {sum(1 for v in sample_rows_dict.values() if v is not None)} with samples"
        _record_tool_call(state, "get_schema_preview", {}, preview, timing_ms)
        return to_toon_block(result, "schema_preview")
    except Exception as e:
        timing_ms = int((time.time() - start) * 1000)
        _record_tool_call(state, "get_schema_preview", {}, "", timing_ms, str(e))
        raise


@single_agent.tool
async def tool_list_tables(ctx: RunContext[SingleAgentState]) -> str:
    """List all table names in the database."""
    state = ctx.deps
    if not state.database_connection:
        return "No database connection."
    start = time.time()
    try:
        rows = await state.database_connection.fetch(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public' ORDER BY table_name"
        )
        tables = [r["table_name"] for r in rows]
        timing_ms = int((time.time() - start) * 1000)
        _record_tool_call(state, "list_tables", {}, f"{len(tables)} tables", timing_ms)
        return ", ".join(tables) if tables else "No tables found."
    except Exception as e:
        timing_ms = int((time.time() - start) * 1000)
        _record_tool_call(state, "list_tables", {}, "", timing_ms, str(e))
        raise


@single_agent.tool
async def tool_get_table_info(ctx: RunContext[SingleAgentState], table_names: list[str] | str) -> str:
    """Get detailed info about tables: columns, types, primary keys, foreign keys, sample row."""
    state = ctx.deps
    if not state.database_connection:
        return "No database connection."
    names = [table_names] if isinstance(table_names, str) else table_names
    start = time.time()
    try:
        result = await get_table_info(state.database_connection, table_names)
        timing_ms = int((time.time() - start) * 1000)
        preview = f"{len(result)} tables"
        _record_tool_call(state, "get_table_info", {"tables": names[:5]}, preview, timing_ms)
        return to_toon_block(result, "tables")
    except Exception as e:
        timing_ms = int((time.time() - start) * 1000)
        _record_tool_call(state, "get_table_info", {"tables": names[:5]}, "", timing_ms, str(e))
        raise


@single_agent.tool
async def tool_sample_values(
    ctx: RunContext[SingleAgentState], table_name: str, column_name: str, limit: int = 10
) -> str:
    """Get sample distinct values from a column."""
    state = ctx.deps
    if not state.database_connection:
        return "No database connection."
    start = time.time()
    try:
        result = await sample_values(state.database_connection, table_name, column_name, limit)
        timing_ms = int((time.time() - start) * 1000)
        preview = f"{len(result)} values"
        _record_tool_call(state, "sample_values", {"table": table_name, "column": column_name}, preview, timing_ms)
        return to_toon_block(result, "values")
    except Exception as e:
        timing_ms = int((time.time() - start) * 1000)
        _record_tool_call(state, "sample_values", {"table": table_name, "column": column_name}, "", timing_ms, str(e))
        raise


@single_agent.tool
async def tool_search_column_values(
    ctx: RunContext[SingleAgentState], table_name: str, column_name: str, keyword: str, limit: int = 10
) -> str:
    """Search for values in a column matching a keyword."""
    state = ctx.deps
    if not state.database_connection:
        return "No database connection."
    start = time.time()
    try:
        result = await search_column_values(state.database_connection, table_name, column_name, keyword, limit)
        timing_ms = int((time.time() - start) * 1000)
        preview = f"{len(result)} matches"
        _record_tool_call(
            state,
            "search_column_values",
            {"table": table_name, "column": column_name, "keyword": keyword},
            preview,
            timing_ms,
        )
        return to_toon_block(result, "values")
    except Exception as e:
        timing_ms = int((time.time() - start) * 1000)
        _record_tool_call(
            state,
            "search_column_values",
            {"table": table_name, "column": column_name, "keyword": keyword},
            "",
            timing_ms,
            str(e),
        )
        raise


@single_agent.tool
async def tool_search_tables(ctx: RunContext[SingleAgentState], keywords: list[str] | str) -> str:
    """Search table names by keywords."""
    state = ctx.deps
    if not state.database_connection:
        return "No database connection."
    kws = [keywords] if isinstance(keywords, str) else keywords
    start = time.time()
    try:
        result = await search_tables(state.database_connection, keywords)
        timing_ms = int((time.time() - start) * 1000)
        preview = f"{len(result)} tables"
        _record_tool_call(state, "search_tables", {"keywords": kws[:5]}, preview, timing_ms)
        return ", ".join(result) if result else "No matches."
    except Exception as e:
        timing_ms = int((time.time() - start) * 1000)
        _record_tool_call(state, "search_tables", {"keywords": kws[:5]}, "", timing_ms, str(e))
        raise


@single_agent.tool
async def tool_validate_sql(ctx: RunContext[SingleAgentState], sql: str) -> str:
    """Validate SQL syntax. Returns valid/error message."""
    state = ctx.deps
    if not state.database_connection:
        return "No database connection."
    start = time.time()
    try:
        valid, err = await validate_sql_syntax(state.database_connection, sql)
        timing_ms = int((time.time() - start) * 1000)
        preview = "valid" if valid else f"error: {err[:100]}"
        _record_tool_call(state, "validate_sql", {"sql_len": len(sql)}, preview, timing_ms)
        return f"Valid: {valid}" if valid else f"Syntax error: {err}"
    except Exception as e:
        timing_ms = int((time.time() - start) * 1000)
        _record_tool_call(state, "validate_sql", {"sql_len": len(sql)}, "", timing_ms, str(e))
        raise


@single_agent.tool
async def tool_execute_sql(ctx: RunContext[SingleAgentState], sql: str, limit: int = 20) -> str:
    """Execute SQL and return results. Use to verify the query works."""
    state = ctx.deps
    if not state.database_connection:
        return "No database connection."
    start = time.time()
    try:
        success, results, error = await execute_sql_safe(state.database_connection, sql, limit)
        timing_ms = int((time.time() - start) * 1000)
        if success:
            preview = f"{len(results) if results else 0} rows"
        else:
            preview = f"error: {(error or '')[:100]}"
        _record_tool_call(state, "execute_sql", {"sql_len": len(sql), "limit": limit}, preview, timing_ms)
        if success:
            return to_toon_block(results or [], "results")
        return f"Execution failed: {error}"
    except Exception as e:
        timing_ms = int((time.time() - start) * 1000)
        _record_tool_call(state, "execute_sql", {"sql_len": len(sql), "limit": limit}, "", timing_ms, str(e))
        raise


@single_agent.tool
async def tool_get_query_plan(ctx: RunContext[SingleAgentState], sql: str) -> str:
    """Get query execution plan for efficiency analysis. Pass raw SQL without EXPLAIN."""
    state = ctx.deps
    if not state.database_connection:
        return "No database connection."
    start = time.time()
    try:
        result = await get_query_plan(state.database_connection, sql)
        timing_ms = int((time.time() - start) * 1000)
        preview = str(result)[:80] if result else "N/A"
        _record_tool_call(state, "get_query_plan", {"sql_len": len(sql)}, preview, timing_ms)
        return to_toon_block(result, "plan") if result else "Could not get plan."
    except Exception as e:
        timing_ms = int((time.time() - start) * 1000)
        _record_tool_call(state, "get_query_plan", {"sql_len": len(sql)}, "", timing_ms, str(e))
        raise


@logfire.instrument("single_agent")
async def run_single_agent(
    question: str,
    database_connection: asyncpg.Connection,
    db_name: str,
    execution_id: str | None = None,
    on_tool_call: callable | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    """Run the single agent. Returns (sql_query, tool_calls)."""
    state = SingleAgentState(
        database_connection=database_connection,
        db_name=db_name,
        execution_id=execution_id,
        on_tool_call=on_tool_call,
    )
    result = await single_agent.run(
        f"Convert this question to SQL: {question}",
        deps=state,
        usage_limits=SINGLE_AGENT_USAGE_LIMITS,
    )
    sql = result.output or ""
    return sql, state.tool_calls
