"""Agent 3: SQL Generator - Generates SQL queries following BIRD dataset structure patterns."""

from __future__ import annotations

from typing import Any

import logfire
from pydantic_ai import Agent, RunContext, UsageLimits

from app.agents.context import AgentState, GeneratorOutput, MapperOutput
from app.agents.llm_timeout import run_with_llm_timeout
from app.agents.telemetry import usage_to_dict
from app.llm_models import generator_model
from app.prompts import (
    DEFAULT_GENERATOR_PROMPT,
    dialect_label,
    dialect_notes,
    format_supervisor_tips,
    render_prompt,
)

generator = Agent[AgentState, GeneratorOutput](
    name="sql_generator",
    model=generator_model,
    deps_type=AgentState,
    output_type=GeneratorOutput,
)


def _render_mapper_output(mapper_output: MapperOutput) -> str:
    """Render structured mapper output into compact guidance for SQL generation."""
    lines: list[str] = []

    if mapper_output.selected_tables:
        lines.append("### Selected Tables")
        for table in mapper_output.selected_tables:
            columns = ", ".join(table.relevant_columns) if table.relevant_columns else "not specified"
            lines.append(f"- {table.table_name} ({table.priority}): {table.reason} Relevant columns: {columns}.")

    if mapper_output.columns:
        lines.append("\n### Columns By Role")
        for column in mapper_output.columns:
            values = f" Values: {', '.join(column.sample_values)}." if column.sample_values else ""
            value_format = f" Format: {column.value_format}." if column.value_format else ""
            data_type = f" Type: {column.data_type}." if column.data_type else ""
            lines.append(
                f"- {column.role}: {column.table_name}.{column.column_name}.{data_type}{value_format}{values} "
                f"Reason: {column.reason}"
            )

    encoded_filter_columns = [
        column
        for column in mapper_output.columns
        if column.role == "filter" and column.sample_values and column.value_format
    ]
    if encoded_filter_columns:
        lines.append("\n### Stored Filter Literals")
        for column in encoded_filter_columns:
            values = ", ".join(repr(value) for value in column.sample_values)
            lines.append(
                f"- {column.table_name}.{column.column_name}: use stored literal(s) {values}; "
                f"format/meaning: {column.value_format}. Do not replace these with semantic labels."
            )

    if mapper_output.joins:
        lines.append("\n### Allowed Joins")
        for join in mapper_output.joins:
            warning = f" Warning: {join.cardinality_warning}" if join.cardinality_warning else ""
            lines.append(
                f"- {join.join_type} JOIN {join.left_table}.{join.left_column} = "
                f"{join.right_table}.{join.right_column}. Reason: {join.reason}.{warning}"
            )
    else:
        lines.append("\n### Allowed Joins\n- No joins required or confirmed by the mapper.")

    if mapper_output.target_columns:
        lines.append("\n### Target SELECT Columns/Expressions")
        lines.extend(f"- {target}" for target in mapper_output.target_columns)

    if mapper_output.filters:
        lines.append("\n### Expected Filters")
        lines.extend(f"- {filter_note}" for filter_note in mapper_output.filters)

    if mapper_output.required_constraints:
        lines.append("\n### Required Constraints")
        lines.extend(f"- {constraint}" for constraint in mapper_output.required_constraints)

    if mapper_output.value_notes:
        lines.append("\n### Value Notes")
        lines.extend(f"- {note}" for note in mapper_output.value_notes)

    if mapper_output.cardinality_notes:
        lines.append("\n### Cardinality Notes")
        lines.extend(f"- {note}" for note in mapper_output.cardinality_notes)

    if mapper_output.validation_notes:
        lines.append("\n### Mapper Validation Notes")
        lines.extend(f"- {note}" for note in mapper_output.validation_notes)

    lines.append(f"\n### Mapper Confidence\n{mapper_output.confidence:.2f}")
    return "\n".join(lines)


