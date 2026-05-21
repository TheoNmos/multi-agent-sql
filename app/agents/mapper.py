"""Agent 2: Schema & Context mapper - Finds relevant database schema and gathers domain knowledge."""

from __future__ import annotations

import re
import time
from typing import Any

import logfire
from pydantic_ai import Agent, RunContext, UsageLimits

from app.agents.context import AgentState, ToolCall, mapperOutput
from app.agents.telemetry import usage_to_dict
from app.agents.tools import (
    get_table_info,
    sample_values,
    search_column_values,
)
from app.llm_models import mapper_model
from app.prompts import (
    DEFAULT_MAPPER_PROMPT,
    dialect_label,
    dialect_notes,
    format_supervisor_tips,
    render_prompt,
)
from app.toon_utils import to_toon_block

mapper = Agent[AgentState, mapperOutput](
    name="schema_mapper",
    model=mapper_model,
    deps_type=AgentState,
    output_type=mapperOutput,
)


_MAPPER_VALUE_TOOL_CALLS_SEEN_KEY = "mapper_value_tool_calls_seen"
_IDENTIFIER_VALUE_RE = re.compile(r"[+-]?\d+|[0-9a-fA-F-]{32,36}")


def _normalize_tool_arg(value: Any) -> str:
    """Normalize tool arguments so repeated calls are caught even with casing/whitespace changes."""
    return str(value).strip().lower()


def _value_tool_signature(
    tool_name: str, table_name: str, column_name: str, keyword: str | None = None
) -> tuple[str, ...]:
    signature = (tool_name, _normalize_tool_arg(table_name), _normalize_tool_arg(column_name))
    if keyword is not None:
        signature = (*signature, _normalize_tool_arg(keyword))
    return signature


def _has_seen_value_tool_call(scratch: dict[str, Any], signature: tuple[str, ...]) -> bool:
    seen = scratch.setdefault(_MAPPER_VALUE_TOOL_CALLS_SEEN_KEY, set())
    if not isinstance(seen, set):
        seen = set(seen)
        scratch[_MAPPER_VALUE_TOOL_CALLS_SEEN_KEY] = seen
    if signature in seen:
        return True
    seen.add(signature)
    return False


def _repeated_value_tool_response(tool_name: str, signature: tuple[str, ...]) -> str:
    return to_toon_block(
        {
            "skipped": True,
            "reason": (
                f"{tool_name} was already called with the same table/column/value. "
                "Use the previous tool result and return MapperOutput if schema, join, and filter evidence are sufficient."
            ),
            "signature": list(signature),
        },
        "values",
    )


def _record_mapper_tool_skip(state: AgentState, tool_name: str, args_redacted: dict[str, Any], reason: str) -> None:
    state.trace.tools.append(
        ToolCall(
            agent="mapper",
            tool=tool_name,
            args_redacted=args_redacted,
            result_preview=f"Skipped: {reason}",
            timing_ms=0,
        )
    )


def _is_identifier_like_column(column_name: str) -> bool:
    normalized = column_name.strip().lower()
    return normalized == "id" or normalized.endswith("_id")


def _looks_like_identifier_value(keyword: str) -> bool:
    return bool(_IDENTIFIER_VALUE_RE.fullmatch(keyword.strip()))


def _filter_identifier_search_results(column_name: str, keyword: str, values: list[Any]) -> list[Any]:
    """Avoid substring false positives such as 2625 matching 26250 on identifier columns."""
    if not (_is_identifier_like_column(column_name) and _looks_like_identifier_value(keyword)):
        return values
    keyword_text = keyword.strip()
    return [value for value in values if str(value).strip() == keyword_text]


