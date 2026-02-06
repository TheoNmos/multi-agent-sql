"""Database tools for agents to interact with PostgreSQL."""

from __future__ import annotations

import re
from typing import Any

import asyncpg
import logfire
from rapidfuzz import fuzz


def clean_sql(sql: str) -> str:
    """
    Clean SQL query by removing trailing newlines, escaped newlines, and everything after semicolon.

    Args:
        sql: Raw SQL query string

    Returns:
        Cleaned SQL query string
    """
    if not sql:
        return sql

    # Remove escaped newlines (\n, \\n, etc.)
    sql = sql.replace("\\n", " ").replace("\\r", " ")
    # Remove actual newlines and carriage returns
    sql = sql.replace("\n", " ").replace("\r", " ")
    # Remove everything after semicolon (if present)
    if ";" in sql:
        sql = sql.split(";")[0]
    # Strip whitespace
    sql = sql.strip()
    # Normalize multiple spaces to single space
    sql = re.sub(r"\s+", " ", sql)

    return sql


async def search_tables(conn: asyncpg.Connection, keywords: list[str] | str) -> list[str]:
    """
    Search for table names matching keywords (case-insensitive, fuzzy matching).

    Args:
        conn: Database connection
        keywords: Single keyword string or list of keywords to search for

    Returns:
        List of matching table names (max 50 results)
    """
    if isinstance(keywords, str):
        keywords = [keywords]

    keyword_lowers = [k.lower() for k in keywords if k]

    # Get all tables from information_schema
    rows = await conn.fetch(
        """
        SELECT table_name
        FROM information_schema.tables
        WHERE table_schema = 'public'
        ORDER BY table_name
        """
    )

    tables = [row["table_name"] for row in rows]
    matches_set = set()

    for table in tables:
        table_lower = table.lower()
        for keyword_lower in keyword_lowers:
            if keyword_lower in table_lower:
                matches_set.add(table)
                break

    matches = sorted(matches_set)[:50]
    logfire.debug("Table search completed", keywords=keywords, match_count=len(matches))
    return matches


