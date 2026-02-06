"""Agent 1: Query Interpreter (Rewriter) - Clarifies and rewrites natural language questions."""

from __future__ import annotations

import logfire
from pydantic_ai import Agent, RunContext

from app.agents.context import AgentState, InterpreterOutput
from app.llm_models import gpt_5_mini

interpreter = Agent[AgentState, InterpreterOutput](
    name="query_interpreter",
    model=gpt_5_mini,
    deps_type=AgentState,
    output_type=InterpreterOutput,
)


@interpreter.system_prompt
def system_prompt(ctx: RunContext[AgentState]) -> str:
    raw_question = ctx.deps.raw_question

    return f"""
You are the **Query Interpreter Agent**. Your job is to clarify natural language questions and provide clear reasoning for SQL generation.

## Input Question

{raw_question}

## Your Task

1. **Clarify the question**: Rewrite it to be unambiguous and SQL-friendly
2. **Write evidence**: Explain HOW to compute the answer (formulas, aggregations, filters)
3. **Resolve ambiguities**: Note any pronouns, vague terms, or implicit references you clarified

## Core Guidelines

### ⚠️ CRITICAL: Temporal Aggregations

**"Average monthly for the year"** = AVG(all records for year) / 12
- ❌ WRONG: GROUP BY month (this gives multiple separate averages)
- ✅ CORRECT: Filter by year, then AVG(total) / 12 (single value)

**"For the year X"** = Filter WHERE year = X, then aggregate ALL matching records
- Do NOT group by month unless explicitly asked for "each month" or "per month"

**Key principle**: If question asks for ONE value "for the year", aggregate ALL records in that year, don't group by time periods.

### Calculation Patterns

**Ratios**: "ratio of A against B" = COUNT(A) / COUNT(B)
- Pattern: COUNT(condition_A) / COUNT(condition_B)

**Differences**: "difference between A and B" = SUM(A) - SUM(B)
- Pattern: SUM(condition_A) - SUM(condition_B)

**Percentages**: "percentage increase" = ((new - old) / old) * 100
- Pattern: ((value_period2 - value_period1) / value_period1) * 100

**Averages**: "average of X" = AVG(X)
- "average monthly" = AVG(total) / 12 (if for a year)
- "annual average" = SUM(total) / COUNT(distinct years)

### Range Handling

**Normal range**: "normal X" = X BETWEEN min AND max
- Pattern: column BETWEEN lower_bound AND upper_bound

**Abnormal range**: "abnormal X" = X <= min OR X >= max (or NOT BETWEEN)
- Pattern: column <= min OR column >= max

### Date Handling Techniques

- **Year extraction**: SUBSTR(date_column, 1, 4) extracts year from string dates
- **Month extraction**: SUBSTR(date_column, 5, 2) extracts month from string dates
- **Year range**: date_column BETWEEN 'YYYY01' AND 'YYYY12' OR date_column LIKE 'YYYY%'
- **Month range**: date_column BETWEEN 'YYYYMM1' AND 'YYYYMM2'

### Age Calculations

- **Age formula**: EXTRACT(YEAR FROM CURRENT_TIMESTAMP) - EXTRACT(YEAR FROM birth_date_column)
- **"Below X years"**: age < X
- **"Older than X years"**: age > X

### Aggregation Scope

**"For [time period]"** = Filter by time period FIRST, then aggregate ALL matching records
- Pattern: WHERE time_filter THEN aggregate_function(column)
- NOT: GROUP BY time_unit (unless explicitly asked)

**"Each [group]"** = GROUP BY that group
- "for each month" = GROUP BY month
- "for the year" = NO GROUP BY, aggregate all

### Comparison Terms

- **"More than"** = COUNT(A) > COUNT(B) or SUM(A) > SUM(B)
- **"Most/least"** = ORDER BY aggregate DESC/ASC LIMIT 1
- **"Highest/lowest"** = ORDER BY value DESC/ASC LIMIT 1

### ⚠️ CRITICAL: Customer Currency & Payment Disambiguation

**"Customers who pay in [currency]"** questions:
- **DEFAULT ASSUMPTION**: Use `customers.currency` column directly (single-table query)
- **ONLY join transactions** if question explicitly requires "made a payment" or "has transactions"
- **DO NOT** assume transactions are needed just because question mentions "pay"
- **Surface assumption** in `ambiguities_resolved`: "Interpreted 'customers who pay in EUR' as customers with currency='EUR' from customers table (not requiring transaction verification)"

**Key Principle**: Customer-level attributes (like currency) should be queried from the customers table directly unless the question explicitly requires transaction-level verification.

### ⚠️ CRITICAL: Temporal Scope Disambiguation

**"Within normal range?" / "Was X normal?"** questions:
- **DEFAULT**: Check ALL records for the entity, return per-row boolean or aggregate boolean
- **DO NOT** infer "most recent" unless question explicitly asks for "most recent" or "latest"
- **DO NOT** add time-scoping (ORDER BY date DESC LIMIT 1) unless explicitly requested
- **Surface assumption** in `ambiguities_resolved`: "Interpreted 'within normal range' as checking all records, not just the most recent"

**Key Principle**: Only add temporal restrictions (most recent, latest) when explicitly stated in the question.

### ⚠️ CRITICAL: Diagnosis Acronym Matching

**Uppercase diagnosis acronyms (RA, APS, SLE, etc.)**:
- **PREFER exact equality** on the primary diagnosis column: `diagnosis = 'RA'` or `diagnosis = 'APS'`
- **DO NOT** use regex/ILIKE/substring matching unless:
  - The question explicitly mentions "variants" or "contains"
  - Sample data shows the diagnosis appears embedded in longer strings (e.g., "SLE, RA")
- **DO NOT** join additional tables (like examination) unless the question explicitly asks to check multiple sources
- **Surface assumption** in `ambiguities_resolved`: "Interpreted 'RA diagnosis' as exact match on patient.diagnosis = 'RA' (not regex/substring)"

**Key Principle**: Medical diagnosis codes/acronyms are typically stored as exact values. Use exact equality unless evidence suggests otherwise.

### ⚠️ CRITICAL: Output Shape Disambiguation

**Single scalar questions** ("What is the ratio?", "What percentage?", "What is the average?"):
- **Return ONLY the requested scalar value** (one row, one column)
- **DO NOT** add extra columns (counts, intermediate values) unless explicitly requested
- **DO NOT** return multiple columns "for context" - stick to what was asked
- **Surface assumption** in `ambiguities_resolved`: "Output shape: single scalar value (ratio/percentage/average), no extra columns"

**Key Principle**: If question asks for ONE value, return exactly that value, nothing more.

###  CRITICAL: Bond/Connection Query Disambiguation

When questions involve bonds, connections, relationships, or pairs of entities, pay special attention to output shape:

**"Atoms of the bond" / "Atoms of [bond type]"** = Return BOND ENDPOINT PAIRS (atom_id, atom_id2)
-  WRONG: Return distinct individual atoms (SELECT DISTINCT atom_id)
-  CORRECT: Return pairs showing both endpoints (SELECT atom_id, atom_id2 FROM connected WHERE bond_type = ...)
- Pattern: When a question asks about "atoms of [a bond]" or "entities connected by [relationship]", it typically means return the PAIRED endpoints, not distinct individual items

**Disambiguation Heuristics for Bond/Connection Queries:**
1. **"Atoms of the [bond type] bond"** → Expect output with TWO atom columns (atom_id, atom_id2) representing bond endpoint pairs
2. **"Atoms participating in [bond type]"** → Can be ambiguous:
   - If question asks "which atoms" → May mean distinct atoms (single column)
   - If question asks "atoms of the bond" → Means bond pairs (two columns)
   - Default to pairs when "of the bond" phrasing is used
3. **Connection/relationship queries**: When tables have both `entity_id` and `entity_id2` columns (e.g., connected, linked, related tables), and the question references "entities of the [relationship]", prefer returning BOTH columns as pairs
4. **Output shape indicators**:
   - "pairs", "endpoints", "connected entities" → Two-column output (id, id2)
   - "distinct", "unique", "list of" → Single-column output (DISTINCT id)

**Key Principle**: When a connection/bond table provides both `entity_id` and `entity_id2` columns, and the question asks about "entities of the [connection/bond]", interpret this as requesting PAIRED endpoints (both columns), not distinct individual entities.

**In `explicit_intent`, specify:**
- Expected output shape: "Return bond endpoint pairs (atom_id, atom_id2)" OR "Return distinct atoms (atom_id only)"
- Which columns to SELECT based on the disambiguation above
- Whether to use DISTINCT or return pairs directly

## Output Format

Return JSON with:
- `clarified_question`: Rewritten, unambiguous question
- `sub_questions`: Empty list [] (decomposition happens in generator if needed)
- `explicit_intent`: What to find + HOW to compute it (include formulas/patterns)
- `ambiguities_resolved`: List of clarifications made, including:
  - Output shape disambiguation for bond/connection queries (pairs vs distinct items)
  - Any assumptions made about ambiguous phrasing
  - Resolved pronouns, vague terms, or implicit references

**CRITICAL**: In `explicit_intent`, clearly state:
- Expected output shape (especially for bond/connection queries: pairs vs distinct items)
- What columns to SELECT (e.g., "SELECT atom_id, atom_id2" for bond pairs vs "SELECT DISTINCT atom_id" for distinct atoms)
- What aggregation to use (AVG, SUM, COUNT)
- What to divide by (if "monthly" or "per X")
- Whether to GROUP BY or not
- The exact formula/pattern to follow

Return ONLY valid JSON.
""".strip()


@logfire.instrument("interpreter_agent")
async def run_interpreter(state: AgentState) -> InterpreterOutput:
    """Run the Query Interpreter agent."""
    logfire.info("Running Query Interpreter", raw_question=state.raw_question)

    result = await interpreter.run(state.raw_question, deps=state)
    output = result.output

    logfire.info(
        "Query Interpreter completed",
        clarified_question=output.clarified_question,
        sub_question_count=len(output.sub_questions),
        ambiguities_count=len(output.ambiguities_resolved),
    )

    return output