def _build_mapper_template_vars(ctx: RunContext[AgentState]) -> dict[str, str]:
    """Build template variables for the mapper prompt."""
    clarified_question = ctx.deps.clarified_question or ctx.deps.raw_question
    interpreter_output = ctx.deps.interpreter_output
    all_tables = ctx.deps.scratch.get("all_tables", [])
    sample_rows = ctx.deps.scratch.get("sample_rows", {})

    sub_questions_context = ""
    if interpreter_output and interpreter_output.sub_questions:
        sub_questions_context = "\nSub-questions to consider:\n" + "\n".join(
            f"- {q}" for q in interpreter_output.sub_questions
        )

    if not all_tables:
        tables_list_section = """
## 📋 TABLES LIST: ALL AVAILABLE TABLES IN THE DATABASE

**WARNING**: No tables found in database! This is likely an error. Check that tables were retrieved in the pipeline.

"""
    else:
        if len(all_tables) <= 150:
            tables_display = ", ".join(all_tables)
        else:
            tables_to_show = all_tables[:150]
            tables_display = ", ".join(tables_to_show)
            tables_display += f"\n\n... and {len(all_tables) - 150} more tables (total: {len(all_tables)} tables)"

        sample_rows_section = ""
        if sample_rows:
            tables_with_samples = [t for t in all_tables if t in sample_rows and sample_rows[t] is not None]
            if tables_with_samples:
                sample_rows_text = []
                for table_name in tables_with_samples:
                    row_data = sample_rows[table_name]
                    if row_data:
                        row_str = ", ".join(f"{k}: {repr(v)}" for k, v in list(row_data.items())[:10])
                        if len(row_data) > 10:
                            row_str += f" ... ({len(row_data)} total columns)"
                        sample_rows_text.append(f"- **{table_name}**: {{ {row_str} }}")

                if sample_rows_text:
                    sample_rows_section = f"""
## 📊 PRE-FETCHED SAMPLE ROWS

**IMPORTANT**: Sample rows have already been fetched for all tables in the database. These show actual data values to help you understand column contents and formats. Text values are truncated to 100 characters for efficiency.

**Sample Rows Available:**
{chr(10).join(sample_rows_text)}

**Note**: These sample rows are already available in the context. When you call `get_table_info` for these tables, the sample_row will be included automatically. However, you can reference these pre-fetched samples to quickly understand data formats without needing to call tools.

"""

        tables_list_section = f"""
## 📋 TABLES LIST: ALL AVAILABLE TABLES IN THE DATABASE

**Tables List**: Here are **ALL {len(all_tables)} tables** available in this database:

{tables_display}

**CRITICAL**: You have been given the COMPLETE list of ALL tables above.
- **DO NOT** search for tables - they're all listed here.
- **SELECT** the smallest sufficient set of tables, usually 1-4.
- **FIRST USE** the pre-fetched sample rows to infer likely columns, formats, and values.
- **CALL** `get_table_info` at most once, only for final candidate tables that need column/key confirmation.
- **PREFER VALUE TOOLS** (`sample_values`, `search_column_values`) when exact filter values or encodings affect correctness.

{sample_rows_section}
"""

    return {
        "clarified_question": clarified_question,
        "sub_questions_context": sub_questions_context,
        "tables_list_section": tables_list_section,
        "supervisor_tips": format_supervisor_tips(ctx.deps.supervisor_tips.get("mapper")),
        "sql_dialect_label": dialect_label(ctx.deps.sql_dialect),
        "sql_dialect_notes": dialect_notes(ctx.deps.sql_dialect),
    }


@mapper.system_prompt
def system_prompt(ctx: RunContext[AgentState]) -> str:
    logfire.debug(
        "mapper prompt generation",
        table_count=len(ctx.deps.scratch.get("all_tables", [])),
        scratch_keys=list(ctx.deps.scratch.keys()),
    )
    template_vars = _build_mapper_template_vars(ctx)
    custom = (ctx.deps.custom_prompts or {}).get("mapper")
    template = custom if custom else DEFAULT_MAPPER_PROMPT
    return render_prompt(template, template_vars)