async def get_table_info(conn: asyncpg.Connection, table_names: list[str] | str) -> dict[str, dict[str, Any]]:
    """
    Get detailed information about one or more tables including columns, types, constraints, foreign keys, and a sample row.

    Args:
        conn: Database connection
        table_names: Single table name (str) or list of table names

    Returns:
        Dictionary mapping table_name -> table_info dict with keys: name, columns, primary_keys, foreign_keys, sample_row
        - sample_row: One sample row from the table (dict with column names as keys) or None if table is empty
    """
    # Normalize to list
    if isinstance(table_names, str):
        table_names = [table_names]

    if not table_names:
        return {}

    # Build query with IN clause for multiple tables
    placeholders = ",".join(f"${i + 1}" for i in range(len(table_names)))

    # Get columns for all tables
    logfire.info(f"Getting columns for tables: {table_names}")
    column_rows = await conn.fetch(
        f"""
        SELECT
            table_name,
            column_name,
            data_type,
            udt_name,
            is_nullable
        FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name IN ({placeholders})
        ORDER BY table_name
        """,
        *table_names,
    )
    logfire.info(f"Columns: {column_rows}")

    # Get primary keys for all tables
    pk_rows = await conn.fetch(
        f"""
        SELECT tc.table_name, kcu.column_name
        FROM information_schema.table_constraints tc
        JOIN information_schema.key_column_usage kcu
            ON tc.constraint_name = kcu.constraint_name
            AND tc.table_schema = kcu.table_schema
        WHERE tc.constraint_type = 'PRIMARY KEY'
            AND tc.table_schema = 'public'
            AND tc.table_name IN ({placeholders})
        ORDER BY tc.table_name
        """,
        *table_names,
    )

    # Get foreign keys for all tables (where src_table OR dst_table is in our list)
    num_tables = len(table_names)
    placeholders1 = ",".join(f"${i + 1}" for i in range(num_tables))
    placeholders2 = ",".join(f"${i + num_tables + 1}" for i in range(num_tables))
    fk_rows = await conn.fetch(
        f"""
        SELECT
            tc.table_name AS src_table,
            kcu.column_name AS src_column,
            ccu.table_name AS dst_table,
            ccu.column_name AS dst_column
        FROM information_schema.table_constraints AS tc
        JOIN information_schema.key_column_usage AS kcu
            ON tc.constraint_name = kcu.constraint_name
            AND tc.table_schema = kcu.table_schema
        JOIN information_schema.constraint_column_usage AS ccu
            ON ccu.constraint_name = tc.constraint_name
            AND ccu.table_schema = tc.table_schema
        WHERE tc.constraint_type = 'FOREIGN KEY'
            AND tc.table_schema = 'public'
            AND (tc.table_name IN ({placeholders1}) OR ccu.table_name IN ({placeholders2}))
        ORDER BY tc.table_name
        """,
        *table_names,
        *table_names,
    )

    # Build result dictionary
    result: dict[str, dict[str, Any]] = {}
    primary_keys_by_table: dict[str, list[str]] = {}
    foreign_keys_by_table: dict[str, list[dict[str, str]]] = {}

    # Initialize result dict for all requested tables
    for table_name in table_names:
        result[table_name] = {
            "name": table_name,
            "columns": [],
            "primary_keys": [],
            "foreign_keys": [],
        }
        primary_keys_by_table[table_name] = []
        foreign_keys_by_table[table_name] = []

    # Process primary keys
    for row in pk_rows:
        table_name = row["table_name"]
        column_name = row["column_name"]
        if table_name in primary_keys_by_table:
            primary_keys_by_table[table_name].append(column_name)

    # Process foreign keys - add to both src_table and dst_table if they're in our list
    for row in fk_rows:
        src_table = row["src_table"]
        dst_table = row["dst_table"]
        fk_info = {
            "src_table": src_table,
            "src_column": row["src_column"],
            "dst_table": dst_table,
            "dst_column": row["dst_column"],
        }
        # Add to src_table if it's in our list (outgoing FK)
        if src_table in foreign_keys_by_table:
            foreign_keys_by_table[src_table].append(fk_info)
        # Add to dst_table if it's in our list (incoming FK)
        if dst_table in foreign_keys_by_table:
            foreign_keys_by_table[dst_table].append(fk_info)

    # Process columns
    for row in column_rows:
        table_name = row["table_name"]
        if table_name in result:
            result[table_name]["columns"].append(
                {"name": row["column_name"], "type": row["data_type"], "nullable": row["is_nullable"] == "YES"}
            )

    # Set primary keys and foreign keys
    for table_name in table_names:
        if table_name in result:
            result[table_name]["primary_keys"] = primary_keys_by_table.get(table_name, [])
            result[table_name]["foreign_keys"] = foreign_keys_by_table.get(table_name, [])

    # Get one sample row for each table
    for table_name in table_names:
        if table_name in result:
            try:
                # Get column names for this table
                column_names = [col["name"] for col in result[table_name]["columns"]]
                if column_names:
                    # Build SELECT query with proper quoting
                    quoted_columns = ", ".join(f'"{col}"' for col in column_names)
                    sample_query = f'SELECT {quoted_columns} FROM "{table_name}" LIMIT 1'
                    sample_rows = await conn.fetch(sample_query)
                    if sample_rows:
                        # Convert row to dict, handling any special types
                        sample_row = dict(sample_rows[0])
                        # Convert any non-serializable types to strings
                        for key, value in sample_row.items():
                            if value is not None and not isinstance(value, (str, int, float, bool, type(None))):
                                sample_row[key] = str(value)
                        result[table_name]["sample_row"] = sample_row
                    else:
                        result[table_name]["sample_row"] = None
                else:
                    result[table_name]["sample_row"] = None
            except Exception as e:
                logfire.warning("Error fetching sample row", table=table_name, error=str(e))
                result[table_name]["sample_row"] = None

    logfire.debug(
        "Table info retrieved",
        table_count=len(result),
        table_names=table_names,
        total_columns=sum(len(info["columns"]) for info in result.values()),
        total_fks=sum(len(info["foreign_keys"]) for info in result.values()),
        tables_with_samples=sum(1 for info in result.values() if info.get("sample_row") is not None),
    )
    return result


async def sample_values(conn: asyncpg.Connection, table_name: str, column_name: str, limit: int = 10) -> list[Any]:
    """
    Get sample distinct values from a column.

    Args:
        conn: Database connection
        table_name: Name of the table
        column_name: Name of the column
        limit: Maximum number of samples to return

    Returns:
        List of sample values
    """
    try:
        # Use parameterized query with proper quoting
        query = f'SELECT DISTINCT "{column_name}" FROM "{table_name}" WHERE "{column_name}" IS NOT NULL LIMIT $1'
        rows = await conn.fetch(query, limit)
        values = [row[column_name] for row in rows]
        logfire.debug("Sample values retrieved", table=table_name, column=column_name, count=len(values))
        return values
    except Exception as e:
        logfire.warning("Error sampling values", table=table_name, column=column_name, error=str(e))
        return []


