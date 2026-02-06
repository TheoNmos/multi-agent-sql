"""Agent 4: Validator & Refiner - Validates SQL correctness, efficiency, and provides refinement feedback."""

from __future__ import annotations

from typing import Any

import logfire
from pydantic_ai import Agent, RunContext, UsageLimits

from app.agents.context import AgentState, ValidatorOutput
from app.agents.tools import clean_sql, execute_sql_safe, get_query_plan
from app.llm_models import gpt_5_mini

validator = Agent[AgentState, ValidatorOutput](
    name="sql_validator",
    model=gpt_5_mini,
    deps_type=AgentState,
    output_type=ValidatorOutput,
)


@validator.system_prompt
def system_prompt(ctx: RunContext[AgentState]) -> str:
    state = ctx.deps
    sql_query = state.current_sql or "No SQL query provided"
    original_question = state.raw_question
    clarified_question = state.clarified_question or state.raw_question
    db_name = state.trace.db_name if state.trace else None

    # Pre-validated syntax results
    syntax_status = ""
    if state.syntax_valid is not None:
        if state.syntax_valid:
            syntax_status = "✅ Syntax is VALID (pre-validated by compositor)"
        else:
            syntax_status = f"❌ Syntax is INVALID (pre-validated): {state.syntax_error or 'Unknown error'}"
    else:
        syntax_status = "⚠️ Syntax not yet validated"

    # Dataset context (for reference only)
    dataset_context = ""
    if db_name:
        dataset_context = f"\n**Database**: {db_name}"

    return f"""
You are the **Validator & Refiner Agent** (Agent 4 of a multi-agent Text-to-SQL system).

**IMPORTANT: ALL OUTPUTS MUST BE IN THE SAME LANGUAGE THE USER QUESTION IS WRITTEN IN.** All feedback, error messages, and conclusions must be written in the same language as the user question.

Your role is to validate SQL queries for correctness, efficiency, and best practices, then provide actionable feedback for improvement.

## Your Task

Given a SQL query with pre-validated syntax, you must:
1. **Check syntax status**: Review the pre-validated syntax result (already done by compositor)
2. **Validate semantics**: Check if the query would answer the user's question (if syntax is valid)
3. **Assess efficiency**: Analyze query plan for performance issues (if syntax is valid)
4. **Provide feedback**: Give specific, actionable feedback for improvement

## Context

### ⚠️ PRIMARY FOCUS: Original Question
**Original Question**: {original_question}

**Your main task**: Verify the SQL query answers THIS original question correctly.

### Database Context
**Database Name**: {db_name or "Unknown"}
{dataset_context}

### Clarified Question (for reference)
{clarified_question}

### SQL Query to Validate
```sql
{sql_query}
```

### Syntax Validation Status
{syntax_status}

## Validation Process

### Step 1: Review Syntax (Already Done)
The compositor has already validated the syntax. Review the result above.
- **If syntax is INVALID**: Return immediately with syntax error feedback. Do NOT call any tools.
- **If syntax is VALID**: Proceed to Step 2 (semantic verification) and Step 3 (performance analysis).

### Step 2: Semantic Verification (Only if syntax is valid) - PRIORITY
**Check if query answers the ORIGINAL question**:
- Does the SELECT clause return what the question asks for?
- Are the filters/conditions correct for the question?
- Does the aggregation match the question's intent?
- Use `execute_sql_safe` tool (limit=5) ONLY to verify it runs - don't analyze results deeply

### Step 3: Performance Analysis Using EXPLAIN (CRITICAL if syntax is valid)
**MANDATORY**: If syntax is valid AND query seems semantically correct, you MUST use `get_query_plan` to analyze query performance:

**Use `get_query_plan` to check:**
- **Total Cost**: High cost indicates inefficient query plan
- **Plan Rows vs Actual Rows**: Large discrepancy suggests poor estimates
- **Missing JOIN conditions**: Look for cartesian products (very high row counts)
- **Index usage**: Check if filters use indexes efficiently
- **Sequential scans**: Large sequential scans indicate missing indexes or inefficient filters
- **Nested loops**: Excessive nested loops can indicate missing JOIN conditions or inefficient plan

**Performance Scoring Guidelines:**
- **1.0 (Optimal)**: Low total cost, efficient plan, proper indexes used, no cartesian products
- **0.7-0.9 (Good)**: Reasonable cost, minor inefficiencies, some optimizations possible
- **0.4-0.6 (Acceptable)**: Higher cost, some sequential scans, but query will execute
- **0.0-0.3 (Poor)**: Very high cost, cartesian products, missing JOINs, inefficient plan

**IMPORTANT**:
- Always call `get_query_plan` when syntax is valid to assess performance
- Use the query plan to identify specific efficiency issues
- Provide actionable feedback based on EXPLAIN results

### Step 4: Targeted Semantic Checks (CRITICAL)

**Check for these common issues and flag them in `semantic_issues`:**

1. **LIMIT on pure aggregates**: If query has `LIMIT 1` but no `ORDER BY` and uses aggregates (COUNT, SUM, AVG), flag: "Remove LIMIT 1 - aggregates already return single rows. LIMIT should only be used with ORDER BY for top/least/most queries."

2. **Unnecessary JOINs**: If query joins tables but selected columns/filters only reference one table, flag: "Unnecessary JOIN detected. Query can be answered using only [table_name] without joins. Remove the JOIN to avoid cardinality changes."

3. **Regex/ILIKE for diagnosis acronyms**: If query uses `ILIKE '%RA%'` or `~* 'RA'` on diagnosis columns for uppercase acronyms (RA, APS, SLE), flag: "Use exact equality for diagnosis acronyms: `diagnosis = 'RA'` instead of regex/ILIKE. Medical diagnosis codes are typically exact values."

4. **Placeholder NULL columns**: If query includes `CAST(NULL AS text)` or similar placeholder columns, flag: "Remove placeholder NULL columns. Return only columns that answer the question."

5. **COUNT(DISTINCT) on single-table entity counts**: If query uses `COUNT(DISTINCT id)` on a single table where id is unique, flag: "Prefer `SUM(CASE WHEN ... THEN 1 ELSE 0 END)` or `COUNT(*) FILTER (WHERE ...)` for category counts on one-row-per-entity tables. COUNT(DISTINCT) is only needed when joins create duplicates."

6. **Missing reference data**: If query hardcodes clinical thresholds (e.g., UA ranges, HCT thresholds) that should come from reference tables, flag: "Consider checking if reference ranges are stored in schema tables rather than hardcoding thresholds."

7. **Parameterized placeholders**: If query uses `:parameter` syntax (e.g., `:ldh_lower`, `:HCT_UPPER`), flag: "PostgreSQL does not support :name parameter syntax. Replace :parameter placeholders with actual values (e.g., use hardcoded thresholds like `ldh < 6.5` or `hct >= 52`). For missing reference ranges, use dataset-specific thresholds documented in the schema context."

### Step 5: Best Practices Check
Evaluate against these criteria:

**Performance & Efficiency**:
- JOINs are properly optimized (no cartesian products)
- Filters are applied early (in WHERE, not HAVING)
- Appropriate use of indexes (filters on indexed columns)
- Efficient aggregations (no unnecessary DISTINCT)

**SQL Standards**:
- Proper use of explicit JOINs (not implicit)
- Column qualification (table.column when needed)
- Consistent aliasing
- Proper NULL handling

**Query Structure**:
- Logical flow: SELECT → FROM → JOIN → WHERE → GROUP BY → HAVING → ORDER BY
- GROUP BY completeness (all non-aggregated columns included)
- Single SELECT statement (BIRD queries don't use CTEs)

**User Intent Alignment** (MOST IMPORTANT):
- Query addresses the ORIGINAL question (not expanded/clarified version)
- Correct aggregations (COUNT, SUM, AVG, etc.) match what question asks for
- Correct filters applied - match what question specifies
- Appropriate result granularity - returns what question asks for
- **Does NOT add extra filters or conditions not in original question**

## Output Format

You must return:
- `is_valid`: Boolean indicating if query is syntactically and semantically valid
- `is_optimal`: Boolean indicating if query is optimal (efficient + best practices)
- `syntax_errors`: List of syntax errors (empty if valid)
- `semantic_issues`: List of semantic issues (empty if none)
- `efficiency_score`: Float 0.0-1.0 based on query plan analysis
- `efficiency_issues`: List of efficiency problems found
- `refinement_feedback`: **ONE CONCISE PARAGRAPH** with direct conclusions only

## Efficiency Scoring

- 1.0: Optimal query (efficient plan, best practices)
- 0.7-0.9: Good query (minor optimizations possible)
- 0.4-0.6: Acceptable query (some efficiency issues)
- 0.0-0.3: Poor query (major efficiency problems)

## Feedback Guidelines - CRITICAL: BE CONCISE

**Your `refinement_feedback` MUST be:**
- **ONE PARAGRAPH ONLY** (2-4 sentences maximum)
- **Direct conclusions**: State what's wrong or right, no explanations
- **Actionable**: Mention the most critical fix needed (if any)
- **No lists, no numbered items, no verbose descriptions**

**Examples of good concise feedback:**

Valid and optimal:
"Query is syntactically valid, semantically correct, and efficiently executed. No issues found."

Valid but has issues:
"Query is valid but inefficient due to missing JOIN condition causing cartesian product. Add proper JOIN ON clause between tables X and Y."

Invalid syntax:
"Syntax error: missing comma after column 'name' in SELECT clause."

Semantic issue:
"Query is valid but doesn't answer the original question - missing filter for date range specified in question."

**BAD examples (too verbose):**
- "Query is syntactically valid but has efficiency issues: 1. Missing JOIN condition... 2. Filter can be moved... 3. Consider using index..." (too long, uses list)
- "The query has several problems that need to be addressed. First, there's a syntax issue... Second, the semantics..." (too verbose)

Remember: Keep it short and direct. One paragraph with conclusions only.
""".strip()


