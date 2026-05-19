"""Metrics for evaluating SQL predictions."""

import asyncio
from datetime import date, datetime
from decimal import Decimal
from typing import Any

import asyncpg

from app.db.connection import database_connect

NUMERIC_CANON_FORMAT = ".12g"


def _make_json_serializable(obj: Any) -> Any:
    """Convert database types to JSON-serializable types."""
    if obj is None:
        return None
    elif isinstance(obj, (Decimal, float)):
        return float(obj)
    elif isinstance(obj, (date, datetime)):
        return obj.isoformat()
    elif isinstance(obj, (bytes, bytearray)):
        return obj.hex()
    elif isinstance(obj, (list, tuple)):
        return [_make_json_serializable(item) for item in obj]
    elif isinstance(obj, dict):
        return {k: _make_json_serializable(v) for k, v in obj.items()}
    else:
        return obj


def exact_match(gold_sql: str, pred_sql: str) -> bool:
    """
    Check if predicted SQL exactly matches gold SQL (naive string comparison).

    Args:
        gold_sql: Gold standard SQL query
        pred_sql: Predicted SQL query

    Returns:
        True if strings are exactly equal, False otherwise
    """
    return gold_sql.strip() == pred_sql.strip()


def _canonical_value(value: Any) -> str:
    if value is None:
        return "NULL"
    # bool is a subclass of int; keep true/false semantics instead of normalizing as 1/0.
    if isinstance(value, bool):
        return str(value).strip().lower()
    if isinstance(value, (int, float, Decimal)):
        try:
            return format(float(value), NUMERIC_CANON_FORMAT)
        except (ValueError, OverflowError):
            return str(value).strip().lower()
    return str(value).strip().lower()


async def _normalize_row(row: asyncpg.Record) -> frozenset[tuple[str, str]]:
    """
    Normalize a database row to a set of (column_name, canonical_value) tuples.

    Args:
        row: Database record

    Returns:
        Frozen set of (column_name, canonical_value) tuples
    """
    return frozenset((str(key).lower(), _canonical_value(value)) for key, value in row.items())


async def _normalize_row_values_only(row: asyncpg.Record) -> tuple[str, ...]:
    """
    Normalize a database row to a tuple of canonicalized values only (ignore column names).

    Args:
        row: Database record

    Returns:
        Tuple of canonicalized values preserving column order
    """
    return tuple(_canonical_value(value) for _, value in row.items())


async def _execute_and_normalize(
    conn: asyncpg.Connection,
    sql: str,
    ignore_column_names: bool = True,
) -> tuple[frozenset[Any], list[dict[str, Any]]]:
    """
    Execute SQL and return normalized result set along with raw results.

    Args:
        conn: Database connection
        sql: SQL query to execute

    Returns:
        Tuple of (normalized_result_set, raw_results)
        - normalized_result_set: Set of normalized rows (each row is a frozenset of (col, value) tuples)
        - raw_results: List of dictionaries representing the raw query results
    """
    print(f"Executing SQL: {sql}")
    rows = await conn.fetch(sql)
    normalized_rows: list[Any] = []
    raw_results = []
    for row in rows:
        if ignore_column_names:
            normalized_rows.append(await _normalize_row_values_only(row))
        else:
            normalized_rows.append(await _normalize_row(row))
        # Convert asyncpg.Record to dict and make values JSON serializable
        row_dict = dict(row)
        serializable_dict = {k: _make_json_serializable(v) for k, v in row_dict.items()}
        raw_results.append(serializable_dict)
    return frozenset(normalized_rows), raw_results


async def execution_match_async(
    server_dsn: str,
    db_name: str,
    gold_sql: str,
    pred_sql: str,
    timeout_s: int = 30,
    ignore_column_names: bool = True,
) -> tuple[bool | None, str | None, list[dict[str, Any]] | None, list[dict[str, Any]] | None]:
    """
    Check if predicted SQL produces the same results as gold SQL (execution accuracy).

    Executes both queries and compares result sets (order-insensitive, value-normalized).

    Args:
        server_dsn: PostgreSQL server DSN (without database name)
        db_name: Database name to connect to
        gold_sql: Gold standard SQL query
        pred_sql: Predicted SQL query
        timeout_s: Timeout in seconds for each query execution

    Returns:
        Tuple of (match_result, error_message, gold_results, pred_results)
        - match_result: True if results match, False if they don't, None if error
        - error_message: Error description if execution failed, None otherwise
        - gold_results: Raw results from gold SQL query (list of dicts), None if error
        - pred_results: Raw results from predicted SQL query (list of dicts), None if error
    """
    if not pred_sql.strip():
        return None, "Predicted SQL is empty", None, None

    try:
        # Connect to database
        async with database_connect(server_dsn=server_dsn, database=db_name) as conn:
            print(f"Executing on database {db_name} with server DSN {server_dsn}")
            # Execute both queries with timeout
            gold_results_normalized = None
            gold_results_raw = None
            pred_results_normalized = None
            pred_results_raw = None

            try:
                gold_results_normalized, gold_results_raw = await asyncio.wait_for(
                    _execute_and_normalize(conn, gold_sql, ignore_column_names=ignore_column_names),
                    timeout=timeout_s,
                )
            except TimeoutError:
                return None, f"Gold SQL execution timed out after {timeout_s}s", None, None
            except Exception as e:
                return None, f"Gold SQL execution error: {str(e)}", None, None

            try:
                pred_results_normalized, pred_results_raw = await asyncio.wait_for(
                    _execute_and_normalize(conn, pred_sql, ignore_column_names=ignore_column_names),
                    timeout=timeout_s,
                )
            except TimeoutError:
                return None, f"Predicted SQL execution timed out after {timeout_s}s", gold_results_raw, None
            except Exception as e:
                return None, f"Predicted SQL execution error: {str(e)}", gold_results_raw, None

            # Compare result sets
            match = gold_results_normalized == pred_results_normalized
            return match, None, gold_results_raw, pred_results_raw

    except Exception as e:
        return None, f"Connection or execution error: {str(e)}", None, None


def execution_match(
    server_dsn: str,
    db_name: str,
    gold_sql: str,
    pred_sql: str,
    timeout_s: int = 30,
    ignore_column_names: bool = True,
) -> tuple[bool | None, str | None, list[dict[str, Any]] | None, list[dict[str, Any]] | None]:
    """
    Synchronous wrapper for execution_match_async.

    Args:
        server_dsn: PostgreSQL server DSN (without database name)
        db_name: Database name to connect to
        gold_sql: Gold standard SQL query
        pred_sql: Predicted SQL query
        timeout_s: Timeout in seconds for each query execution

    Returns:
        Tuple of (match_result, error_message, gold_results, pred_results)
        - match_result: True if results match, False if they don't, None if error
        - error_message: Error description if execution failed, None otherwise
        - gold_results: Raw results from gold SQL query (list of dicts), None if error
        - pred_results: Raw results from predicted SQL query (list of dicts), None if error
    """
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

    return loop.run_until_complete(
        execution_match_async(
            server_dsn,
            db_name,
            gold_sql,
            pred_sql,
            timeout_s,
            ignore_column_names=ignore_column_names,
        )
    )
