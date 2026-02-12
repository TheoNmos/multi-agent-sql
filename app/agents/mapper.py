"""Agent 2: Schema & Context mapper - Finds relevant database schema and gathers domain knowledge."""

from __future__ import annotations

import time

import logfire
from pydantic_ai import Agent, RunContext, UsageLimits

from app.agents.context import AgentState, ToolCall, mapperOutput
from app.agents.tools import (
    get_table_info,
    sample_values,
    search_column_values,
)
from app.llm_models import gpt_5_mini
from app.prompts import DEFAULT_MAPPER_PROMPT, render_prompt
from app.toon_utils import to_toon_block

mapper = Agent[AgentState, mapperOutput](
    name="schema_mapper",
    model=gpt_5_mini,
    deps_type=AgentState,
    output_type=mapperOutput,
)


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

**WARNING**: No tables found in database! This is likely an error. Check that tables were retrieved in the compositor.

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
- **DO NOT** search for tables - they're all listed here!
- **SELECT** 3-5 most relevant tables from this list
- **USE** `get_table_info` to explore your selected tables
- **NO NEED** to search for columns - `get_table_info` shows them all!
- **SAMPLE ROWS**: You have access to sample rows for all tables (see PRE-FETCHED SAMPLE ROWS section above)

{sample_rows_section}
"""

    return {
        "clarified_question": clarified_question,
        "sub_questions_context": sub_questions_context,
        "tables_list_section": tables_list_section,
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

    Returns sample values in TOON format for efficient LLM processing.
    """
    if not ctx.deps.database_connection:
        return to_toon_block([], "values")

    tool_start_time = time.time()
    try:
        result = await sample_values(ctx.deps.database_connection, table_name, column_name, limit)
        tool_timing_ms = int((time.time() - tool_start_time) * 1000)

        # Record tool call in trace (keep trace logging with raw result for accuracy)
        result_preview = f"Retrieved {len(result)} sample values"

        ctx.deps.trace.tools.append(
            ToolCall(
                agent="mapper",
                tool="sample_values",
                args_redacted={"table_name": table_name, "column_name": column_name, "limit": limit},
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
                args_redacted={"table_name": table_name, "column_name": column_name, "limit": limit},
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

    Returns matching values in TOON format for efficient LLM processing.
    """
    if not ctx.deps.database_connection:
        return to_toon_block([], "values")

    tool_start_time = time.time()
    try:
        result = await search_column_values(ctx.deps.database_connection, table_name, column_name, keyword, limit)
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
                    "limit": limit,
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
                    "limit": limit,
                },
                timing_ms=tool_timing_ms,
                error=str(e),
            )
        )
        raise


mapper_USAGE_LIMITS = UsageLimits(
    tool_calls_limit=10,  # Reduced: tables provided upfront, only need to explore selected ones
    input_tokens_limit=50000,
)


@logfire.instrument("mapper_agent")
async def run_mapper(state: AgentState) -> mapperOutput:
    """Run the Schema & Context mapper agent."""
    clarified_question = state.clarified_question or state.raw_question
    logfire.info("Running Schema mapper", clarified_question=clarified_question)

    # Enforce sequential tool calls to prevent asyncpg connection errors
    with mapper.sequential_tool_calls():
        result = await mapper.run(clarified_question, deps=state, usage_limits=mapper_USAGE_LIMITS)
    output = result.output

    logfire.info(
        "Schema mapper completed",
        output_length=len(output),
        output_preview=output[:200] if len(output) > 200 else output,
    )

    return output
