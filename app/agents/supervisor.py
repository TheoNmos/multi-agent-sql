"""Supervisor Agent - Orchestrates the multi-agent text-to-SQL pipeline via worker tools."""

from __future__ import annotations

import time
from datetime import UTC, datetime

import logfire
from pydantic_ai import Agent, RunContext, UsageLimits

from app.agents.context import (
    AgentState,
    GeneratorTrace,
    StepInfo,
    SupervisorOutput,
)
from app.agents.generator import run_generator
from app.agents.interpreter import run_interpreter
from app.agents.mapper import run_mapper
from app.agents.tools import clean_sql, execute_sql_safe, validate_sql_syntax
from app.agents.validator import run_validator
from app.llm_models import gpt_5_mini
from app.prompts import DEFAULT_SUPERVISOR_PROMPT, render_prompt

supervisor = Agent[AgentState, SupervisorOutput](
    name="supervisor",
    model=gpt_5_mini,
    deps_type=AgentState,
    output_type=SupervisorOutput,
)

SUPERVISOR_USAGE_LIMITS = UsageLimits(tool_calls_limit=20, input_tokens_limit=100000)


@supervisor.system_prompt
def system_prompt(ctx: RunContext[AgentState]) -> str:
    custom = (ctx.deps.custom_prompts or {}).get("supervisor")
    template = custom if custom else DEFAULT_SUPERVISOR_PROMPT
    return render_prompt(template, {})


def _get_redis_helpers(state: AgentState):
    """Extract Redis update functions from state scratch if available."""
    execution_id = state.scratch.get("execution_id")
    update_step = state.scratch.get("update_step")
    update_status = state.scratch.get("update_status")
    return execution_id, update_step, update_status


async def _run_interpreter_tool(ctx: RunContext[AgentState], tips: str | None) -> str:
    """Run the interpreter worker. Returns summary for supervisor."""
    state = ctx.deps
    execution_id, update_step, update_status = _get_redis_helpers(state)

    if tips and tips.strip():
        state.supervisor_tips["interpreter"] = tips.strip()
    else:
        state.supervisor_tips.pop("interpreter", None)

    if update_step and execution_id:
        await update_step(execution_id, "interpreter", "running")

    step_start = time.time()
    try:
        output = await run_interpreter(state)
        state.interpreter_output = output
        state.clarified_question = output.clarified_question
        timing_ms = int((time.time() - step_start) * 1000)

        if update_step and execution_id:
            await update_step(execution_id, "interpreter", "done", output=output.model_dump())

        state.trace.steps.append(
            StepInfo(
                name="interpreter",
                timing_ms=timing_ms,
                input_summary={"raw_question": state.raw_question[:200]},
                output_summary={
                    "clarified_question": output.clarified_question[:200],
                    "intent": output.explicit_intent[:300] if output.explicit_intent else None,
                    "sub_questions_count": len(output.sub_questions),
                },
            )
        )

        return f"Interpreter done. Clarified: {output.clarified_question[:150]}... Intent: {output.explicit_intent[:150]}..."
    except Exception as e:
        timing_ms = int((time.time() - step_start) * 1000)
        state.trace.steps.append(
            StepInfo(
                name="interpreter",
                timing_ms=timing_ms,
                input_summary={"raw_question": state.raw_question[:200]},
                error=str(e),
            )
        )
        if update_step and execution_id:
            await update_step(execution_id, "interpreter", "error")
        if update_status and execution_id:
            await update_status(execution_id, "error", error=f"Interpreter failed: {str(e)}")
        raise