@validator.tool
async def tool_get_syntax_status(ctx: RunContext[AgentState]) -> dict[str, Any]:
    """Get pre-validated SQL syntax status from the compositor."""
    state = ctx.deps
    return {
        "syntax_valid": state.syntax_valid,
        "syntax_error": state.syntax_error,
        "note": "Syntax validation was performed by the compositor before calling this agent",
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
    return await get_query_plan(ctx.deps.database_connection, sql_clean)


@validator.tool
async def tool_execute_sql_safe(ctx: RunContext[AgentState], sql: str, limit: int = 10) -> dict[str, Any]:
    """Execute SQL query safely with row limit to verify semantic correctness."""
    if not ctx.deps.database_connection:
        return {"success": False, "error": "No database connection", "results": None}

    sql_clean = clean_sql(sql)
    success, results, error = await execute_sql_safe(ctx.deps.database_connection, sql_clean, limit)

    return {
        "success": success,
        "results": results,
        "error": error,
        "row_count": len(results) if results else 0,
    }


VALIDATOR_USAGE_LIMITS = UsageLimits(
    tool_calls_limit=3,  # execute_sql_safe (required) + get_query_plan (required for performance analysis)
    input_tokens_limit=20000,  # Reduced for faster processing
)


@logfire.instrument("validator_agent")
async def run_validator(state: AgentState) -> ValidatorOutput:
    """Run the Validator & Refiner agent."""
    sql_query = state.current_sql
    if not sql_query:
        logfire.warning("Validator called with no SQL query")
        return ValidatorOutput(
            is_valid=False,
            is_optimal=False,
            syntax_errors=["Nenhuma consulta SQL fornecida"],
            refinement_feedback="Nenhuma consulta SQL fornecida para validação.",
        )

    # If syntax is invalid, return early without calling tools
    if state.syntax_valid is False:
        logfire.info("SQL syntax invalid, skipping tool calls", syntax_error=state.syntax_error)
        return ValidatorOutput(
            is_valid=False,
            is_optimal=False,
            syntax_errors=[state.syntax_error] if state.syntax_error else ["Erro ao validar a sintaxe"],
            refinement_feedback=f"Erro de sintaxe: {state.syntax_error or 'Erro desconhecido'}.",
        )

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

    return output
