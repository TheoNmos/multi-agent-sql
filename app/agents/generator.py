"""Agent 3: SQL Generator - Generates SQL queries following BIRD dataset structure patterns."""

from __future__ import annotations

import logfire
from pydantic_ai import Agent, RunContext

from app.agents.context import AgentState, GeneratorOutput
from app.llm_models import gpt_5_mini
from app.prompts import DEFAULT_GENERATOR_PROMPT, render_prompt

generator = Agent[AgentState, GeneratorOutput](
    name="sql_generator",
    model=gpt_5_mini,
    deps_type=AgentState,
    output_type=GeneratorOutput,
)


def _build_generator_template_vars(ctx: RunContext[AgentState]) -> dict[str, str]:
    """Build template variables for the generator prompt."""
    state = ctx.deps
    clarified_question = state.clarified_question or state.raw_question
    interpreter_output = state.interpreter_output
    mapper_output = state.mapper_output

    schema_context = ""
    if mapper_output:
        schema_context = f"\n## Available Schema & Context\n\n{mapper_output}\n"

    iteration_context = ""
    if state.attempt_count > 0:
        iteration_context = f"""
## Iteration Context (Attempt {state.attempt_count + 1}/{state.max_attempts})

This is a refinement iteration. Previous attempts have been made.

### Previous Query:
{state.current_sql or "None"}

### Previous Validation Feedback:
{state.validator_output.refinement_feedback if state.validator_output else "None"}

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

    return {
        "clarified_question": clarified_question,
        "explicit_intent": explicit_intent,
        "sub_questions_context": sub_questions_context,
        "schema_context": schema_context,
        "iteration_context": iteration_context,
    }


@generator.system_prompt
def system_prompt(ctx: RunContext[AgentState]) -> str:
    template_vars = _build_generator_template_vars(ctx)
    custom = (ctx.deps.custom_prompts or {}).get("generator")
    template = custom if custom else DEFAULT_GENERATOR_PROMPT
    return render_prompt(template, template_vars)


@logfire.instrument("generator_agent")
async def run_generator(state: AgentState) -> GeneratorOutput:
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

    result = await generator.run(prompt, deps=state)
    output = result.output

    logfire.info(
        "SQL Generator completed",
        sql_length=len(output.sql_query),
        reasoning_steps_count=len(output.reasoning_steps),
        sub_queries_count=len(output.sub_queries),
        confidence=output.confidence,
    )

    return output
