"""Helpers for bounded schema prefetch and per-connection metadata caching."""

from __future__ import annotations

import re
from typing import Any

from app.db.adapter import DatabaseAdapter
from app.db.value_sanitize import get_column_type

# Cap sample-row prefetch on large schemas to cut DB round-trips and mapper prompt tokens.
DEFAULT_MAX_PREFETCH_TABLES = 32
_MIN_PREFETCH_TABLES = 8

_TOKEN_RE = re.compile(r"[a-z0-9_]{3,}", re.IGNORECASE)


def _question_tokens(question: str) -> list[str]:
    return [token.lower() for token in _TOKEN_RE.findall(question)]


def select_tables_for_sample_prefetch(
    question: str,
    all_tables: list[str],
    *,
    max_tables: int = DEFAULT_MAX_PREFETCH_TABLES,
) -> list[str]:
    """Choose tables whose sample rows are worth prefetching for the mapper prompt."""
    if not all_tables:
        return []
    if len(all_tables) <= max_tables:
        return list(all_tables)

    tokens = _question_tokens(question)
    if not tokens:
        return all_tables[:max_tables]

    scored: list[tuple[int, str]] = []
    for table in all_tables:
        table_lower = table.lower()
        score = 0
        for token in tokens:
            if token == table_lower:
                score += 100
            elif table_lower.startswith(token) or table_lower.endswith(token):
                score += 40
            elif token in table_lower:
                score += 20
        if score > 0:
            scored.append((score, table))

    scored.sort(key=lambda item: (-item[0], item[1]))
    selected = [table for _, table in scored[:max_tables]]

    if len(selected) < _MIN_PREFETCH_TABLES:
        selected_set = set(selected)
        for table in sorted(all_tables):
            if table not in selected_set:
                selected.append(table)
                selected_set.add(table)
            if len(selected) >= min(_MIN_PREFETCH_TABLES, max_tables):
                break

    return selected[:max_tables]


def column_type_cache_key(table: str, column: str) -> tuple[str, str]:
    return (table.strip().lower(), column.strip().lower())


def seed_column_types_from_table_info(
    table_info: dict[str, dict[str, Any]],
    cache: dict[tuple[str, str], str | None],
) -> None:
    """Populate the per-run column type cache from get_table_info metadata."""
    for table_name, info in table_info.items():
        for column in info.get("columns", []):
            name = column.get("name")
            col_type = column.get("type")
            if name and col_type:
                cache[column_type_cache_key(table_name, str(name))] = str(col_type)


async def get_column_type_cached(
    adapter: DatabaseAdapter,
    table: str,
    column: str,
    cache: dict[tuple[str, str], str | None],
) -> str | None:
    key = column_type_cache_key(table, column)
    if key in cache:
        return cache[key]
    col_type = await get_column_type(adapter, table, column)
    cache[key] = col_type
    return col_type