def _build_generator_template_vars(ctx: RunContext[AgentState]) -> dict[str, str]:
    """Build template variables for the generator prompt."""
    state = ctx.deps
    clarified_question = state.clarified_question or state.raw_question
    interpreter_output = state.interpreter_output
    mapper_output = state.mapper_output

    schema_context = ""
    if mapper_output:
        schema_context = f"\n## Structured Schema Mapping\n\n{_render_mapper_output(mapper_output)}\n"

    iteration_context = ""
    if state.attempt_count > 0:
        execution_feedback = state.scratch.get("execution_feedback")
        execution_feedback_section = ""
        if execution_feedback:
            execution_feedback_section = f"""
### Previous Execution Feedback:
{execution_feedback}
"""
        iteration_context = f"""
## Iteration Context (Attempt {state.attempt_count + 1}/{state.max_attempts})

This is a refinement iteration. Previous attempts have been made.

### Previous Query:
{state.current_sql or "None"}

### Previous Validation Feedback:
{state.validator_output.refinement_feedback if state.validator_output else "None"}

{execution_feedback_section}

**IMPORTANT**: You must address the feedback above and generate an improved query.
"""

    sub_questions_context = ""
    if interpreter_output and interpreter_output.sub_questions:
        sub_questions_context = "\n### Sub-Questions to Address:\n"
        for i, subq in enumerate(interpreter_output.sub_questions, 1):
            sub_questions_context += f"{i}. {subq}\n"
        sub_questions_context += (
            "\nNote: Even if complex, generate a single SELECT statement (no CTEs or subqueries).\n"
        )

    explicit_intent = interpreter_output.explicit_intent if interpreter_output else "Not provided"

    user_filter_literals_section = ""
    if interpreter_output and interpreter_output.user_filter_literals:
        items = "\n".join(
            f"- `{key}`: {repr(value)}" for key, value in interpreter_output.user_filter_literals.items()
        )
        user_filter_literals_section = (
            "\n## User-Specified Filter Values (HIGHEST PRIORITY — override mapper samples when they conflict)\n"
            f"{items}\n"
        )

    aggregation_granularity_section = ""
    if interpreter_output and interpreter_output.aggregation_granularity != "unspecified":
        aggregation_granularity_section = (
            f"\n## Aggregation granularity (from interpreter)\n"
            f"`{interpreter_output.aggregation_granularity}`\n"
        )

    return {
        "clarified_question": clarified_question,
        "explicit_intent": explicit_intent,
        "user_filter_literals": user_filter_literals_section,
        "aggregation_granularity": aggregation_granularity_section,
        "sub_questions_context": sub_questions_context,
        "schema_context": schema_context,
        "iteration_context": iteration_context,
        "supervisor_tips": format_supervisor_tips(ctx.deps.supervisor_tips.get("generator")),
        "sql_dialect_label": dialect_label(ctx.deps.sql_dialect),
        "sql_dialect_notes": dialect_notes(ctx.deps.sql_dialect),
    }


@generator.system_prompt
def system_prompt(ctx: RunContext[AgentState]) -> str:
    template_vars = _build_generator_template_vars(ctx)
    custom = (ctx.deps.custom_prompts or {}).get("generator")
    template = custom if custom else DEFAULT_GENERATOR_PROMPT
    return render_prompt(template, template_vars)


GENERATOR_USAGE_LIMITS = UsageLimits(
    input_tokens_limit=100000,
    # Cap completion length so a bad model/provider cannot stream unbounded on retries.
    output_tokens_limit=20000,
)


@logfire.instrument("generator_agent")
async def run_generator(state: AgentState) -> tuple[GeneratorOutput, dict[str, Any]]:
    """Run the SQL Generator agent."""
    clarified_question = state.clarified_question or state.raw_question
    logfire.info(
        "Running SQL Generator",
        clarified_question=clarified_question,
        attempt=state.attempt_count + 1,
        has_feedback=bool(state.validator_output),
    )

    # Build prompt with context
    prompt = clarified_question
    if state.validator_output and state.validator_output.refinement_feedback:
        prompt += f"\n\nPrevious validation feedback:\n{state.validator_output.refinement_feedback}"
    if state.scratch.get("execution_feedback"):
        prompt += f"\n\nPrevious execution feedback:\n{state.scratch['execution_feedback']}"

    result = await run_with_llm_timeout(
        generator.run(prompt, deps=state, usage_limits=GENERATOR_USAGE_LIMITS),
        context="sql generator",
    )
    output = result.output

    logfire.info(
        "SQL Generator completed",
        sql_length=len(output.sql_query),
        reasoning_steps_count=len(output.reasoning_steps),
        sub_queries_count=len(output.sub_queries),
        confidence=output.confidence,
    )

    return output, usage_to_dict(result.usage())