async def _run_mapper_tool(ctx: RunContext[AgentState], tips: str | None) -> str:
    """Run the mapper worker. Returns summary for supervisor."""
    state = ctx.deps
    execution_id, update_step, update_status = _get_redis_helpers(state)

    if tips and tips.strip():
        state.supervisor_tips["mapper"] = tips.strip()
    else:
        state.supervisor_tips.pop("mapper", None)

    if update_step and execution_id:
        await update_step(execution_id, "mapper", "running")

    step_start = time.time()
    try:
        output = await run_mapper(state)
        state.mapper_output = output
        timing_ms = int((time.time() - step_start) * 1000)

        if update_step and execution_id:
            await update_step(execution_id, "mapper", "done", output=output)

        all_tables = state.scratch.get("all_tables", [])
        state.trace.steps.append(
            StepInfo(
                name="mapper",
                timing_ms=timing_ms,
                input_summary={
                    "clarified_question": (state.clarified_question or "")[:200],
                    "all_tables_count": len(all_tables),
                },
                output_summary={
                    "output_length": len(output),
                    "output_preview": output[:300] if len(output) > 300 else output,
                },
            )
        )

        return f"Mapper done. Schema context length: {len(output)} chars. Preview: {output[:200]}..."
    except Exception as e:
        timing_ms = int((time.time() - step_start) * 1000)
        all_tables = state.scratch.get("all_tables", [])
        state.trace.steps.append(
            StepInfo(
                name="mapper",
                timing_ms=timing_ms,
                input_summary={
                    "clarified_question": (state.clarified_question or "")[:200],
                    "all_tables_count": len(all_tables),
                },
                error=str(e),
            )
        )
        if update_step and execution_id:
            await update_step(execution_id, "mapper", "error")
        if update_status and execution_id:
            await update_status(execution_id, "error", error=f"Mapper failed: {str(e)}")
        raise


async def _run_generator_tool(ctx: RunContext[AgentState], tips: str | None) -> str:
    """Run the generator worker. Returns summary for supervisor."""
    state = ctx.deps
    execution_id, update_step, update_status = _get_redis_helpers(state)

    # Set attempt_count for generator's iteration context (0-indexed)
    state.attempt_count = len(state.sql_history)

    if tips and tips.strip():
        state.supervisor_tips["generator"] = tips.strip()
    else:
        state.supervisor_tips.pop("generator", None)

    if update_step and execution_id:
        await update_step(execution_id, "generator", "running")

    step_start = time.time()
    try:
        output = await run_generator(state)
        state.generator_output = output
        cleaned_sql = clean_sql(output.sql_query)
        state.current_sql = cleaned_sql
        state.sql_history.append(cleaned_sql)
        timing_ms = int((time.time() - step_start) * 1000)

        if update_status and execution_id and cleaned_sql:
            await update_status(execution_id, "running", sql_query=cleaned_sql)
        if update_step and execution_id:
            await update_step(execution_id, "generator", "done", output=output.model_dump())

        state.trace.steps.append(
            StepInfo(
                name="generator",
                timing_ms=timing_ms,
                input_summary={
                    "clarified_question": (state.clarified_question or "")[:200],
                    "attempt": state.attempt_count + 1,
                },
                output_summary={
                    "sql_length": len(output.sql_query),
                    "reasoning_steps_count": len(output.reasoning_steps),
                    "confidence": output.confidence,
                },
            )
        )
        state.trace.generator = GeneratorTrace(
            sql_query=cleaned_sql,
            reasoning_steps=output.reasoning_steps,
            confidence=output.confidence,
        )

        if state.database_connection and cleaned_sql:
            syntax_valid, syntax_error = await validate_sql_syntax(state.database_connection, cleaned_sql)
            state.syntax_valid = syntax_valid
            state.syntax_error = syntax_error

        return f"Generator done. SQL length: {len(cleaned_sql)}. Confidence: {output.confidence}. Syntax valid: {state.syntax_valid}"
    except Exception as e:
        timing_ms = int((time.time() - step_start) * 1000)
        state.trace.steps.append(
            StepInfo(
                name="generator",
                timing_ms=timing_ms,
                input_summary={
                    "clarified_question": (state.clarified_question or "")[:200],
                    "attempt": state.attempt_count + 1,
                },
                error=str(e),
            )
        )
        if update_step and execution_id:
            await update_step(execution_id, "generator", "error")
        if update_status and execution_id:
            await update_status(execution_id, "error", error=f"Generator failed: {str(e)}")
        raise


