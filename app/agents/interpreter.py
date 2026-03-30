"""Agent 1: Query Interpreter (Rewriter) - Clarifies and rewrites natural language questions."""

from __future__ import annotations

from typing import Any

import logfire
from pydantic_ai import Agent, RunContext, UsageLimits

from app.agents.context import AgentState, InterpreterOutput
from app.agents.telemetry import usage_to_dict
from app.llm_models import gpt_5_mini
from app.prompts import DEFAULT_INTERPRETER_PROMPT, format_supervisor_tips, render_prompt

interpreter = Agent[AgentState, InterpreterOutput](
    name="query_interpreter",
    model=gpt_5_mini,
    deps_type=AgentState,
    output_type=InterpreterOutput,
)


@interpreter.system_prompt
def system_prompt(ctx: RunContext[AgentState]) -> str:
    template_vars = {
        "raw_question": ctx.deps.raw_question,
        "supervisor_tips": format_supervisor_tips(ctx.deps.supervisor_tips.get("interpreter")),
    }
    custom = (ctx.deps.custom_prompts or {}).get("interpreter")
    template = custom if custom else DEFAULT_INTERPRETER_PROMPT
    return render_prompt(template, template_vars)


INTERPRETER_USAGE_LIMITS = UsageLimits(input_tokens_limit=100000)


@logfire.instrument("interpreter_agent")
async def run_interpreter(state: AgentState) -> tuple[InterpreterOutput, dict[str, Any]]:
    """Run the Query Interpreter agent."""
    logfire.info("Running Query Interpreter", raw_question=state.raw_question)

    result = await interpreter.run(state.raw_question, deps=state, usage_limits=INTERPRETER_USAGE_LIMITS)
    output = result.output

    logfire.info(
        "Query Interpreter completed",
        clarified_question=output.clarified_question,
        sub_question_count=len(output.sub_questions),
        ambiguities_count=len(output.ambiguities_resolved),
    )

    return output, usage_to_dict(result.usage())
