"""Agent 4: Validator & Refiner - Validates SQL correctness, efficiency, and provides refinement feedback."""

from __future__ import annotations

import re
from typing import Any

import logfire
from pydantic_ai import Agent, RunContext, UsageLimits

from app.agents.context import AgentState, ToolCall, ValidatorOutput
from app.agents.telemetry import usage_to_dict
from app.agents.tools import clean_sql, execute_sql_safe, get_query_plan
from app.llm_models import validator_model
from app.prompts import (
    DEFAULT_VALIDATOR_PROMPT,
    dialect_label,
    dialect_notes,
    format_supervisor_tips,
    render_prompt,
)

validator = Agent[AgentState, ValidatorOutput](
    name="sql_validator",
    model=validator_model,
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

    mapper_contract = ""
    mapper_output = state.mapper_output
    if mapper_output:
        required_constraints = "\n".join(f"- {item}" for item in mapper_output.required_constraints)
        filters = "\n".join(f"- {item}" for item in mapper_output.filters)
        joins = "\n".join(
            f"- {join.left_table}.{join.left_column} = {join.right_table}.{join.right_column}"
            for join in mapper_output.joins
        )
        mapper_contract = f"""
### Mapper Contract
Expected filters:
{filters or "- None"}

Required constraints:
{required_constraints or "- None"}

Required/allowed joins:
{joins or "- None"}
"""

    return {
        "original_question": original_question,
        "clarified_question": clarified_question,
        "db_name": db_name or "Unknown",
        "dataset_context": dataset_context,
        "sql_query": sql_query,
        "syntax_status": syntax_status,
        "mapper_contract": mapper_contract,
        "supervisor_tips": format_supervisor_tips(ctx.deps.supervisor_tips.get("validator")),
        "sql_dialect_label": dialect_label(ctx.deps.sql_dialect),
        "sql_dialect_notes": dialect_notes(ctx.deps.sql_dialect),
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
                result_preview=f"{len(results) if results else 0} rows"
                if success
                else (error or "Execution failed")[:120],
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


_PHYSICAL_TUNING_PATTERNS = [
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\badd(?:ing)? an? index\b",
        r"\bcreate(?:ing)? an? index\b",
        r"\bmissing index\b",
        r"\bindex usage\b",
        r"\buse(?:s|d)? indexes?\b",
        r"\bindexed columns?\b",
        r"\bsequential scan\b",
        r"\bseq scan\b",
        r"\bvacuum\b",
        r"\banalyze\b",
        r"\bupdate statistics\b",
        r"\bpartition(?:ing)?\b",
        r"\bmaterialized view\b",
        r"\bschema change\b",
    )
]


def _is_physical_tuning_feedback(text: str) -> bool:
    return any(pattern.search(text) for pattern in _PHYSICAL_TUNING_PATTERNS)


def _remove_physical_tuning_feedback(output: ValidatorOutput) -> None:
    """Drop feedback the SQL generator cannot act on, such as adding indexes."""
    original_semantic_count = len(output.semantic_issues)
    original_efficiency_count = len(output.efficiency_issues)

    output.semantic_issues = [issue for issue in output.semantic_issues if not _is_physical_tuning_feedback(issue)]
    output.efficiency_issues = [issue for issue in output.efficiency_issues if not _is_physical_tuning_feedback(issue)]

    removed_only_feedback = (
        output.is_valid
        and original_semantic_count + original_efficiency_count > 0
        and not output.semantic_issues
        and not output.efficiency_issues
    )
    if removed_only_feedback:
        output.is_optimal = True
        output.efficiency_score = max(output.efficiency_score, 0.95)

    if _is_physical_tuning_feedback(output.refinement_feedback):
        if output.semantic_issues or output.efficiency_issues:
            output.refinement_feedback = (
                "Query is syntactically valid, but it has SQL-level issues listed above that the generator should fix."
            )
        else:
            output.refinement_feedback = (
                "Query is syntactically valid and semantically correct. No SQL-level optimization issues found."
            )


_CONSTRAINT_LITERAL_RE = re.compile(r"'([^']+)'|\"([^\"]+)\"|\b[A-Z]{2,}\b")
_CREATININE_THRESHOLD_RE = re.compile(
    r"\b(?:cre|creatinine)\w*\b\s*(?:>=|>|<=|<|=)\s*([0-9]+(?:\.[0-9]+)?)"
    r"|([0-9]+(?:\.[0-9]+)?)\s*(?:>=|>|<=|<|=)\s*\b(?:cre|creatinine)\w*\b",
    re.IGNORECASE,
)
_CURRENT_AGE_PHRASE_RE = re.compile(
    r"\b(?:under|younger than|below|less than|aren'?t|isn'?t|not yet)\s+\d+\b", re.IGNORECASE
)
_SQL_LITERAL_RE = re.compile(r"'([^']*)'|\"([^\"]*)\"")
_CONSTRAINT_LITERAL_STOP_WORDS = {
    "AND",
    "AS",
    "BETWEEN",
    "CASE",
    "COUNT",
    "DATE",
    "ELSE",
    "END",
    "FROM",
    "GROUP",
    "INNER",
    "JOIN",
    "LIKE",
    "NULL",
    "ON",
    "OR",
    "ORDER",
    "SELECT",
    "SQL",
    "SUM",
    "THEN",
    "WHERE",
}


def _mapper_contract_text(state: AgentState) -> str:
    mapper_output = state.mapper_output
    if not mapper_output:
        return ""
    chunks = [
        *mapper_output.filters,
        *mapper_output.required_constraints,
        *mapper_output.value_notes,
        *mapper_output.validation_notes,
    ]
    for column in mapper_output.columns:
        chunks.extend([column.table_name, column.column_name, column.reason, column.data_type or "", column.value_format or ""])
        chunks.extend(column.sample_values)
    return "\n".join(str(chunk) for chunk in chunks if chunk)


def _extract_constraint_literals(text: str, *, include_unquoted_codes: bool = False) -> set[str]:
    literals: set[str] = set()
    for match in _CONSTRAINT_LITERAL_RE.finditer(text):
        quoted_literal = match.group(1) or match.group(2)
        if quoted_literal is None and not include_unquoted_codes:
            continue
        literal = quoted_literal or match.group(0)
        if len(literal) >= 2 and literal.upper() not in _CONSTRAINT_LITERAL_STOP_WORDS:
            literals.add(literal)
    return literals


def _is_code_like_value(value: str) -> bool:
    stripped = value.strip()
    return bool(stripped) and (len(stripped) <= 3 or not any(char.isalpha() for char in stripped))


def _filter_values_are_constraining(column: Any, mapper_output: Any) -> bool:
    """Decide whether sample_values are confirmed filter literals rather than broad examples."""
    if not column.sample_values:
        return False

    evidence = "\n".join(
        [
            *mapper_output.filters,
            *mapper_output.required_constraints,
            *mapper_output.value_notes,
            *mapper_output.validation_notes,
            column.reason,
            column.value_format or "",
        ]
    ).lower()
    sample_values = [str(value).strip() for value in column.sample_values if str(value).strip()]

    return (
        bool(column.value_format)
        or any(value.lower() in evidence for value in sample_values)
        or all(_is_code_like_value(value) for value in sample_values)
    )


def _extract_column_filter_literals(sql_query: str, column_name: str) -> set[str]:
    """Extract quoted literals compared against a column in WHERE-like predicates."""
    escaped_column = re.escape(column_name)
    column_ref = rf"(?:\b\w+\.)?{escaped_column}\b"
    literals: set[str] = set()

    rhs_pattern = re.compile(
        rf"{column_ref}\s*(?:=|!=|<>|IN\s*\()\s*(?P<rhs>[^;\n)]*(?:\)[^;\n]*)?)",
        re.IGNORECASE,
    )
    for match in rhs_pattern.finditer(sql_query):
        rhs = re.split(r"\b(?:AND|OR|GROUP\s+BY|ORDER\s+BY|LIMIT|HAVING)\b", match.group("rhs"), maxsplit=1, flags=re.IGNORECASE)[
            0
        ]
        literals.update(group for literal_match in _SQL_LITERAL_RE.finditer(rhs) for group in literal_match.groups() if group)

    lhs_pattern = re.compile(rf"(?P<lhs>'[^']*'|\"[^\"]*\")\s*(?:=|!=|<>)\s*{column_ref}", re.IGNORECASE)
    for match in lhs_pattern.finditer(sql_query):
        literal_match = _SQL_LITERAL_RE.match(match.group("lhs"))
        if literal_match:
            literals.update(group for group in literal_match.groups() if group)

    return literals


def _append_semantic_issue(output: ValidatorOutput, issue: str) -> None:
    if issue not in output.semantic_issues:
        output.semantic_issues.append(issue)
    output.is_valid = False
    output.is_optimal = False
    if output.efficiency_score > 0.6:
        output.efficiency_score = 0.6
    if issue not in output.refinement_feedback:
        output.refinement_feedback = (
            f"{output.refinement_feedback}\n{issue}".strip()
            if output.refinement_feedback
            else issue
        )


def _apply_intent_semantic_guards(state: AgentState, output: ValidatorOutput) -> None:
    """Reject SQL that silently drops mapper constraints or invents risky clinical semantics."""
    sql_query = state.current_sql or ""
    sql_lower = sql_query.lower()
    mapper_output = state.mapper_output

    interp = state.interpreter_output
    user_literal_values_lower: set[str] = set()
    if interp and interp.user_filter_literals:
        user_literal_values_lower = {
            str(v).strip().lower() for v in interp.user_filter_literals.values() if str(v).strip()
        }
        for key, val in interp.user_filter_literals.items():
            v = str(val).strip()
            if not v:
                continue
            if not re.search(re.escape(v), sql_query, re.IGNORECASE):
                _append_semantic_issue(
                    output,
                    f"Missing user-specified filter value `{v}` (interpreter key `{key}`). "
                    "Include it in the appropriate WHERE/CASE/HAVING predicate; user literals override mapper samples.",
                )

    if mapper_output:
        required_literals = set()
        for text in mapper_output.filters:
            required_literals.update(_extract_constraint_literals(text, include_unquoted_codes=True))
        for text in mapper_output.required_constraints:
            required_literals.update(_extract_constraint_literals(text))

        for literal in sorted(required_literals, key=len, reverse=True):
            if literal.lower() not in sql_lower:
                _append_semantic_issue(
                    output,
                    f"Missing required mapper constraint value `{literal}` in the SQL. "
                    "Do not drop required filters from the mapped intent.",
                )

        for column in mapper_output.columns:
            if column.role != "filter":
                continue
            if column.column_name.lower() not in sql_lower:
                _append_semantic_issue(
                    output,
                    f"Missing required filter column `{column.table_name}.{column.column_name}` from mapper output.",
                )
                continue

            if _filter_values_are_constraining(column, mapper_output):
                allowed_values = {str(value).strip() for value in column.sample_values if str(value).strip()}
                allowed_lower = {value.lower() for value in allowed_values}
                used_literals = _extract_column_filter_literals(sql_query, column.column_name)
                unexpected_literals = sorted(
                    literal
                    for literal in used_literals
                    if literal.strip().lower() not in allowed_lower
                    and literal.strip().lower() not in user_literal_values_lower
                )
                if unexpected_literals:
                    _append_semantic_issue(
                        output,
                        f"Filter on `{column.table_name}.{column.column_name}` uses literal(s) "
                        f"{unexpected_literals}, but mapper confirmed stored value(s) {sorted(allowed_values)}. "
                        "Use exact encoded/sample values from mapper instead of semantic labels.",
                    )

        for join in mapper_output.joins:
            left_table = join.left_table.lower()
            right_table = join.right_table.lower()
            if left_table not in sql_lower or right_table not in sql_lower:
                _append_semantic_issue(
                    output,
                    f"Missing mapper-required join between `{join.left_table}` and `{join.right_table}`.",
                )

    mapper_text = _mapper_contract_text(state).lower()
    question_text = state.raw_question.lower()
    question_and_mapper = f"{question_text}\n{mapper_text}"

    if "abnormal" in question_text and re.search(r"\b(?:cre|creatinine)\b", question_text, re.IGNORECASE):
        for match in _CREATININE_THRESHOLD_RE.finditer(sql_query):
            threshold = match.group(1) or match.group(2)
            if threshold and threshold.lower() not in question_and_mapper:
                _append_semantic_issue(
                    output,
                    f"SQL hardcodes creatinine threshold `{threshold}` without mapper/question evidence. "
                    "Use documented schema/reference thresholds or request another generation with mapper evidence.",
                )

    if _CURRENT_AGE_PHRASE_RE.search(state.raw_question) and "birthday" in sql_lower:
        uses_current_date = any(token in sql_lower for token in ("current_date", "current_timestamp", "now()"))
        if not uses_current_date:
            _append_semantic_issue(
                output,
                "Age constraint appears to use a measurement date instead of current age. "
                "Phrases like `aren't 70 yet` should use current date/current timestamp.",
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
    with validator.sequential_tool_calls():  # pyright: ignore[reportDeprecated]
        result = await validator.run(
            f"Validate this SQL query:\n\n{sql_query}", deps=state, usage_limits=VALIDATOR_USAGE_LIMITS
        )
    output = result.output
    _apply_intent_semantic_guards(state, output)
    _remove_physical_tuning_feedback(output)

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