async def search_column_values(
    conn: asyncpg.Connection, table_name: str, column_name: str, keyword: str, limit: int = 10
) -> list[Any]:
    """
    Search for specific values in a column using LIKE pattern matching.

    Args:
        conn: Database connection
        table_name: Name of the table
        column_name: Name of the column
        keyword: Keyword to search for
        limit: Maximum number of results

    Returns:
        List of matching values
    """
    try:
        # Use parameterized query with proper quoting
        query = f'SELECT DISTINCT "{column_name}" FROM "{table_name}" WHERE "{column_name}"::text LIKE $1 LIMIT $2'
        pattern = f"%{keyword}%"
        rows = await conn.fetch(query, pattern, limit)
        values = [row[column_name] for row in rows]
        logfire.debug(
            "Column values searched", table=table_name, column=column_name, keyword=keyword, count=len(values)
        )
        return values
    except Exception as e:
        logfire.warning(
            "Error searching column values", table=table_name, column=column_name, keyword=keyword, error=str(e)
        )
        return []


async def validate_sql_syntax(conn: asyncpg.Connection, sql: str) -> tuple[bool, str | None]:
    """
    Validate SQL syntax using PostgreSQL EXPLAIN.

    Args:
        conn: Database connection
        sql: SQL query to validate

    Returns:
        Tuple of (is_valid, error_message)
    """
    sql_clean = clean_sql(sql)

    # Replace :parameter placeholders with NULL for syntax validation
    # PostgreSQL doesn't support :name syntax natively (uses $1, $2 instead)
    # We replace them with NULL so syntax validation can proceed
    sql_for_validation = re.sub(r":\w+", "NULL", sql_clean)

    try:
        await conn.execute(f"EXPLAIN {sql_for_validation}")
        return True, None
    except asyncpg.exceptions.PostgresSyntaxError as e:
        return False, f"Syntax error: {str(e)}"
    except asyncpg.exceptions.PostgresError as e:
        return False, f"SQL error: {str(e)}"
    except Exception as e:
        return False, f"Unexpected error: {str(e)}"


async def get_query_plan(conn: asyncpg.Connection, sql: str) -> dict[str, Any] | None:
    """
    Get query execution plan using EXPLAIN ANALYZE.

    Args:
        conn: Database connection
        sql: SQL query to analyze (should NOT include EXPLAIN prefix - it will be added automatically)

    Returns:
        Dictionary with query plan information or None if error
    """
    sql_clean = clean_sql(sql)
    # Strip any existing EXPLAIN prefix if the LLM accidentally added it
    # This handles cases where LLM prepends EXPLAIN ANALYZE before calling the tool
    sql_upper = sql_clean.upper().strip()
    if sql_upper.startswith("EXPLAIN"):
        # Use regex to find the actual SQL statement after EXPLAIN (with optional options)
        # Matches: EXPLAIN, EXPLAIN ANALYZE, EXPLAIN (FORMAT JSON), EXPLAIN (FORMAT JSON, ANALYZE), etc.
        # Find the first SQL keyword (SELECT, INSERT, UPDATE, DELETE, WITH, etc.)
        match = re.search(r"\b(SELECT|INSERT|UPDATE|DELETE|WITH|CREATE|ALTER|DROP)\b", sql_upper, re.IGNORECASE)
        if match:
            sql_clean = sql_clean[match.start() :].strip()
        else:
            # Fallback: if no SQL keyword found, try to remove just the EXPLAIN part
            # This shouldn't happen in practice, but handles edge cases
            sql_clean = re.sub(r"^EXPLAIN\s+(ANALYZE\s+)?(\([^)]+\)\s+)?", "", sql_clean, flags=re.IGNORECASE).strip()

    # Replace :parameter placeholders with NULL for query plan analysis
    # PostgreSQL doesn't support :name syntax natively (uses $1, $2 instead)
    sql_for_plan = re.sub(r":\w+", "NULL", sql_clean)

    try:
        rows = await conn.fetch(f"EXPLAIN (FORMAT JSON, ANALYZE) {sql_for_plan}")
        if rows and len(rows) > 0:
            plan_json = rows[0][0]
            if isinstance(plan_json, str):
                import json

                plan_json = json.loads(plan_json)
            plan = plan_json[0] if isinstance(plan_json, list) and len(plan_json) > 0 else plan_json
            plan_obj = plan.get("Plan", {}) if isinstance(plan, dict) else {}
            return {
                "total_cost": plan_obj.get("Total Cost", "N/A"),
                "plan_rows": plan_obj.get("Plan Rows", "N/A"),
                "actual_rows": plan_obj.get("Actual Rows", None),
                "plan": plan_obj,
            }
        return None
    except Exception as e:
        logfire.warning("Error getting query plan", error=str(e))
        return None