@mapper.tool
async def tool_get_table_info(ctx: RunContext[AgentState], table_names: list[str] | str) -> str:
    """Get detailed information about one or more tables including columns, primary keys, foreign keys, and sample rows.

    Returns table information in TOON format for efficient LLM processing.
    """
    if not ctx.deps.database_connection:
        return to_toon_block({}, "tables")

    # Normalize to list for tracing
    if isinstance(table_names, str):
        table_list = [table_names]
    else:
        table_list = table_names

    if ctx.deps.scratch.get("mapper_get_table_info_called"):
        return to_toon_block(
            {
                "skipped": True,
                "reason": "get_table_info may only be called once; use existing context and value tools now.",
            },
            "tables",
        )

    table_list = table_list[:6]
    table_names = table_list
    ctx.deps.scratch["mapper_get_table_info_called"] = True

    # Record tool call start
    tool_start_time = time.time()

    try:
        result = await get_table_info(ctx.deps.database_connection, table_names)
        tool_timing_ms = int((time.time() - tool_start_time) * 1000)

        # Record selected tables
        for table in table_list:
            if table not in ctx.deps.trace.mapper.selected_tables:
                ctx.deps.trace.mapper.selected_tables.append(table)

        # Record tool call (keep trace logging with raw result for accuracy)
        result_preview = f"Retrieved info for {len(result)} tables"

        ctx.deps.trace.tools.append(
            ToolCall(
                agent="mapper",
                tool="get_table_info",
                args_redacted=table_list[:10],  # Limit to first 10 tables
                result_preview=result_preview,
                timing_ms=tool_timing_ms,
            )
        )

        # Return TOON-encoded result for LLM
        return to_toon_block(result, "tables")
    except Exception as e:
        tool_timing_ms = int((time.time() - tool_start_time) * 1000)
        ctx.deps.trace.tools.append(
            ToolCall(
                agent="mapper",
                tool="get_table_info",
                args_redacted=table_list[:10],
                timing_ms=tool_timing_ms,
                error=str(e),
            )
        )
        raise


@mapper.tool
async def tool_sample_values(ctx: RunContext[AgentState], table_name: str, column_name: str, limit: int = 10) -> str:
    """Get sample distinct values from a column to understand value encodings.

    Do not call this repeatedly for the same table/column. If a concrete filter value has already been confirmed,
    return MapperOutput instead of sampling again.

    Returns sample values in TOON format for efficient LLM processing.
    """
    if not ctx.deps.database_connection:
        return to_toon_block([], "values")

    signature = _value_tool_signature("sample_values", table_name, column_name)
    capped_limit = min(max(limit, 1), 10)
    if _has_seen_value_tool_call(ctx.deps.scratch, signature):
        args = {"table_name": table_name, "column_name": column_name, "limit": capped_limit}
        _record_mapper_tool_skip(ctx.deps, "sample_values", args, "repeated table/column sample")
        return _repeated_value_tool_response("sample_values", signature)

    tool_start_time = time.time()
    try:
        result = await sample_values(ctx.deps.database_connection, table_name, column_name, capped_limit)
        tool_timing_ms = int((time.time() - tool_start_time) * 1000)

        # Record tool call in trace (keep trace logging with raw result for accuracy)
        result_preview = f"Retrieved {len(result)} sample values"

        ctx.deps.trace.tools.append(
            ToolCall(
                agent="mapper",
                tool="sample_values",
                args_redacted={"table_name": table_name, "column_name": column_name, "limit": capped_limit},
                result_preview=result_preview,
                timing_ms=tool_timing_ms,
            )
        )

        # Return TOON-encoded result for LLM
        return to_toon_block(result, "values")
    except Exception as e:
        tool_timing_ms = int((time.time() - tool_start_time) * 1000)
        ctx.deps.trace.tools.append(
            ToolCall(
                agent="mapper",
                tool="sample_values",
                args_redacted={"table_name": table_name, "column_name": column_name, "limit": min(max(limit, 1), 10)},
                timing_ms=tool_timing_ms,
                error=str(e),
            )
        )
        raise


