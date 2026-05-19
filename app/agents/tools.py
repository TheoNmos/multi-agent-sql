"""Database tools for agents to interact with the active database.

These helpers delegate to a :class:`DatabaseAdapter` so the same agent code
can run against PostgreSQL or MySQL.
"""

from __future__ import annotations

import re
from typing import Any

import logfire
from rapidfuzz import fuzz

from app.db.adapter import DatabaseAdapter


def clean_sql(sql: str) -> str:
    """Clean SQL query by removing trailing newlines, escaped newlines, and everything after semicolon."""
    if not sql:
        return sql

    sql = sql.replace("\\n", " ").replace("\\r", " ")
    sql = sql.replace("\n", " ").replace("\r", " ")
    if ";" in sql:
        sql = sql.split(";")[0]
    sql = sql.strip()
    sql = re.sub(r"\s+", " ", sql)

    return sql


async def search_tables(adapter: DatabaseAdapter, keywords: list[str] | str) -> list[str]:
    """Search for table names matching keywords (case-insensitive substring match)."""
    if isinstance(keywords, str):
        keywords = [keywords]

    keyword_lowers = [k.lower() for k in keywords if k]

    tables = await adapter.list_tables()
    matches_set: set[str] = set()

    for table in tables:
        table_lower = table.lower()
        for keyword_lower in keyword_lowers:
            if keyword_lower in table_lower:
                matches_set.add(table)
                break

    matches = sorted(matches_set)[:50]
    logfire.debug("Table search completed", keywords=keywords, match_count=len(matches))
    return matches


async def get_table_info(
    adapter: DatabaseAdapter, table_names: list[str] | str
) -> dict[str, dict[str, Any]]:
    """Get detailed information about one or more tables."""
    result = await adapter.get_table_info(table_names)
    logfire.debug(
        "Table info retrieved",
        table_count=len(result),
        table_names=list(result.keys()),
        total_columns=sum(len(info["columns"]) for info in result.values()),
        total_fks=sum(len(info["foreign_keys"]) for info in result.values()),
        tables_with_samples=sum(1 for info in result.values() if info.get("sample_row") is not None),
    )
    return result


async def sample_values(
    adapter: DatabaseAdapter, table_name: str, column_name: str, limit: int = 10
) -> list[Any]:
    """Get sample distinct values from a column."""
    values = await adapter.sample_values(table_name, column_name, limit)
    logfire.debug(
        "Sample values retrieved", table=table_name, column=column_name, count=len(values)
    )
    return values


async def search_column_values(
    adapter: DatabaseAdapter,
    table_name: str,
    column_name: str,
    keyword: str,
    limit: int = 10,
) -> list[Any]:
    """Search for specific values in a column using LIKE pattern matching."""
    values = await adapter.search_column_values(table_name, column_name, keyword, limit)
    logfire.debug(
        "Column values searched",
        table=table_name,
        column=column_name,
        keyword=keyword,
        count=len(values),
    )
    return values


async def validate_sql_syntax(adapter: DatabaseAdapter, sql: str) -> tuple[bool, str | None]:
    """Validate SQL syntax via the adapter's dialect-specific EXPLAIN."""
    return await adapter.validate_sql(clean_sql(sql))


async def get_query_plan(adapter: DatabaseAdapter, sql: str) -> dict[str, Any] | None:
    """Get query execution plan using the adapter's dialect-specific EXPLAIN."""
    return await adapter.explain(clean_sql(sql))


async def execute_sql_safe(
    adapter: DatabaseAdapter, sql: str, limit: int = 50
) -> tuple[bool, list[dict[str, Any]] | None, str | None]:
    """Execute SQL query safely with a row limit and return results."""
    sql_clean = clean_sql(sql)
    success, results, error = await adapter.execute_sql_safe(sql_clean, limit)
    if success:
        logfire.debug("SQL executed safely", row_count=len(results or []))
    return success, results, error


async def search_columns(
    adapter: DatabaseAdapter,
    keywords: list[str] | str,
    table_hint: str | None = None,
) -> list[dict[str, Any]]:
    """Search for columns across tables by keyword(s)."""
    if isinstance(keywords, str):
        keywords = [keywords]

    keyword_lowers = [k.lower() for k in keywords if k]

    tables = await adapter.list_tables()
    if table_hint:
        hint_lower = table_hint.lower()
        tables = [t for t in tables if hint_lower in t.lower()][:20]
    else:
        tables = tables[:50]

    candidates: list[tuple[str, str, float]] = []
    seen_columns: set[tuple[str, str]] = set()

    for table in tables:
        try:
            column_names = await adapter.list_column_names(table)

            for col_name in column_names:
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

    candidates.sort(key=lambda x: x[2], reverse=True)

    result = [
        {"table": table, "column": col, "score": score, "full_name": f"{table}.{col}"}
        for table, col, score in candidates[:30]
    ]

    logfire.debug("Column search completed", keywords=keywords, match_count=len(result))
    return result