async def _run_validator_tool(ctx: RunContext[AgentState], tips: str | None) -> str:
    """Run the validator worker. Returns summary for supervisor."""
    state = ctx.deps
    execution_id, update_step, update_status = _get_redis_helpers(state)

    if tips and tips.strip():
        state.supervisor_tips["validator"] = tips.strip()
    else:
        state.supervisor_tips.pop("validator", None)

    if update_step and execution_id:
        await update_step(execution_id, "validator", "running")

    step_start = time.time()
    try:
        output = await run_validator(state)
        state.validator_output = output
        timing_ms = int((time.time() - step_start) * 1000)

        if update_step and execution_id:
            await update_step(execution_id, "validator", "done", output=output.model_dump())

        state.trace.steps.append(
            StepInfo(
                name="validator",
                timing_ms=timing_ms,
                input_summary={
                    "sql_length": len(state.current_sql or ""),
                    "attempt": state.attempt_count + 1,
                },
                output_summary={
                    "is_valid": output.is_valid,
                    "is_optimal": output.is_optimal,
                    "efficiency_score": output.efficiency_score,
                    "syntax_error_count": len(output.syntax_errors),
                    "semantic_issue_count": len(output.semantic_issues),
                },
            )
        )

        if output.is_valid:
            state.best_sql = state.current_sql
            state.best_validator_output = output

        return f"Validator done. is_valid={output.is_valid}, is_optimal={output.is_optimal}, efficiency={output.efficiency_score}. Feedback: {output.refinement_feedback[:200]}..."
    except Exception as e:
        timing_ms = int((time.time() - step_start) * 1000)
        state.trace.steps.append(
            StepInfo(
                name="validator",
                timing_ms=timing_ms,
                input_summary={
                    "sql_length": len(state.current_sql or ""),
                    "attempt": state.attempt_count + 1,
                },
                error=str(e),
            )
        )
        if update_step and execution_id:
            await update_step(execution_id, "validator", "error")
        if update_status and execution_id:
            await update_status(execution_id, "error", error=f"Validator failed: {str(e)}")
        raise


@supervisor.tool
async def tool_run_interpreter(ctx: RunContext[AgentState], tips: str | None = None) -> str:
    """Run the Query Interpreter to clarify the user question and extract intent. Call this first."""
    return await _run_interpreter_tool(ctx, tips)


@supervisor.tool
async def tool_run_mapper(ctx: RunContext[AgentState], tips: str | None = None) -> str:
    """Run the Schema Mapper to select relevant tables and build schema context. Call after interpreter."""
    return await _run_mapper_tool(ctx, tips)


@supervisor.tool
async def tool_run_generator(ctx: RunContext[AgentState], tips: str | None = None) -> str:
    """Run the SQL Generator to produce a SQL query. Call after mapper. On retry, receives validator feedback from state."""
    return await _run_generator_tool(ctx, tips)


@supervisor.tool
async def tool_run_validator(ctx: RunContext[AgentState], tips: str | None = None) -> str:
    """Run the SQL Validator to check correctness and efficiency. Call after generator. Returns is_valid and refinement_feedback."""
    return await _run_validator_tool(ctx, tips)


@supervisor.tool
async def tool_execute_query(ctx: RunContext[AgentState], row_limit: int = 20) -> str:
    """Execute the current SQL query against the database to verify it runs. Use after validator says valid.
    Returns success with row count and sample results, or failure with error message.
    If execution fails, call run_generator with tips describing the error so it can fix the SQL."""
    state = ctx.deps
    sql = state.current_sql
    if not sql or not sql.strip():
        return "No SQL to execute. Run the generator first."
    if not state.database_connection:
        return "No database connection available."

    step_start = time.time()
    success, results, error = await execute_sql_safe(state.database_connection, sql, limit=row_limit)
    timing_ms = int((time.time() - step_start) * 1000)

    state.trace.steps.append(
        StepInfo(
            name="execute_query",
            timing_ms=timing_ms,
            input_summary={"sql_length": len(sql), "row_limit": row_limit},
            output_summary={"success": success, "row_count": len(results) if results else 0, "error": error}
            if not success
            else {"success": True, "row_count": len(results) if results else 0},
        )
    )

    if success:
        row_count = len(results) if results else 0
        preview = ""
        if results and len(results) > 0:
            sample = results[0]
            preview = " Sample row: " + str(dict(list(sample.items())[:5]))
        return f"Query executed successfully. Rows returned: {row_count}.{preview}"
    return f"Execution failed: {error}. Call run_generator with tips to fix this error."
