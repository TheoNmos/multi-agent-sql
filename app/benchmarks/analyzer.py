"""Benchmark Result Analyzer - Approves good results or analyzes failed benchmark results to identify root causes."""

from __future__ import annotations

import logging
from typing import Any

import logfire
from pydantic import BaseModel, Field
from pydantic_ai import Agent, RunContext

from app.llm_models import gpt_5_mini_minimal
from app.toon_utils import to_toon_block

logger = logging.getLogger(__name__)


class AnalyzerOutput(BaseModel):
    """Output from the benchmark result analyzer."""

    analyzer_match: bool = Field(
        description="Whether the results deliver the same business impact/information (AM - Analyzer Match). True if results are semantically equivalent despite differences in column names, order, or extra fields."
    )
    approved: bool = Field(
        description="Whether the result is approved (good) or needs failure analysis. Set to True if exact_match, execution_match, or analyzer_match is True."
    )
    failure_step: str = Field(
        default="none",
        description="The step/agent where the failure occurred (e.g., 'interpreter', 'mapper', 'generator', 'validator', 'none' if approved). Only set if analyzer_match is False.",
    )
    failure_reason: str = Field(
        default="Result is correct - no failure analysis needed",
        description="Detailed explanation of why the failure occurred at this step, or explanation of why the result is valid (if analyzer_match is True).",
    )
    query_issues: str = Field(
        default="No issues found - query matches expected results",
        description="Specific issues identified in the generated SQL query compared to the gold SQL, or explanation of differences that don't affect business impact (if analyzer_match is True).",
    )
    root_cause: str = Field(
        default="Result is correct",
        description="Root cause analysis explaining the fundamental reason for the incorrect query, or explanation of why results are semantically equivalent (if analyzer_match is True).",
    )
    suggestions: list[str] = Field(
        default_factory=list,
        description="Actionable suggestions for improving the system to prevent similar failures, or empty list if result is good. Should identify which agents need improvement and what should be done.",
    )


analyzer = Agent[dict[str, Any], AnalyzerOutput](
    name="benchmark_analyzer",
    model=gpt_5_mini_minimal,
    deps_type=dict,
    output_type=AnalyzerOutput,
)


