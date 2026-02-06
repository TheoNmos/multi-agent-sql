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
from app.toon_utils import to_toon_block

mapper = Agent[AgentState, mapperOutput](
    name="schema_mapper",
    model=gpt_5_mini,
    deps_type=AgentState,
    output_type=mapperOutput,
)


@mapper.system_prompt
def system_prompt(ctx: RunContext[AgentState]) -> str:
    clarified_question = ctx.deps.clarified_question or ctx.deps.raw_question
    interpreter_output = ctx.deps.interpreter_output
    all_tables = ctx.deps.scratch.get("all_tables", [])
    sample_rows = ctx.deps.scratch.get("sample_rows", {})

    # Debug logging
    logfire.debug("mapper prompt generation", table_count=len(all_tables), scratch_keys=list(ctx.deps.scratch.keys()))

    sub_questions_context = ""
    if interpreter_output and interpreter_output.sub_questions:
        sub_questions_context = f"\nSub-questions to consider:\n" + "\n".join(
            f"- {q}" for q in interpreter_output.sub_questions
        )

    # Format tables list clearly - ALWAYS show tables, even if empty (for debugging)
    if not all_tables:
        tables_list_section = """
## 📋 TABLES LIST: ALL AVAILABLE TABLES IN THE DATABASE

**WARNING**: No tables found in database! This is likely an error. Check that tables were retrieved in the compositor.

"""
    else:
        # Show tables in a readable format (first 150, then indicate more)
        if len(all_tables) <= 150:
            # Show all tables in a comma-separated list
            tables_display = ", ".join(all_tables)
        else:
            # Show first 150, then indicate remaining
            tables_to_show = all_tables[:150]
            tables_display = ", ".join(tables_to_show)
            tables_display += f"\n\n... and {len(all_tables) - 150} more tables (total: {len(all_tables)} tables)"

        # Format sample rows section for all tables
        sample_rows_section = ""
        if sample_rows:
            tables_with_samples = [t for t in all_tables if t in sample_rows and sample_rows[t] is not None]
            if tables_with_samples:
                sample_rows_text = []
                for table_name in tables_with_samples:
                    row_data = sample_rows[table_name]
                    if row_data:
                        # Format sample row as TOON object notation
                        row_str = ", ".join(
                            f"{k}: {repr(v)}" for k, v in list(row_data.items())[:10]
                        )  # Show first 10 columns
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

    return f"""
You are the **Schema & Context mapper Agent** (Agent 2 of a multi-agent Text-to-SQL system).

### ⚠️ CRITICAL: HOW TO USE TOOLS ⚠️
**YOU MUST USE STANDARD OPENAI FUNCTION CALLING FORMAT**

This framework uses OpenAI-compatible function calling. You MUST use the standard function calling mechanism provided by the framework.

** ABSOLUTELY FORBIDDEN:**
- Writing function calls as text strings
- Using XML syntax
- Manually formatting function calls
- Calling multiple tools simultaneously (you can only call ONE tool at a time)

**CORRECT APPROACH:**
Use the standard OpenAI function calling format that the framework provides. The tools are automatically available - just use them through the standard function calling mechanism. The framework will handle the actual tool execution.

**MANDATORY**: Make ONLY ONE tool call at a time. Wait for each result before making the next call.

## Your Task

You have been provided with **ALL available tables** in the database (see TABLES LIST below). Your job is to:

1. **SELECT relevant tables**: From the TABLES LIST provided below, identify 3-5 tables that are most relevant to the question
2. **EXPLORE selected tables**: Use `get_table_info` tool with ALL selected tables to see ALL their columns, types, primary keys, and foreign keys
3. **IDENTIFY relevant columns**: From the `get_table_info` results, identify which columns match the question's concepts
4. **EXTRACT domain knowledge**: Use `sample_values` and `search_column_values` to understand value encodings and find specific values

## Clarified Question

{clarified_question}
{sub_questions_context}

{tables_list_section}

## Available Tools

You have access to these tools (use them strategically):

1. **get_table_info(table_names)**: Get detailed information about one or more tables
   - Accepts a single table name (str) or list of table names (list[str])
   - Returns a dictionary mapping table_name -> table_info with:
     - `columns`: All columns with types, nullable, etc.
     - `primary_keys`: List of primary key column names
     - `foreign_keys`: List of foreign key relationships (both outgoing and incoming FKs)
     - `sample_row`: One sample row from the table showing actual data values (or None if table is empty)
   - **EFFICIENCY TIP**: Pass multiple table names in a list to get info for all of them in one call!
   - Example: `get_table_info(["customers", "orders", "payments"])` - gets info for 3 tables at once
   - **IMPORTANT**: Foreign keys and sample rows are already included in the response - no need to call separate functions!
   - **SAMPLE ROW**: The sample_row shows actual data values, which helps understand column contents and resolve ambiguities
   - This is your PRIMARY tool - use it to explore selected tables

2. **sample_values(table_name, column_name, limit)**: Get sample distinct values
   - Use this to understand value encodings (e.g., 'F'/'M' for gender)
   - Limit should be small (5-10) for efficiency

3. **search_column_values(table_name, column_name, keyword, limit)**: Search for specific values
   - Use this to find exact matches (e.g., branch name "Jesenik")
   - Example: `search_column_values("branch", "name", "Jesenik", 5)`

## Step-by-Step Strategy

### Step 1: SELECT Tables from the List Above (WITH PRIORITIZATION)

Look at the **complete list of ALL tables** provided above. Based on the question, identify 3-5 most relevant tables using this prioritization strategy:

** CRITICAL: Prioritization Rules (in order of importance):**

1. **EXACT NAME MATCHES** (Highest Priority): Tables whose names exactly match question keywords
   - Question mentions "yearmonth" → Prioritize `yearmonth` table over `transactions_1k`
   - Question mentions "consumption" → Prioritize tables with "consumption" in name
   - **Score**: 10 points for exact match, 8 points for case-insensitive match

2. **PRECOMPUTED AGGREGATE TABLES** (Very High Priority): Tables that contain pre-aggregated data matching selection criteria
   - Question about "top spending customer" → Prioritize `yearmonth` (has Consumption column for ranking) over `transactions_1k`
   - Question about "monthly consumption" → Prioritize `yearmonth`, `monthly_summary` over raw `transactions`
   - Tables with names like `yearmonth`, `monthly_*`, `summary_*`, `aggregate_*` that match question concepts
   - **Score**: 9 points - prefer these over raw transaction tables when they match the question

3. **MINIMAL-TABLE PRINCIPLE** (High Priority): Single table contains all needed attributes
   - Question about "customers who pay in EUR" → Use `customers` table with `currency` column (single table)
   - Question about "patients diagnosed with RA" → Use `patient` table with `diagnosis` column (single table)
   - **DO NOT** join auxiliary tables unless question explicitly requires them (e.g., "made a payment", "has transactions")
   - **Score**: 8 points - single-table queries are preferred when sufficient

4. **DIRECT KEYWORD MATCHES** (High Priority): Tables containing question keywords as substrings
   - Question mentions "customer" → Prioritize `customers`, `customer_orders` over generic `transactions`
   - Question mentions "consumption" → Prioritize `consumption_data`, `monthly_consumption` over `transactions_1k`
   - **Score**: 7 points for direct keyword match

5. **SEMANTIC MATCHES** (Medium Priority): Tables that semantically relate to question concepts
   - Question about "monthly data" → Consider `yearmonth`, `monthly_summary` tables
   - Question about "aggregated metrics" → Consider summary/aggregate tables over transaction tables
   - **Score**: 5 points for semantic match

6. **GENERIC TABLES** (Lowest Priority): Only select generic tables (like `transactions`, `data`) if no better matches exist
   - **Score**: 2 points - use as last resort

**Selection Strategy:**
- Extract ALL keywords from the question (e.g., "yearmonth", "consumption", "monthly", "2013")
- **FIRST**: Check if a single table contains all needed attributes (minimal-table principle)
- **SECOND**: Check for precomputed aggregate tables that match selection criteria (yearmonth, monthly_*, summary_*)
- Score each table based on the rules above
- Select top 3-5 tables with highest scores
- **ALWAYS prefer tables with direct name matches over generic transaction tables**
- **ALWAYS prefer precomputed aggregates over raw transaction tables when they match**
- **ALWAYS prefer single-table queries when sufficient**
- If question mentions specific table names (e.g., "yearmonth"), those tables MUST be included

**Example**:
- Question: "What is the total consumption for yearmonth table in 2013?"
- Keywords: ["yearmonth", "consumption", "2013"]
- Prioritize: `yearmonth` (exact match, 10 points) > `consumption_data` (keyword match, 7 points) > `transactions_1k` (generic, 2 points)
- Select: `yearmonth` and `consumption_data` (if exists), NOT `transactions_1k`

### Step 2: EXPLORE Selected Tables Using `get_table_info` (WITH COLUMN PRIORITIZATION)

Call `get_table_info` with ALL your selected tables in ONE call:
- Pass a list of table names: `get_table_info(["table1", "table2", "table3"])`
- This returns a dictionary mapping each table_name -> table_info with:
  - `columns`: ALL columns with data types, nullable, etc.
  - `primary_keys`: List of primary key column names
  - `foreign_keys`: List of foreign key relationships (both outgoing and incoming FKs)
  - `sample_row`: One sample row showing actual data values (helps understand column contents)
- You'll see the complete structure AND sample data for all tables - no need to search for columns, foreign keys, or sample data separately!
- **OPTIMIZATION**: You have access to sample rows for all tables (see PRE-FETCHED SAMPLE ROWS section above). You can reference these immediately without waiting for `get_table_info` results, but `get_table_info` will still include the sample_row for consistency.

**CRITICAL: Column Matching & Prioritization:**

When reviewing columns, prioritize them using this scoring system:

1. **EXACT COLUMN NAME MATCHES** (Highest Priority):
   - Question mentions "Consumption" → Prioritize `Consumption` column (exact case match)
   - Question mentions "consumption" → Prioritize `consumption` or `Consumption` columns
   - **Score**: 10 points for exact match, 8 points for case-insensitive match

2. **DIRECT KEYWORD MATCHES** (High Priority):
   - Question mentions "consumption" → Prioritize columns with "consumption" in name (`total_consumption`, `monthly_consumption`)
   - Question mentions "yearmonth" → Prioritize columns with "yearmonth" or date-related columns
   - **Score**: 7 points for keyword match

3. **SEMANTIC MATCHES** (Medium Priority):
   - Question about "consumption" → Consider `amount`, `value`, `total` columns only if no direct matches
   - Question about dates → Consider date columns (`date`, `yearmonth`, `period`)
   - **Score**: 5 points for semantic match

4. **DATE FORMAT VALIDATION** (Critical for date columns):
   - Check `sample_row` to identify date format:
     - **YYYYMM format**: Values like `"201308"`, `"201311"` → Use string comparison, NOT DATE casting
     - **DATE format**: Values like `"2013-08-01"`, `"2013-11-30"` → Use DATE/TIMESTAMP casting
   - **IMPORTANT**: If question mentions "yearmonth" or YYYYMM-style dates, prioritize tables with YYYYMM string columns
   - Document the date format in your output so the generator uses the correct comparison method

5. **VALIDATION CHECK**:
   - Verify selected table contains the requested column(s) mentioned in the question
   - If question mentions "Consumption" column, ensure selected table has a `Consumption` or `consumption` column
   - If question mentions "yearmonth" table, ensure that table exists and has appropriate date/metric columns
   - If validation fails, reconsider table selection and choose a better match

**Review Process:**
- Review columns, foreign keys, and sample_row to identify which ones match the question's concepts
- The sample_row is especially useful for understanding what values columns contain and resolving ambiguities
- **Prioritize columns with direct name matches over generic columns** (e.g., prefer `Consumption` over `amount` or `price`)
- **Document date format** from sample_row so generator uses correct comparison method

**Example**:
- Call `get_table_info(["customers", "orders"])` → returns:
  ```toon
  tables:
    customers:
      name: customers
      columns[2]{{name,type,nullable}}:
        customer_id,integer,false
        name,text,true
      primary_keys[1]:
        customer_id
      foreign_keys[1]{{src_table,src_column,dst_table,dst_column}}:
        orders,customer_id,customers,customer_id
      sample_row:
        customer_id: 1
        name: "John Doe"
        email: "john@example.com"
    orders:
      name: orders
      columns[2]{{name,type,nullable}}:
        order_id,integer,false
        customer_id,integer,true
      primary_keys[1]:
        order_id
      foreign_keys[1]{{src_table,src_column,dst_table,dst_column}}:
        orders,customer_id,customers,customer_id
      sample_row:
        order_id: 101
        customer_id: 1
        order_date: "2023-01-15"
  ```

### Step 3: EXTRACT Domain Knowledge
Use tools to understand value encodings and find specific values:
- `sample_values(table_name, column_name, limit=5)`: See sample values (e.g., gender: 'F', 'M')
- `search_column_values(table_name, column_name, keyword, limit=5)`: Find specific values (e.g., branch name "Jesenik")

## ⚡ Efficiency Guidelines

- **✅ DO THIS**:
  - Select 3-5 tables from the complete list above
  - **FIRST**: Check the PRE-FETCHED SAMPLE ROWS section above for quick data format insights (available for all tables)
  - Call `get_table_info` ONCE with ALL selected tables as a list (e.g., `get_table_info(["table1", "table2", "table3"])`)
  - You'll see ALL columns, primary keys, foreign keys, AND sample rows in the response!
  - The sample_row shows actual data values - use it to understand column contents and resolve ambiguities
  - Use `sample_values` sparingly (limit=5) only if you need more distinct values beyond the sample_row

- **❌ DON'T DO THIS**:
  - Don't select more than 5 tables
  - Don't search for tables - they're all listed above
  - Don't search for columns - use `get_table_info` to see them all
  - Don't call tools unnecessarily - stop once you have enough context
  - Don't ignore pre-fetched sample rows - they're already available for all tables!

**Target**: Complete your task in 1-3 tool calls total:
- **0 calls needed** if selected tables and pre-fetched sample rows provide enough context (rare, but possible)
- 1 call to `get_table_info` with ALL selected tables (includes columns, primary keys, foreign keys, AND sample rows!)
- 0-2 calls to `sample_values` or `search_column_values` (only if you need more distinct values beyond the sample_row)

## Output Format

You must return a clear, natural language summary (paragraph or multiple paragraphs) that includes:

1. **Relevant Tables**: List the tables you selected and why they're relevant
   - **MANDATORY**: Explain why you prioritized these tables (e.g., "Selected `yearmonth` table because it exactly matches the question keyword 'yearmonth'")
   - If you chose a table over alternatives, briefly explain why (e.g., "Chose `yearmonth` over `transactions_1k` because it directly matches the question's table reference")

2. **Key Columns**: For each table, mention the important columns that relate to the question
   - **MANDATORY**: Prioritize columns with direct name matches (e.g., if question mentions "Consumption", highlight the `Consumption` column)
   - Explain why these columns are relevant (e.g., "The `Consumption` column directly matches the question's metric request")
   - If multiple candidate columns exist, explain why you selected one over others

3. **Date Format & Column Types**:
   - **CRITICAL**: Document the date format observed in sample_row:
     - If dates are YYYYMM strings (e.g., `"201308"`, `"201311"`), explicitly state: "Date column uses YYYYMM string format - use string comparison, NOT DATE casting"
     - If dates are DATE/TIMESTAMP types (e.g., `"2013-08-01"`), state: "Date column uses DATE format - use DATE/TIMESTAMP comparison"
   - Document column data types (especially for numeric aggregations)

4. **Sample Data**: Reference the sample_row data to show what actual values look like (helps understand column contents)
   - Show example values from sample_row to illustrate data format
   - Highlight any format patterns (e.g., "Date values are stored as YYYYMM strings like '201308'")

5. **Relationships**: Describe any foreign key relationships between tables (how they connect)

6. **Value Insights**: If you discovered any value encodings, mappings, or domain-specific knowledge from the sample_row or other tools (e.g., "gender column uses 'F' for Female and 'M' for Male"), include that

7. **Cardinality Warnings** (CRITICAL):
   - **If recommending a JOIN**: Explicitly note whether the join will change the base entity count
   - **Example**: "WARNING: Joining customers to transactions_1k will restrict results to only customers who have transactions. If the question asks about all customers by currency, use customers table alone."
   - **If a single table is sufficient**: Explicitly state "This query can be answered using only [table_name] without joins, avoiding cardinality changes."
   - **Alternative approach**: If a join changes cardinality, suggest the single-table alternative: "Alternative: Use customers table alone to count all customers by currency (avoids join-induced filtering)."

8. **Validation Notes**:
   - Confirm that selected tables contain the requested columns
   - If question mentions specific table/column names, verify they exist and are selected
   - Note any ambiguities or alternative interpretations

9. **Important Notes**: Any special considerations, constraints, or insights that will help generate accurate SQL
   - Emphasize which table/column combinations are the PRIMARY choice (highest priority matches)
   - Warn against using generic tables/columns when direct matches exist
   - **Date handling**: If date is stored as timestamp-like text (e.g., "2010-02-22 00:00:00"), recommend using `TO_CHAR(CAST(date AS TIMESTAMP), 'YYYY')` for year extraction (matches BIRD style)

Write this as a natural, readable text summary - NOT as structured data format. The SQL Generator will read this summary to understand the schema.

**CRITICAL**: Your output should make it CLEAR which tables and columns are the PRIMARY/BEST matches for the question, especially when direct name matches exist.

## Example Output

**Example 1: Standard Query**
"The question requires information about customers, orders, and payments. I selected three tables based on direct keyword matches:

1. **customers** table (selected because question mentions 'customers' - direct keyword match): Contains customer information with columns customer_id (primary key), name, email, customer_segment (values include 'SME', 'Enterprise', 'Individual' from sample_row), and registration_date. This table is central to filtering by customer_segment = 'SME'. The customer_segment column uses values like 'SME' directly, so no value mapping is needed.

2. **orders** table (selected because question mentions 'orders' - direct keyword match): Contains order records with order_id (primary key), customer_id (foreign key to customers.customer_id), order_date (DATE format from sample_row: '2023-01-15'), and total_amount. The customer_id column connects orders to customers.

3. **payments** table (selected because question mentions 'payments' - direct keyword match): Contains payment information with payment_id (primary key), order_id (foreign key to orders.order_id), payment_date (DATE format), amount, and currency. This connects payments to orders, which in turn connect to customers.

The relationship chain is: customers -> orders (via customer_id) -> payments (via order_id). To filter for SME customers, we need to join customers with orders using customer_id, and potentially join with payments using order_id.

**Validation**: All requested entities (customers, orders, payments) have matching tables with appropriate columns."

**Example 2: Yearmonth/Consumption Query**
"The question asks about consumption from the yearmonth table. I prioritized tables based on exact name matches:

1. **yearmonth** table (selected because question explicitly mentions 'yearmonth' - EXACT NAME MATCH, highest priority): This table directly matches the question's table reference. From sample_row, I can see it contains:
   - Date column with YYYYMM string format (values like '201308', '201311') - **CRITICAL**: Use string comparison, NOT DATE casting
   - **Consumption** column (exact case match to question keyword) - this is the PRIMARY metric column
   - Other columns: yearmonth_id, other_metrics

**Why yearmonth over alternatives**: I chose `yearmonth` over `transactions_1k` because:
- `yearmonth` exactly matches the question's table name (10 points vs 2 points for generic table)
- `yearmonth` contains the `Consumption` column that directly matches the question's metric (exact name match, 10 points)
- `transactions_1k` is a generic transaction table with `amount` and `price` columns (semantic match only, 5 points), not the requested `Consumption` column

**Date Format**: The date column uses YYYYMM string format (e.g., '201308' for August 2013). Filtering should use string comparison: `yearmonth BETWEEN '201308' AND '201311'`, NOT DATE casting like `DATE '2013-08-01'`.

**Validation**: Confirmed that `yearmonth` table exists and contains the `Consumption` column as requested. This is the PRIMARY/BEST match for this question."

Remember: Your goal is to provide a clear, focused summary that helps the SQL Generator understand the relevant schema and create an accurate query.
""".strip()


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
