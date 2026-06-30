from __future__ import annotations

from typing import Any

from app.db.adapter import DatabaseAdapter


async def execute(adapter: DatabaseAdapter, sql: str, *args: Any) -> str:
    return await adapter.execute(sql, *args)


async def fetch_query(adapter: DatabaseAdapter, sql: str, *args: Any) -> list[dict[str, Any]]:
    return await adapter.fetch(sql, *args)


async def check_query_valid(adapter: DatabaseAdapter, sql: str) -> None:
    sql = sql.replace("\\", "")
    if not sql.strip().upper().startswith("SELECT"):
        raise ValueError("Only SELECT queries are allowed during validation")
    valid, error = await adapter.validate_sql(sql)
    if not valid:
        raise ValueError(error or "Invalid SQL")


async def explain_json(adapter: DatabaseAdapter, sql: str) -> Any:
    sql = sql.replace("\\", "")
    if not sql.strip().upper().startswith("SELECT"):
        raise ValueError("Only SELECT queries are allowed during explain")
    plan = await adapter.explain(sql)
    return plan


async def list_tables(adapter: DatabaseAdapter) -> list[str]:
    return await adapter.list_tables()


async def sample_distinct(
    adapter: DatabaseAdapter, table: str, column: str, limit: int = 20
) -> list[Any]:
    """Sample distinct values from a column (bounded)."""
    return await adapter.sample_values(table, column, limit)