async def execute_sql_safe(
    conn: asyncpg.Connection, sql: str, limit: int = 50
) -> tuple[bool, list[dict[str, Any]] | None, str | None]:
    """
    Execute SQL query safely with a row limit and return results.

    Args:
        conn: Database connection
        sql: SQL query to execute
        limit: Maximum number of rows to return

    Returns:
        Tuple of (success, results_list_or_none, error_message_or_none)
    """
    sql_clean = clean_sql(sql)

    # Replace :parameter placeholders with NULL for execution
    # PostgreSQL doesn't support :name syntax natively (uses $1, $2 instead)
    sql_clean = re.sub(r":\w+", "NULL", sql_clean)

    # Add LIMIT if not present (for safety)
    sql_upper = sql_clean.upper()
    if "LIMIT" not in sql_upper:
        # Add LIMIT to the cleaned SQL
        sql_clean = f"{sql_clean} LIMIT {limit}"

    try:
        rows = await conn.fetch(sql_clean)
        results = [dict(row) for row in rows[:limit]]
        logfire.debug("SQL executed safely", row_count=len(results))
        return True, results, None
    except asyncpg.exceptions.PostgresSyntaxError as e:
        return False, None, f"Syntax error: {str(e)}"
    except asyncpg.exceptions.PostgresError as e:
        return False, None, f"SQL error: {str(e)}"
    except Exception as e:
        return False, None, f"Unexpected error: {str(e)}"


async def search_columns(
    conn: asyncpg.Connection, keywords: list[str] | str, table_hint: str | None = None
) -> list[dict[str, Any]]:
    """
    Search for columns across tables by keyword(s).

    Args:
        conn: Database connection
        keywords: Single keyword string or list of keywords
        table_hint: Optional table name hint to limit search

    Returns:
        List of matching columns with table, column, and relevance score
    """
    if isinstance(keywords, str):
        keywords = [keywords]

    keyword_lowers = [k.lower() for k in keywords if k]

    # Get tables to search
    if table_hint:
        tables_query = """
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema = 'public' AND LOWER(table_name) LIKE $1
            LIMIT 20
        """
        tables_rows = await conn.fetch(tables_query, f"%{table_hint.lower()}%")
        tables = [row["table_name"] for row in tables_rows]
    else:
        tables_query = """
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema = 'public'
            LIMIT 50
        """
        tables_rows = await conn.fetch(tables_query)
        tables = [row["table_name"] for row in tables_rows]

    candidates: list[tuple[str, str, float]] = []  # (table, column, score)
    seen_columns = set()

    for table in tables:
        try:
            column_rows = await conn.fetch(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = 'public' AND table_name = $1
                """,
                table,
            )

            for col_row in column_rows:
                col_name = col_row["column_name"]
                col_key = (table, col_name)
                if col_key in seen_columns:
                    continue
                seen_columns.add(col_key)

                col_lower = col_name.lower()
                best_score = 0.0
                for keyword_lower in keyword_lowers:
                    if keyword_lower == col_lower:
                        best_score = max(best_score, 100.0)
                    elif keyword_lower in col_lower:
                        score = fuzz.ratio(keyword_lower, col_lower)
                        if score > 50:
                            best_score = max(best_score, score)

                if best_score > 50:
                    candidates.append((table, col_name, best_score))
        except Exception as e:
            logfire.warning("Error searching columns in table", table=table, error=str(e))
            continue

    # Sort by score descending
    candidates.sort(key=lambda x: x[2], reverse=True)

    result = [
        {"table": table, "column": col, "score": score, "full_name": f"{table}.{col}"}
        for table, col, score in candidates[:30]
    ]

    logfire.debug("Column search completed", keywords=keywords, match_count=len(result))
    return result
