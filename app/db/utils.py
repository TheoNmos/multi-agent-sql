from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import asyncpg


async def execute(conn: asyncpg.Connection, sql: str, *args: Any) -> str:
    return await conn.execute(sql, *args)


async def fetch_query(conn: asyncpg.Connection, sql: str, *args: Any) -> Sequence[asyncpg.Record]:
    return await conn.fetch(sql, *args)


async def check_query_valid(conn: asyncpg.Connection, sql: str) -> None:
    # Remove stray backslashes sometimes emitted by LLMs
    sql = sql.replace("\\", "")
    if not sql.strip().upper().startswith("SELECT"):
        raise ValueError("Only SELECT queries are allowed during validation")
    await conn.execute(f"EXPLAIN {sql}")


async def explain_json(conn: asyncpg.Connection, sql: str) -> Any:
    sql = sql.replace("\\", "")
    if not sql.strip().upper().startswith("SELECT"):
        raise ValueError("Only SELECT queries are allowed during explain")
    # Using FORMAT JSON to return a JSON plan
    rows = await conn.fetch(f"EXPLAIN (FORMAT JSON) {sql}")
    # asyncpg returns a Record with a single column 'QUERY PLAN' that is JSON
    # Normalize as Python object if available, else raw text
    if rows and len(rows[0]) == 1:
        return rows[0][0]
    return [dict(r) for r in rows]


async def list_tables(conn: asyncpg.Connection) -> list[str]:
    result = await conn.fetch(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public' ORDER BY 1"
    )
    if not result:
        return []
    return [row["table_name"] for row in result]


async def list_primary_keys(conn: asyncpg.Connection) -> dict[str, set[str]]:
    """List primary keys by table."""
    rows = await conn.fetch(
        """
        SELECT
            tc.table_name,
            kcu.column_name
        FROM information_schema.table_constraints tc
        JOIN information_schema.key_column_usage kcu
            ON tc.constraint_name = kcu.constraint_name
            AND tc.table_schema = kcu.table_schema
        WHERE tc.constraint_type = 'PRIMARY KEY'
            AND tc.table_schema = 'public'
        ORDER BY tc.table_name, kcu.ordinal_position
        """
    )
    result: dict[str, set[str]] = {}
    for row in rows:
        table = row["table_name"]
        col = row["column_name"]
        if table not in result:
            result[table] = set()
        result[table].add(col)
    return result


async def list_foreign_keys(conn: asyncpg.Connection) -> list[dict[str, str]]:
    """List foreign key relationships."""
    rows = await conn.fetch(
        """
        SELECT
            tc.table_name AS src_table,
            kcu.column_name AS src_col,
            ccu.table_name AS dst_table,
            ccu.column_name AS dst_col
        FROM information_schema.table_constraints tc
        JOIN information_schema.key_column_usage kcu
            ON tc.constraint_name = kcu.constraint_name
            AND tc.table_schema = kcu.table_schema
        JOIN information_schema.constraint_column_usage ccu
            ON ccu.constraint_name = tc.constraint_name
            AND ccu.table_schema = tc.table_schema
        WHERE tc.constraint_type = 'FOREIGN KEY'
            AND tc.table_schema = 'public'
        ORDER BY tc.table_name, kcu.ordinal_position
        """
    )
    return [
        {
            "src_table": row["src_table"],
            "src_col": row["src_col"],
            "dst_table": row["dst_table"],
            "dst_col": row["dst_col"],
        }
        for row in rows
    ]


async def sample_distinct(conn: asyncpg.Connection, table: str, column: str, limit: int = 20) -> list[Any]:
    """Sample distinct values from a column (bounded)."""
    try:
        rows = await conn.fetch(
            f'SELECT DISTINCT "{column}" FROM "{table}" WHERE "{column}" IS NOT NULL LIMIT $1',
            limit,
        )
        return [row[column] for row in rows]
    except Exception:
        return []