@analyzer.system_prompt
def system_prompt(ctx: RunContext[dict[str, Any]]) -> str:
    result_data = ctx.deps

    # Extract key information from the benchmark result
    question = result_data.get("question", "")
    gold_sql = result_data.get("gold_sql", "")
    predicted_sql = result_data.get("predicted_sql", "")
    exact_match = result_data.get("exact_match", False)
    execution_match = result_data.get("execution_match", False)
    gold_execution_results = result_data.get("gold_execution_results", [])
    predicted_execution_results = result_data.get("predicted_execution_results", [])
    agent_trace = result_data.get("agent_trace", {})
    error = result_data.get("error")
    execution_error = result_data.get("execution_error")

    # Format execution results for comparison using TOON
    gold_results_str = to_toon_block(gold_execution_results, "gold_results") if gold_execution_results else "None"
    predicted_results_str = (
        to_toon_block(predicted_execution_results, "predicted_results") if predicted_execution_results else "None"
    )

    # Extract trace information
    trace_steps = agent_trace.get("trace", {}).get("steps", []) if agent_trace else []
    steps_info = []
    for step in trace_steps:
        step_output = step.get("output_summary", {})
        step_name = step.get("name", "unknown")
        timing_ms = step.get("timing_ms", 0)
        # Format step output as TOON if it's a dict, otherwise as text
        if isinstance(step_output, dict) and step_output:
            step_toon = to_toon_block(step_output, f"{step_name}_output")
            steps_info.append(f"- {step_name} (timing: {timing_ms}ms):\n{step_toon}")
        else:
            steps_info.append(f"- {step_name}: {step_output} (timing: {timing_ms}ms)")

    steps_str = "\n".join(steps_info) if steps_info else "No step information available"

    # Extract agent outputs from trace
    interpreter_output = agent_trace.get("interpreter_output", {}) if agent_trace else {}
    mapper_output = agent_trace.get("mapper_output", "") if agent_trace else ""
    if isinstance(mapper_output, dict):
        mapper_output_text = to_toon_block(mapper_output, "mapper_output")
    else:
        mapper_output_text = str(mapper_output) if mapper_output else ""
    generator_output = agent_trace.get("generator_output", {}) if agent_trace else {}
    validator_output = agent_trace.get("validator_output") if agent_trace else None

    return f"""
You are a **Benchmark Result Analyzer**. Your job is to determine if the predicted SQL results deliver the same **business impact** and **information** as the gold SQL results, even if they differ in format, column names, order, or have extra fields.

## Benchmark Result Data

### Question
{question}

### Gold SQL (Correct Answer)
```sql
{gold_sql}
```

### Predicted SQL (Generated by System)
```sql
{predicted_sql}
```

### Execution Results Comparison
**Gold SQL Results (TOON):**
{gold_results_str}

**Predicted SQL Results (TOON):**
{predicted_results_str}

### Match Status
- Exact Match (EM): {exact_match}
- Result Match (RM/Execution Match): {execution_match}

### Errors
- System Error: {error if error else "None"}
- Execution Error: {execution_error if execution_error else "None"}

### Agent Execution Trace
**Pipeline Steps:**
{steps_str}

**Interpreter Output (TOON):**
{to_toon_block(interpreter_output, "interpreter_output") if interpreter_output else "None"}

**mapper Output:**
{mapper_output_text[:2000] if mapper_output_text else "None"}

**Generator Output (TOON):**
{to_toon_block(generator_output, "generator_output") if generator_output else "None"}

**Validator Output (TOON):**
{to_toon_block(validator_output, "validator_output") if validator_output else "None"}

## Your Analysis Task

### Step 1: Check Rule-Based Matches

**If exact_match is True OR execution_match is True:**
- Set `analyzer_match` to `True` (since rule-based match already confirms correctness)
- Set `approved` to `True`
- Set `failure_step` to `"none"`
- Set `failure_reason` to `"Result is correct - rule-based match (EM or RM) confirms correctness"`
- Set `query_issues` to `"No issues found - query matches expected results"`
- Set `root_cause` to `"Result is correct - validated by exact match or execution match"`
- Set `suggestions` to an empty list `[]`
- **STOP HERE - do not perform business impact analysis**

### Step 2: Business Impact Analysis (Analyzer Match - AM)

**If both exact_match and execution_match are False, you MUST analyze whether the results deliver the same business impact:**

Your goal is to determine if the predicted results answer the question with the same **business meaning** and **information content** as the gold results, even if:
- Column names are different (e.g., "total" vs "sum_amount")
- Row order is different
- Extra columns are present in predicted results (as long as they don't contradict the answer)
- Data formatting differs (e.g., "100.00" vs "100")

**Set `analyzer_match` to `True` if:**
- The predicted results contain the same **core information** needed to answer the question
- The **business impact** is equivalent (same counts, sums, averages, etc.)
- Differences are only cosmetic (column names, order, formatting)
- Extra fields don't contradict or mislead

**Set `analyzer_match` to `False` if:**
- Missing critical data that answers the question
- Wrong values (different counts, sums, calculations)
- Different filtering logic that changes the answer
- Contradictory information

### Step 3: Output Fields Based on Analyzer Match

**If `analyzer_match` is True:**
- Set `approved` to `True`
- Set `failure_step` to `"none"`
- Set `failure_reason` to a detailed explanation of WHY the results are semantically equivalent despite differences (e.g., "Results are equivalent: predicted query uses different column names ('total_sales' vs 'sum') but returns the same values. Row order differs but doesn't affect the answer.")
- Set `query_issues` to describe the differences that DON'T affect business impact (e.g., "Column names differ: 'total' vs 'sum_amount', but values match. Extra column 'id' present but doesn't affect answer.")
- Set `root_cause` to explain why the results are valid (e.g., "Results deliver equivalent business impact: same data values, only presentation differs")
- Set `suggestions` to an empty list `[]`

**If `analyzer_match` is False:**
- Set `approved` to `False`
- Set `failure_step` to identify which agent(s) introduced the error:
  - "interpreter" - if the question interpretation was wrong
  - "mapper" - if wrong tables/columns were selected
  - "generator" - if SQL generation logic was incorrect
  - "validator" - if validation failed to catch errors
  - "multiple" - if errors occurred in multiple steps
- Set `failure_reason` to detailed explanation of what went wrong at the identified step, including:
  - What the agent did incorrectly
  - What information was missing or misinterpreted
  - How this led to incorrect results
- Set `query_issues` to specific issues in the predicted SQL/results compared to gold:
  - Missing or incorrect WHERE clauses
  - Wrong aggregations or calculations
  - Incorrect table/column references
  - Logic errors in CASE statements or calculations
  - Missing critical data
  - Wrong values in results
- Set `root_cause` to fundamental reason for the failure:
  - Was it a misunderstanding of the question?
  - Was it incorrect schema understanding?
  - Was it a logic error in SQL construction?
  - Was it a missing validation step?
- Set `suggestions` to actionable suggestions (3-5 items) identifying:
  - **Which specific agents** need improvement
  - **What should be done** to fix the issue
  - What additional validation or checks could prevent this
  - What improvements to prompts or logic would help

## Analysis Guidelines

- **CRITICAL**: Always check business impact, not just exact matching
- If EM or RM is True, set analyzer_match=True and approve immediately
- If EM and RM are False, you MUST determine analyzer_match by checking business impact
- Be specific and concrete in your analysis
- Reference specific parts of the trace, SQL, or results
- Focus on actionable insights that can improve the system
- Consider the full pipeline flow, not just the final SQL
- Identify the earliest point where the error was introduced (if analyzer_match is False)
- When analyzer_match is True, clearly explain WHY the results are equivalent despite differences
"""


