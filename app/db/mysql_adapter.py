"""MySQL adapter implemented on top of asyncmy."""

from __future__ import annotations

import inspect
import json
from typing import Any, ClassVar, override

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


def _import_asyncmy() -> Any:
    """Import asyncmy lazily so the module can be loaded without the driver installed."""
    try:
        import asyncmy
    except ImportError as e:  # pragma: no cover - handled at runtime when MySQL is requested
        raise ImportError(
            "asyncmy is required for MySQL connections. Install it with `pip install asyncmy`."
        ) from e
    return asyncmy


def _import_asyncmy_errors() -> Any:
    try:
        return _import_asyncmy().errors
    except ImportError as e:  # pragma: no cover
        raise ImportError(
            "asyncmy is required for MySQL connections. Install it with `pip install asyncmy`."
        ) from e


class MySQLAdapter(DatabaseAdapter):
    """Adapter that exposes asyncmy connections through the shared interface."""

    dialect: ClassVar[Dialect] = "mysql"

    def __init__(self, conn: Any, database_name: str) -> None:
        super().__init__(database_name=database_name)
        self._conn = conn

    @property
    def raw_connection(self) -> Any:
        return self._conn

    @override
    async def close(self) -> None:
        try:
            await self._conn.ensure_closed()
        except AttributeError:
            close = getattr(self._conn, "close", None)
            if callable(close):
                maybe_awaitable = close()
                if inspect.isawaitable(maybe_awaitable):
                    await maybe_awaitable

    @override
    def quote_identifier(self, name: str) -> str:
        escaped = name.replace("`", "``")
        return f"`{escaped}`"

    async def _run_cursor(
        self, sql: str, args: tuple[Any, ...] | None = None
    ) -> tuple[list[dict[str, Any]], list[str]]:
        async with self._conn.cursor() as cur:
            await cur.execute(sql, args or ())
            description = cur.description or []
            columns = [desc[0] for desc in description]
            try:
                raw_rows = await cur.fetchall()
            except Exception:
                raw_rows = []
        rows: list[dict[str, Any]] = []
        for raw in raw_rows:
            if isinstance(raw, dict):
                rows.append(raw)
            else:
                rows.append({col: raw[idx] for idx, col in enumerate(columns)})
        return rows, columns

    @override
    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        rows, _ = await self._run_cursor(sql, args if args else None)
        return rows

    @override
    async def execute(self, sql: str, *args: Any) -> str:
        async with self._conn.cursor() as cur:
            await cur.execute(sql, args or ())
            return f"OK {cur.rowcount}"

    @override
    async def fetchval(self, sql: str, *args: Any) -> Any:
        rows, columns = await self._run_cursor(sql, args if args else None)
        if not rows or not columns:
            return None
        return rows[0].get(columns[0])

    @override
    async def server_version(self) -> str:
        version = await self.fetchval("SELECT VERSION()")
        return str(version) if version is not None else ""

    @override
    async def list_tables(self) -> list[str]:
        rows, _ = await self._run_cursor(
            """
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema = DATABASE()
            ORDER BY table_name
            """
        )
        names: list[str] = []
        for row in rows:
            value = row.get("table_name") or row.get("TABLE_NAME")
            if value is not None:
                names.append(value)
        return names

    @override
    async def list_column_names(self, table: str) -> list[str]:
        rows, _ = await self._run_cursor(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = DATABASE() AND table_name = %s
            ORDER BY ordinal_position
            """,
            (table,),
        )
        names: list[str] = []
        for row in rows:
            value = row.get("column_name") or row.get("COLUMN_NAME")
            if value is not None:
                names.append(value)
        return names

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

        placeholders = ",".join(["%s"] * len(table_names))
        params = tuple(table_names)

        column_rows, _ = await self._run_cursor(
            f"""
            SELECT
                table_name,
                column_name,
                data_type,
                column_type,
                is_nullable
            FROM information_schema.columns
            WHERE table_schema = DATABASE() AND table_name IN ({placeholders})
            ORDER BY table_name, ordinal_position
            """,
            params,
        )

        pk_rows, _ = await self._run_cursor(
            f"""
            SELECT k.table_name, k.column_name
            FROM information_schema.table_constraints AS t
            JOIN information_schema.key_column_usage AS k
                ON t.constraint_name = k.constraint_name
                AND t.table_schema = k.table_schema
                AND t.table_name = k.table_name
            WHERE t.constraint_type = 'PRIMARY KEY'
                AND t.table_schema = DATABASE()
                AND t.table_name IN ({placeholders})
            ORDER BY k.table_name, k.ordinal_position
            """,
            params,
        )

        fk_rows, _ = await self._run_cursor(
            f"""
            SELECT
                k.table_name AS src_table,
                k.column_name AS src_column,
                k.referenced_table_name AS dst_table,
                k.referenced_column_name AS dst_column
            FROM information_schema.key_column_usage AS k
            WHERE k.table_schema = DATABASE()
                AND k.referenced_table_name IS NOT NULL
                AND (k.table_name IN ({placeholders}) OR k.referenced_table_name IN ({placeholders}))
            ORDER BY k.table_name
            """,
            params + params,
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

        def _row_field(row: dict[str, Any], *names: str) -> Any:
            for name in names:
                if name in row:
                    return row[name]
                upper = name.upper()
                if upper in row:
                    return row[upper]
            return None

        for row in pk_rows:
            table_name = _row_field(row, "table_name")
            column_name = _row_field(row, "column_name")
            if table_name in primary_keys_by_table and column_name is not None:
                primary_keys_by_table[table_name].append(column_name)

        for row in fk_rows:
            src_table = _row_field(row, "src_table")
            dst_table = _row_field(row, "dst_table")
            if src_table is None or dst_table is None:
                continue
            fk_info = {
                "src_table": src_table,
                "src_column": _row_field(row, "src_column"),
                "dst_table": dst_table,
                "dst_column": _row_field(row, "dst_column"),
            }
            if src_table in foreign_keys_by_table:
                foreign_keys_by_table[src_table].append(fk_info)
            if dst_table in foreign_keys_by_table:
                foreign_keys_by_table[dst_table].append(fk_info)

        for row in column_rows:
            table_name = _row_field(row, "table_name")
            if table_name in result:
                result[table_name]["columns"].append(
                    {
                        "name": _row_field(row, "column_name"),
                        "type": _row_field(row, "data_type") or _row_field(row, "column_type"),
                        "nullable": (_row_field(row, "is_nullable") or "").upper() == "YES",
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
                f"WHERE {self.quote_identifier(column)} IS NOT NULL LIMIT %s"
            )
            rows, _ = await self._run_cursor(query, (limit,))
            raw_values = [row.get(column) for row in rows]
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
                f"WHERE CAST({self.quote_identifier(column)} AS CHAR) LIKE %s LIMIT %s"
            )
            pattern = f"%{keyword}%"
            rows, _ = await self._run_cursor(query, (pattern, limit))
            raw_values = [row.get(column) for row in rows]
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
        errors = _import_asyncmy_errors()
        try:
            await self.execute(f"EXPLAIN {sql_for_validation}")
            return True, None
        except errors.ProgrammingError as e:
            return False, f"Syntax error: {str(e)}"
        except errors.Error as e:
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
            rows, _ = await self._run_cursor(f"EXPLAIN FORMAT=JSON {sql_for_plan}")
            if not rows:
                return None
            first = rows[0]
            json_text = next(iter(first.values()), None)
            if not json_text:
                return None
            if isinstance(json_text, (bytes, bytearray)):
                json_text = json_text.decode("utf-8", errors="replace")
            try:
                plan_obj = json.loads(json_text) if isinstance(json_text, str) else json_text
            except json.JSONDecodeError:
                return {"plan": {"raw": str(json_text)}}
            query_block = plan_obj.get("query_block") if isinstance(plan_obj, dict) else None
            cost_info = (
                query_block.get("cost_info", {}) if isinstance(query_block, dict) else {}
            )
            total_cost = cost_info.get("query_cost") or "N/A"
            return {
                "total_cost": total_cost,
                "plan_rows": "N/A",
                "actual_rows": None,
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
        errors = _import_asyncmy_errors()
        try:
            rows, _ = await self._run_cursor(sql_clean)
            return True, sanitize_result_rows(rows[:limit]), None
        except errors.ProgrammingError as e:
            return False, None, f"Syntax error: {str(e)}"
        except errors.Error as e:
            return False, None, f"SQL error: {str(e)}"
        except Exception as e:
            return False, None, f"Unexpected error: {str(e)}"