@mapper.tool
async def tool_search_column_values(
    ctx: RunContext[AgentState], table_name: str, column_name: str, keyword: str, limit: int = 10
) -> str:
    """Search for specific values in a column using LIKE pattern matching to find exact matches.

    This is for confirming human names, category labels, codes, or enum values. Avoid ID-chasing: if a name is
    confirmed in a dimension table and a join key exists, return the name filter and join instead of resolving an ID.
    For identifier-like columns, numeric keywords are filtered to exact matches to avoid substring false positives.

    Returns matching values in TOON format for efficient LLM processing.
    """
    if not ctx.deps.database_connection:
        return to_toon_block([], "values")

    signature = _value_tool_signature("search_column_values", table_name, column_name, keyword)
    capped_limit = min(max(limit, 1), 10)
    if _has_seen_value_tool_call(ctx.deps.scratch, signature):
        args = {"table_name": table_name, "column_name": column_name, "keyword": keyword, "limit": capped_limit}
        _record_mapper_tool_skip(ctx.deps, "search_column_values", args, "repeated table/column/keyword search")
        return _repeated_value_tool_response("search_column_values", signature)

    tool_start_time = time.time()
    try:
        result = await search_column_values(
            ctx.deps.database_connection, table_name, column_name, keyword, capped_limit
        )
        result = _filter_identifier_search_results(column_name, keyword, result)
        tool_timing_ms = int((time.time() - tool_start_time) * 1000)

        # Record tool call in trace (keep trace logging with raw result for accuracy)
        result_preview = f"Found {len(result)} matching values"

        ctx.deps.trace.tools.append(
            ToolCall(
                agent="mapper",
                tool="search_column_values",
                args_redacted={
                    "table_name": table_name,
                    "column_name": column_name,
                    "keyword": keyword,
                    "limit": capped_limit,
                },
                result_preview=result_preview,
                timing_ms=tool_timing_ms,
            )
        )

        # Return TOON-encoded result for LLM
        return to_toon_block(result, "values")
    except Exception as e:
        tool_timing_ms = int((time.time() - tool_start_time) * 1000)
        ctx.deps.trace.tools.append(
            ToolCall(
                agent="mapper",
                tool="search_column_values",
                args_redacted={
                    "table_name": table_name,
                    "column_name": column_name,
                    "keyword": keyword,
                    "limit": min(max(limit, 1), 10),
                },
                timing_ms=tool_timing_ms,
                error=str(e),
            )
        )
        raise


mapper_USAGE_LIMITS = UsageLimits(
    tool_calls_limit=12,  # One schema confirmation plus a few non-repeated value checks.
    input_tokens_limit=100000,
)


@logfire.instrument("mapper_agent")
async def run_mapper(state: AgentState) -> tuple[mapperOutput, dict[str, Any]]:
    """Run the Schema & Context mapper agent."""
    clarified_question = state.clarified_question or state.raw_question
    logfire.info("Running Schema mapper", clarified_question=clarified_question)
    state.scratch.pop("mapper_get_table_info_called", None)
    state.scratch.pop(_MAPPER_VALUE_TOOL_CALLS_SEEN_KEY, None)

    # Enforce sequential tool calls to prevent async database connection errors.
    with mapper.sequential_tool_calls():  # pyright: ignore[reportDeprecated]
        result = await mapper.run(clarified_question, deps=state, usage_limits=mapper_USAGE_LIMITS)
    output = result.output

    for table in output.selected_tables:
        if table.table_name not in state.trace.mapper.selected_tables:
            state.trace.mapper.selected_tables.append(table.table_name)

    logfire.info(
        "Schema mapper completed",
        selected_table_count=len(output.selected_tables),
        column_count=len(output.columns),
        confidence=output.confidence,
    )

    return output, usage_to_dict(result.usage())
