"""Database adapter interface used by agents and workflows.

The adapter exposes a small, runtime-focused surface area that lets the rest of
the app remain dialect-agnostic. Each concrete implementation maps the methods
to its driver-specific behavior (asyncpg for PostgreSQL, asyncmy for MySQL).
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from typing import Any, ClassVar, Literal

Dialect = Literal["postgres", "mysql"]


class DatabaseAdapter(ABC):
    """Common interface for PostgreSQL and MySQL runtime adapters."""

    dialect: ClassVar[Dialect]

    def __init__(self, database_name: str) -> None:
        self.database_name = database_name

    @property
    def dialect_label(self) -> str:
        """Human-readable dialect label used in prompts and logs."""
        return "PostgreSQL" if self.dialect == "postgres" else "MySQL"

    @abstractmethod
    async def close(self) -> None:
        """Close the underlying connection."""

    @abstractmethod
    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        """Execute a query and return rows as dictionaries.

        Parameter style is driver-specific: the PostgreSQL adapter uses ``$1``
        placeholders, the MySQL adapter uses ``%s`` placeholders. Callers
        should prefer the high-level helpers below; raw SQL pass-through is
        intended for LLM-generated queries that have no parameters.
        """

    @abstractmethod
    async def execute(self, sql: str, *args: Any) -> str:
        """Execute a non-returning statement (used internally for EXPLAIN)."""

    @abstractmethod
    async def fetchval(self, sql: str, *args: Any) -> Any:
        """Execute a query returning a single scalar value."""

    @abstractmethod
    async def server_version(self) -> str:
        """Return the database server version string for diagnostics."""

    @abstractmethod
    async def list_tables(self) -> list[str]:
        """List all user table names visible in the current database/schema."""

    @abstractmethod
    async def list_column_names(self, table: str) -> list[str]:
        """Return column names for ``table`` in declaration order."""

    @abstractmethod
    async def get_table_info(self, table_names: list[str] | str) -> dict[str, dict[str, Any]]:
        """Return columns, primary keys, foreign keys, and a sample row per table."""

    @abstractmethod
    async def sample_values(self, table: str, column: str, limit: int = 10) -> list[Any]:
        """Return distinct sample values for ``column`` of ``table`` (max ``limit``)."""

    @abstractmethod
    async def search_column_values(
        self, table: str, column: str, keyword: str, limit: int = 10
    ) -> list[Any]:
        """Return values from ``column`` of ``table`` that match ``keyword`` (LIKE)."""

    @abstractmethod
    async def validate_sql(self, sql: str) -> tuple[bool, str | None]:
        """Validate ``sql`` syntactically using ``EXPLAIN``.

        Returns ``(is_valid, error_message)``.
        """

    @abstractmethod
    async def explain(self, sql: str) -> dict[str, Any] | None:
        """Return a normalized query plan for ``sql`` if available."""

    @abstractmethod
    async def execute_sql_safe(
        self, sql: str, limit: int = 50
    ) -> tuple[bool, list[dict[str, Any]] | None, str | None]:
        """Execute ``sql`` with a safety LIMIT and dialect-aware error mapping."""

    @abstractmethod
    def quote_identifier(self, name: str) -> str:
        """Quote ``name`` according to the dialect's identifier rules."""


_PARAM_PLACEHOLDER_START_RE = re.compile(r"[A-Za-z_]")
_PARAM_PLACEHOLDER_REST_RE = re.compile(r"[A-Za-z0-9_]")


def _consume_dollar_quote(sql: str, start: int) -> int | None:
    tag_end = start + 1
    while tag_end < len(sql) and (
        sql[tag_end].isalnum() or sql[tag_end] == "_"
    ):
        tag_end += 1

    if tag_end >= len(sql) or sql[tag_end] != "$":
        return None

    tag = sql[start : tag_end + 1]
    end = sql.find(tag, tag_end + 1)
    return len(sql) if end == -1 else end + len(tag)


def strip_named_placeholders(sql: str) -> str:
    """Replace ``:name`` placeholders with ``NULL`` outside SQL literals.

    Both PostgreSQL and MySQL use positional placeholders (``$1`` and ``%s``)
    rather than the SQLAlchemy/JDBC-style ``:name`` placeholders that some
    LLM outputs include. We replace them with ``NULL`` so syntax validation
    and execution can proceed. PostgreSQL casts such as ``value::text`` must be
    preserved.
    """
    result: list[str] = []
    i = 0
    while i < len(sql):
        char = sql[i]

        if char == "'":
            start = i
            i += 1
            while i < len(sql):
                if sql[i] == "'":
                    i += 1
                    if i < len(sql) and sql[i] == "'":
                        i += 1
                        continue
                    break
                i += 1
            result.append(sql[start:i])
            continue

        if char == '"':
            start = i
            i += 1
            while i < len(sql):
                if sql[i] == '"':
                    i += 1
                    if i < len(sql) and sql[i] == '"':
                        i += 1
                        continue
                    break
                i += 1
            result.append(sql[start:i])
            continue

        if char == "-" and i + 1 < len(sql) and sql[i + 1] == "-":
            end = sql.find("\n", i + 2)
            if end == -1:
                result.append(sql[i:])
                break
            result.append(sql[i : end + 1])
            i = end + 1
            continue

        if char == "/" and i + 1 < len(sql) and sql[i + 1] == "*":
            end = sql.find("*/", i + 2)
            if end == -1:
                result.append(sql[i:])
                break
            result.append(sql[i : end + 2])
            i = end + 2
            continue

        if char == "$":
            dollar_quote_end = _consume_dollar_quote(sql, i)
            if dollar_quote_end is not None:
                result.append(sql[i:dollar_quote_end])
                i = dollar_quote_end
                continue

        if (
            char == ":"
            and (i == 0 or sql[i - 1] != ":")
            and i + 1 < len(sql)
            and _PARAM_PLACEHOLDER_START_RE.fullmatch(sql[i + 1])
        ):
            j = i + 2
            while j < len(sql) and _PARAM_PLACEHOLDER_REST_RE.fullmatch(sql[j]):
                j += 1
            result.append("NULL")
            i = j
            continue

        result.append(char)
        i += 1

    return "".join(result)


def ensure_limit(sql: str, limit: int) -> str:
    """Append ``LIMIT <limit>`` if the query does not already specify one."""
    if "LIMIT" in sql.upper():
        return sql
    return f"{sql} LIMIT {limit}"


def strip_explain_prefix(sql: str) -> str:
    """Strip an accidental ``EXPLAIN`` prefix from ``sql``.

    Some agents prepend ``EXPLAIN`` before calling tools that already wrap
    the query in EXPLAIN. We detect a leading EXPLAIN (with optional options
    like ``ANALYZE`` or ``FORMAT JSON``) and return only the underlying
    statement.
    """
    sql_upper = sql.upper().strip()
    if not sql_upper.startswith("EXPLAIN"):
        return sql
    match = re.search(r"\b(SELECT|INSERT|UPDATE|DELETE|WITH|CREATE|ALTER|DROP)\b", sql, re.IGNORECASE)
    if match:
        return sql[match.start() :].strip()
    return re.sub(
        r"^EXPLAIN\s+(ANALYZE\s+)?(\([^)]+\)\s+)?",
        "",
        sql,
        flags=re.IGNORECASE,
    ).strip()
