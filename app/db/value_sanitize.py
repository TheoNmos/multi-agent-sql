"""Sanitize database cell values before exposing them to LLM-facing tools."""

from __future__ import annotations

from typing import Any

from app.db.adapter import DatabaseAdapter

MAX_AGENT_STRING_LEN = 120
MAX_BINARY_PREVIEW_BYTES = 16

BINARY_PG_TYPES = frozenset({"bytea"})
BINARY_MYSQL_TYPES = frozenset(
    {"binary", "varbinary", "blob", "tinyblob", "mediumblob", "longblob"}
)

_BINARY_NAME_HINTS = ("_base64", "_blob", "_binary", "_bytes", "_photo", "_image")


def is_binary_column(data_type: str | None) -> bool:
    """Return True when schema metadata indicates a binary/BLOB column."""
    if not data_type:
        return False
    normalized = data_type.strip().lower()
    if normalized in BINARY_PG_TYPES or normalized in BINARY_MYSQL_TYPES:
        return True
    return any(token in normalized for token in ("blob", "binary", "bytea"))


def _looks_like_binary_name(column_name: str | None) -> bool:
    if not column_name:
        return False
    lowered = column_name.lower()
    return any(hint in lowered for hint in _BINARY_NAME_HINTS)


def _binary_placeholder(data_type: str | None, byte_length: int | None = None) -> str:
    type_label = data_type or "binary"
    if byte_length is not None:
        return f"<binary column: {type_label}, {byte_length} bytes omitted from sample>"
    return f"<binary column: {type_label}, omitted from sample>"


def _truncate_text(text: str) -> str:
    if len(text) <= MAX_AGENT_STRING_LEN:
        return text
    return text[: MAX_AGENT_STRING_LEN - 1] + "…"


def sanitize_cell_value(
    value: Any,
    *,
    column_name: str | None = None,
    data_type: str | None = None,
) -> Any:
    """Return a compact, TOON-safe scalar for agent consumption."""
    if value is None or isinstance(value, (bool, int, float)):
        return value

    if isinstance(value, (bytes, bytearray, memoryview)):
        raw = bytes(value)
        preview = raw[:MAX_BINARY_PREVIEW_BYTES].hex()
        suffix = f", hex={preview}" if preview else ""
        return f"<binary: {len(raw)} bytes{suffix}>"

    if isinstance(value, str):
        if (
            len(value) > MAX_AGENT_STRING_LEN
            and (_looks_like_binary_name(column_name) or is_binary_column(data_type))
        ):
            return f"<binary-like text: {len(value)} chars omitted from sample>"
        return _truncate_text(value)

    return _truncate_text(str(value))