async def analyze_benchmark_result(result: dict[str, Any]) -> AnalyzerOutput:
    """
    Analyze a benchmark result to determine if results deliver the same business impact.
    Always runs unless exact_match or execution_match is True.

    Args:
        result: Dictionary containing benchmark result data (EvalCaseResult as dict)

    Returns:
        AnalyzerOutput with analyzer_match (AM) status and analysis
    """
    exact_match = result.get("exact_match", False)
    execution_match = result.get("execution_match", False)
    index = result.get("index")

    logfire.info("Analyzing benchmark result", index=index, exact_match=exact_match, execution_match=execution_match)

    try:
        # Check if result has rule-based match - if so, approve immediately with analyzer_match=True
        if exact_match or execution_match:
            logfire.info("Benchmark result approved - rule-based match (EM or RM)", index=index)
            return AnalyzerOutput(
                analyzer_match=True,
                approved=True,
                failure_step="none",
                failure_reason="Result is correct - rule-based match (EM or RM) confirms correctness",
                query_issues="No issues found - query matches expected results",
                root_cause="Result is correct - validated by exact match or execution match",
                suggestions=[],
            )

        # Run the analyzer agent to check business impact (AM - Analyzer Match)
        from app.agents.llm_timeout import run_with_llm_timeout

        analyzer_result = await run_with_llm_timeout(
            analyzer.run(
                "Determine if the predicted SQL results deliver the same business impact and information as the gold SQL results, even if they differ in format, column names, order, or have extra fields.",
                deps=result,
            ),
            context="benchmark analyzer",
        )
        output = analyzer_result.output

        logfire.info(
            "Benchmark analysis completed",
            index=index,
            analyzer_match=output.analyzer_match,
            approved=output.approved,
            failure_step=output.failure_step,
        )

        return output

    except Exception as e:
        logger.error(f"Error analyzing benchmark result: {e}", exc_info=True)
        logfire.error("Failed to analyze benchmark result", error=str(e), index=index)

        # Return a default error output
        return AnalyzerOutput(
            analyzer_match=False,
            approved=False,
            failure_step="analyzer",
            failure_reason=f"Analysis failed with error: {str(e)}",
            query_issues="Unable to analyze due to analyzer failure",
            root_cause="Analyzer agent encountered an error",
            suggestions=["Fix analyzer agent error handling", "Review analyzer prompt"],
        )
