"""Agent 4: Validator & Refiner - Validates SQL correctness, efficiency, and provides refinement feedback."""

from __future__ import annotations

from typing import Any

import logfire
from pydantic_ai import Agent, RunContext, UsageLimits

from app.agents.context import AgentState, ValidatorOutput
from app.agents.context import ToolCall
from app.agents.telemetry import usage_to_dict
from app.agents.tools import clean_sql, execute_sql_safe, get_query_plan
from app.llm_models import gpt_5_mini
from app.prompts import DEFAULT_VALIDATOR_PROMPT, format_supervisor_tips, render_prompt

validator = Agent[AgentState, ValidatorOutput](
    name="sql_validator",
    model=gpt_5_mini,
    deps_type=AgentState,
    output_type=ValidatorOutput,
)


def _build_validator_template_vars(ctx: RunContext[AgentState]) -> dict[str, str]:
    """Build template variables for the validator prompt."""
    state = ctx.deps
    sql_query = state.current_sql or "No SQL query provided"
    original_question = state.raw_question
    clarified_question = state.clarified_question or state.raw_question
    db_name = state.trace.db_name if state.trace else None

    if state.syntax_valid is not None:
        if state.syntax_valid:
            syntax_status = "✅ Syntax is VALID (pre-validated)"
        else:
            syntax_status = f"❌ Syntax is INVALID (pre-validated): {state.syntax_error or 'Unknown error'}"
    else:
        syntax_status = "⚠️ Syntax not yet validated"

    dataset_context = ""
    if db_name:
        dataset_context = f"\n**Database**: {db_name}"

    return {
        "original_question": original_question,
        "clarified_question": clarified_question,
        "db_name": db_name or "Unknown",
        "dataset_context": dataset_context,
        "sql_query": sql_query,
        "syntax_status": syntax_status,
        "supervisor_tips": format_supervisor_tips(ctx.deps.supervisor_tips.get("validator")),
    }


@validator.system_prompt
def system_prompt(ctx: RunContext[AgentState]) -> str:
    template_vars = _build_validator_template_vars(ctx)
    custom = (ctx.deps.custom_prompts or {}).get("validator")
    template = custom if custom else DEFAULT_VALIDATOR_PROMPT
    return render_prompt(template, template_vars)


@validator.tool
async def tool_get_syntax_status(ctx: RunContext[AgentState]) -> dict[str, Any]:
    """Get pre-validated SQL syntax status from the pipeline."""
    state = ctx.deps
    return {
        "syntax_valid": state.syntax_valid,
        "syntax_error": state.syntax_error,
        "note": "Syntax validation was performed by the pipeline before calling this agent",
    }


@validator.tool
async def tool_get_query_plan(ctx: RunContext[AgentState], sql: str) -> dict[str, Any] | None:
    """Get query execution plan using EXPLAIN ANALYZE to assess query efficiency.

    IMPORTANT: Pass the raw SQL query WITHOUT any EXPLAIN prefix. The tool will add EXPLAIN automatically.
    Example: Pass 'SELECT * FROM table' NOT 'EXPLAIN ANALYZE SELECT * FROM table'
    """
    if not ctx.deps.database_connection:
        return None

    sql_clean = clean_sql(sql)
    try:
        result = await get_query_plan(ctx.deps.database_connection, sql_clean)
        ctx.deps.trace.tools.append(
            ToolCall(
                agent="validator",
                tool="get_query_plan",
                args_redacted={"sql_length": len(sql_clean)},
                result_preview=str(result)[:120] if result else "No plan available",
                timing_ms=0,
            )
        )
        return result
    except Exception as e:
        ctx.deps.trace.tools.append(
            ToolCall(
                agent="validator",
                tool="get_query_plan",
                args_redacted={"sql_length": len(sql_clean)},
                timing_ms=0,
                error=str(e),
            )
        )
        raise


@validator.tool
async def tool_execute_sql_safe(ctx: RunContext[AgentState], sql: str, limit: int = 10) -> dict[str, Any]:
    """Execute SQL query safely with row limit to verify semantic correctness."""
    if not ctx.deps.database_connection:
        return {"success": False, "error": "No database connection", "results": None}

    sql_clean = clean_sql(sql)
    try:
        success, results, error = await execute_sql_safe(ctx.deps.database_connection, sql_clean, limit)
        ctx.deps.trace.tools.append(
            ToolCall(
                agent="validator",
                tool="execute_sql_safe",
                args_redacted={"sql_length": len(sql_clean), "limit": limit},
                result_preview=f"{len(results) if results else 0} rows" if success else (error or "Execution failed")[:120],
                timing_ms=0,
                error=error,
            )
        )
        return {
            "success": success,
            "results": results,
            "error": error,
            "row_count": len(results) if results else 0,
        }
    except Exception as e:
        ctx.deps.trace.tools.append(
            ToolCall(
                agent="validator",
                tool="execute_sql_safe",
                args_redacted={"sql_length": len(sql_clean), "limit": limit},
                timing_ms=0,
                error=str(e),
            )
        )
        raise


VALIDATOR_USAGE_LIMITS = UsageLimits(
    tool_calls_limit=3,  # execute_sql_safe (required) + get_query_plan (required for performance analysis)
    input_tokens_limit=100000,
)


@logfire.instrument("validator_agent")
async def run_validator(state: AgentState) -> tuple[ValidatorOutput, dict[str, Any]]:
    """Run the Validator & Refiner agent."""
    sql_query = state.current_sql
    if not sql_query:
        logfire.warning("Validator called with no SQL query")
        return ValidatorOutput(
            is_valid=False,
            is_optimal=False,
            syntax_errors=["Nenhuma consulta SQL fornecida"],
            refinement_feedback="Nenhuma consulta SQL fornecida para validação.",
        ), usage_to_dict(None)

    # If syntax is invalid, return early without calling tools
    if state.syntax_valid is False:
        logfire.info("SQL syntax invalid, skipping tool calls", syntax_error=state.syntax_error)
        return ValidatorOutput(
            is_valid=False,
            is_optimal=False,
            syntax_errors=[state.syntax_error] if state.syntax_error else ["Erro ao validar a sintaxe"],
            refinement_feedback=f"Erro de sintaxe: {state.syntax_error or 'Erro desconhecido'}.",
        ), usage_to_dict(None)

    logfire.info("Running SQL Validator", sql_length=len(sql_query), attempt=state.attempt_count + 1)

    # Enforce sequential tool calls to prevent asyncpg connection errors
    with validator.sequential_tool_calls():
        result = await validator.run(
            f"Validate this SQL query:\n\n{sql_query}", deps=state, usage_limits=VALIDATOR_USAGE_LIMITS
        )
    output = result.output

    # Include syntax errors from pre-validation if any
    if state.syntax_error:
        output.syntax_errors = [state.syntax_error]

    logfire.info(
        "SQL Validator completed",
        is_valid=output.is_valid,
        is_optimal=output.is_optimal,
        efficiency_score=output.efficiency_score,
        syntax_error_count=len(output.syntax_errors),
        semantic_issue_count=len(output.semantic_issues),
    )

    return output, usage_to_dict(result.usage())