def sanitize_sample_row(
    row: dict[str, Any],
    column_types: dict[str, str],
    *,
    omitted_binary_columns: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Sanitize every value in a sample row dict."""
    sanitized: dict[str, Any] = {}
    if omitted_binary_columns:
        for column_name, data_type in omitted_binary_columns.items():
            sanitized[column_name] = _binary_placeholder(data_type)
    for column_name, value in row.items():
        data_type = column_types.get(column_name)
        sanitized[column_name] = sanitize_cell_value(
            value,
            column_name=column_name,
            data_type=data_type,
        )
    return sanitized


def sanitize_result_row(
    row: dict[str, Any],
    column_types: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Sanitize an arbitrary query result row before JSON storage or LLM exposure."""
    types = column_types or {}
    sanitized: dict[str, Any] = {}
    for column_name, value in row.items():
        key = str(column_name)
        sanitized[key] = sanitize_cell_value(
            value,
            column_name=key,
            data_type=types.get(key),
        )
    return sanitized


def sanitize_result_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Sanitize arbitrary query rows so they can be safely JSON serialized."""
    return [sanitize_result_row(row) for row in rows]


def sanitize_value_list(
    values: list[Any],
    *,
    column_name: str,
    data_type: str | None,
) -> list[Any]:
    """Sanitize values returned by sample/search column tools."""
    if is_binary_column(data_type) or _looks_like_binary_name(column_name):
        return []
    return [
        sanitize_cell_value(value, column_name=column_name, data_type=data_type)
        for value in values
    ]


def column_types_from_columns(columns: list[dict[str, Any]]) -> dict[str, str]:
    """Build a name -> type map from get_table_info column metadata."""
    types: dict[str, str] = {}
    for column in columns:
        name = column.get("name")
        col_type = column.get("type")
        if name and col_type:
            types[str(name)] = str(col_type)
    return types


async def get_column_type(adapter: DatabaseAdapter, table: str, column: str) -> str | None:
    """Look up a column's data type from information_schema."""
    if adapter.dialect == "postgres":
        rows = await adapter.fetch(
            """
            SELECT data_type
            FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = $1 AND column_name = $2
            LIMIT 1
            """,
            table,
            column,
        )
    else:
        rows = await adapter.fetch(
            """
            SELECT data_type
            FROM information_schema.columns
            WHERE table_schema = DATABASE() AND table_name = %s AND column_name = %s
            LIMIT 1
            """,
            table,
            column,
        )
    if not rows:
        return None
    row = rows[0]
    return row.get("data_type") or row.get("DATA_TYPE")


async def list_table_columns_with_types(
    adapter: DatabaseAdapter,
    table: str,
) -> list[dict[str, Any]]:
    """Return column name/type metadata for one table."""
    if adapter.dialect == "postgres":
        rows = await adapter.fetch(
            """
            SELECT column_name, data_type
            FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = $1
            ORDER BY ordinal_position
            """,
            table,
        )
    else:
        rows = await adapter.fetch(
            """
            SELECT column_name, data_type
            FROM information_schema.columns
            WHERE table_schema = DATABASE() AND table_name = %s
            ORDER BY ordinal_position
            """,
            table,
        )
    columns: list[dict[str, Any]] = []
    for row in rows:
        name = row.get("column_name") or row.get("COLUMN_NAME")
        col_type = row.get("data_type") or row.get("DATA_TYPE")
        if name:
            columns.append({"name": name, "type": col_type or ""})
    return columns


async def prefetch_sample_rows(
    adapter: DatabaseAdapter,
    table_names: list[str],
) -> dict[str, dict[str, Any] | None]:
    """Fetch one sanitized sample row per table (binary payloads omitted)."""
    samples: dict[str, dict[str, Any] | None] = {}
    for table_name in table_names:
        try:
            columns = await list_table_columns_with_types(adapter, table_name)
            samples[table_name] = await fetch_table_sample_row(adapter, table_name, columns)
        except Exception:
            samples[table_name] = None
    return samples


async def fetch_table_sample_row(
    adapter: DatabaseAdapter,
    table_name: str,
    columns: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Fetch one sample row, omitting binary payloads from the SELECT."""
    column_types = column_types_from_columns(columns)
    if not column_types:
        return None

    sample_columns: list[str] = []
    omitted_binary: dict[str, str] = {}
    for column in columns:
        name = column.get("name")
        if not name:
            continue
        col_type = column_types.get(str(name), "")
        if is_binary_column(col_type):
            omitted_binary[str(name)] = col_type
        else:
            sample_columns.append(str(name))

    fetched_row: dict[str, Any] = {}
    if sample_columns:
        quoted_columns = ", ".join(adapter.quote_identifier(col) for col in sample_columns)
        sample_query = f"SELECT {quoted_columns} FROM {adapter.quote_identifier(table_name)} LIMIT 1"
        rows = await adapter.fetch(sample_query)
        if rows:
            fetched_row = dict(rows[0])
        elif not omitted_binary:
            return None

    if not fetched_row and not omitted_binary:
        return None

    return sanitize_sample_row(
        fetched_row,
        column_types,
        omitted_binary_columns=omitted_binary,
    )
