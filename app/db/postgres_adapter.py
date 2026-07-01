"""PostgreSQL adapter implemented on top of asyncpg."""

from __future__ import annotations

import json
from typing import Any, ClassVar, override

import asyncpg
import logfire

from app.db.adapter import (
    DatabaseAdapter,
    Dialect,
    ensure_limit,
    strip_explain_prefix,
    strip_named_placeholders,
)
from app.db.sql_guard import validate_select_only_sql
from app.db.value_sanitize import (
    fetch_table_sample_row,
    get_column_type,
    sanitize_result_rows,
    sanitize_value_list,
)


class PostgresAdapter(DatabaseAdapter):
    """Adapter that exposes asyncpg connections through the shared interface."""

    dialect: ClassVar[Dialect] = "postgres"

    def __init__(self, conn: asyncpg.Connection, database_name: str) -> None:
        super().__init__(database_name=database_name)
        self._conn = conn

    @property
    def raw_connection(self) -> asyncpg.Connection:
        return self._conn

    @override
    async def close(self) -> None:
        await self._conn.close()

    @override
    def quote_identifier(self, name: str) -> str:
        escaped = name.replace('"', '""')
        return f'"{escaped}"'

    @override
    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        rows = await self._conn.fetch(sql, *args)
        return [dict(row) for row in rows]

    @override
    async def execute(self, sql: str, *args: Any) -> str:
        return await self._conn.execute(sql, *args)

    @override
    async def fetchval(self, sql: str, *args: Any) -> Any:
        return await self._conn.fetchval(sql, *args)

    @override
    async def server_version(self) -> str:
        version = await self._conn.fetchval("SELECT version()")
        return str(version) if version is not None else ""

    @override
    async def list_tables(self) -> list[str]:
        rows = await self._conn.fetch(
            """
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema = 'public'
            ORDER BY table_name
            """
        )
        return [row["table_name"] for row in rows]

    @override
    async def list_column_names(self, table: str) -> list[str]:
        rows = await self._conn.fetch(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = $1
            ORDER BY ordinal_position
            """,
            table,
        )
        return [row["column_name"] for row in rows]

    @override
    async def get_table_info(
        self,
        table_names: list[str] | str,
        *,
        preloaded_sample_rows: dict[str, dict[str, Any] | None] | None = None,
    ) -> dict[str, dict[str, Any]]:
        if isinstance(table_names, str):
            table_names = [table_names]

        if not table_names:
            return {}

        placeholders = ",".join(f"${i + 1}" for i in range(len(table_names)))

        column_rows = await self._conn.fetch(
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

        pk_rows = await self._conn.fetch(
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

        num_tables = len(table_names)
        placeholders1 = ",".join(f"${i + 1}" for i in range(num_tables))
        placeholders2 = ",".join(f"${i + num_tables + 1}" for i in range(num_tables))
        fk_rows = await self._conn.fetch(
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

        result: dict[str, dict[str, Any]] = {}
        primary_keys_by_table: dict[str, list[str]] = {}
        foreign_keys_by_table: dict[str, list[dict[str, str]]] = {}

        for table_name in table_names:
            result[table_name] = {
                "name": table_name,
                "columns": [],
                "primary_keys": [],
                "foreign_keys": [],
            }
            primary_keys_by_table[table_name] = []
            foreign_keys_by_table[table_name] = []

        for row in pk_rows:
            table_name = row["table_name"]
            column_name = row["column_name"]
            if table_name in primary_keys_by_table:
                primary_keys_by_table[table_name].append(column_name)

        for row in fk_rows:
            src_table = row["src_table"]
            dst_table = row["dst_table"]
            fk_info = {
                "src_table": src_table,
                "src_column": row["src_column"],
                "dst_table": dst_table,
                "dst_column": row["dst_column"],
            }
            if src_table in foreign_keys_by_table:
                foreign_keys_by_table[src_table].append(fk_info)
            if dst_table in foreign_keys_by_table:
                foreign_keys_by_table[dst_table].append(fk_info)

        for row in column_rows:
            table_name = row["table_name"]
            if table_name in result:
                result[table_name]["columns"].append(
                    {
                        "name": row["column_name"],
                        "type": row["data_type"],
                        "nullable": row["is_nullable"] == "YES",
                    }
                )

        for table_name in table_names:
            if table_name in result:
                result[table_name]["primary_keys"] = primary_keys_by_table.get(table_name, [])
                result[table_name]["foreign_keys"] = foreign_keys_by_table.get(table_name, [])

        for table_name in table_names:
            if table_name not in result:
                continue
            cached_row = (preloaded_sample_rows or {}).get(table_name)
            if cached_row is not None:
                result[table_name]["sample_row"] = cached_row
                continue
            try:
                result[table_name]["sample_row"] = await fetch_table_sample_row(
                    self,
                    table_name,
                    result[table_name]["columns"],
                )
            except Exception as e:
                logfire.warning("Error fetching sample row", table=table_name, error=str(e))
                result[table_name]["sample_row"] = None

        return result

    @override
    async def sample_values(
        self,
        table: str,
        column: str,
        limit: int = 10,
        *,
        column_type_cache: dict[tuple[str, str], str | None] | None = None,
    ) -> list[Any]:
        try:
            if column_type_cache is not None:
                from app.db.schema_prefetch import get_column_type_cached

                col_type = await get_column_type_cached(self, table, column, column_type_cache)
            else:
                col_type = await get_column_type(self, table, column)
            query = (
                f"SELECT DISTINCT {self.quote_identifier(column)} "
                f"FROM {self.quote_identifier(table)} "
                f"WHERE {self.quote_identifier(column)} IS NOT NULL LIMIT $1"
            )
            rows = await self._conn.fetch(query, limit)
            raw_values = [row[column] for row in rows]
            return sanitize_value_list(raw_values, column_name=column, data_type=col_type)
        except Exception as e:
            logfire.warning("Error sampling values", table=table, column=column, error=str(e))
            return []

    @override
    async def search_column_values(
        self,
        table: str,
        column: str,
        keyword: str,
        limit: int = 10,
        *,
        column_type_cache: dict[tuple[str, str], str | None] | None = None,
    ) -> list[Any]:
        try:
            if column_type_cache is not None:
                from app.db.schema_prefetch import get_column_type_cached

                col_type = await get_column_type_cached(self, table, column, column_type_cache)
            else:
                col_type = await get_column_type(self, table, column)
            query = (
                f"SELECT DISTINCT {self.quote_identifier(column)} "
                f"FROM {self.quote_identifier(table)} "
                f"WHERE {self.quote_identifier(column)}::text LIKE $1 LIMIT $2"
            )
            pattern = f"%{keyword}%"
            rows = await self._conn.fetch(query, pattern, limit)
            raw_values = [row[column] for row in rows]
            return sanitize_value_list(raw_values, column_name=column, data_type=col_type)
        except Exception as e:
            logfire.warning(
                "Error searching column values",
                table=table,
                column=column,
                keyword=keyword,
                error=str(e),
            )
            return []

    @override
    async def validate_sql(self, sql: str) -> tuple[bool, str | None]:
        is_allowed, guard_error = validate_select_only_sql(sql)
        if not is_allowed:
            return False, guard_error

        sql_for_validation = strip_named_placeholders(sql)
        try:
            await self._conn.execute(f"EXPLAIN {sql_for_validation}")
            return True, None
        except asyncpg.exceptions.PostgresSyntaxError as e:
            return False, f"Syntax error: {str(e)}"
        except asyncpg.exceptions.PostgresError as e:
            return False, f"SQL error: {str(e)}"
        except Exception as e:
            return False, f"Unexpected error: {str(e)}"

    @override
    async def explain(self, sql: str) -> dict[str, Any] | None:
        sql_clean = strip_explain_prefix(sql)
        is_allowed, guard_error = validate_select_only_sql(sql_clean)
        if not is_allowed:
            logfire.warning("Query plan rejected by read-only SQL guard", error=guard_error)
            return None

        sql_for_plan = strip_named_placeholders(sql_clean)
        try:
            rows = await self._conn.fetch(f"EXPLAIN (FORMAT JSON, ANALYZE) {sql_for_plan}")
            if not rows:
                return None
            plan_json = rows[0][0]
            if isinstance(plan_json, str):
                plan_json = json.loads(plan_json)
            plan = plan_json[0] if isinstance(plan_json, list) and len(plan_json) > 0 else plan_json
            plan_obj = plan.get("Plan", {}) if isinstance(plan, dict) else {}
            return {
                "total_cost": plan_obj.get("Total Cost", "N/A"),
                "plan_rows": plan_obj.get("Plan Rows", "N/A"),
                "actual_rows": plan_obj.get("Actual Rows", None),
                "plan": plan_obj,
            }
        except Exception as e:
            logfire.warning("Error getting query plan", error=str(e))
            return None

    @override
    async def execute_sql_safe(
        self, sql: str, limit: int = 50
    ) -> tuple[bool, list[dict[str, Any]] | None, str | None]:
        is_allowed, guard_error = validate_select_only_sql(sql)
        if not is_allowed:
            return False, None, guard_error

        sql_clean = strip_named_placeholders(sql)
        sql_clean = ensure_limit(sql_clean, limit)
        try:
            rows = await self._conn.fetch(sql_clean)
            results = sanitize_result_rows([dict(row) for row in rows[:limit]])
            return True, results, None
        except asyncpg.exceptions.PostgresSyntaxError as e:
            return False, None, f"Syntax error: {str(e)}"
        except asyncpg.exceptions.PostgresError as e:
            return False, None, f"SQL error: {str(e)}"
        except Exception as e:
            return False, None, f"Unexpected error: {str(e)}"
