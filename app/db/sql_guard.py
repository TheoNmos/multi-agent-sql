"""Read-only SQL guard: allow SELECT / WITH … SELECT only, block DML and DDL."""

from __future__ import annotations

import re

from app.db.adapter import strip_explain_prefix

# Whole-word keywords that indicate writes, DDL, or session/admin commands.
_FORBIDDEN_KEYWORDS = frozenset(
    {
        "insert",
        "update",
        "delete",
        "merge",
        "replace",
        "truncate",
        "drop",
        "create",
        "alter",
        "rename",
        "grant",
        "revoke",
        "copy",
        "load",
        "unload",
        "call",
        "exec",
        "execute",
        "do",
        "vacuum",
        "refresh",
        "reindex",
        "cluster",
        "reset",
        "use",
        "unlock",
        "attach",
        "detach",
        "pragma",
        "begin",
        "commit",
        "rollback",
        "start",
        "savepoint",
        "release",
        "prepare",
        "deallocate",
        "listen",
        "notify",
        "unlisten",
        "checkpoint",
        "discard",
        "comment",
        "security",
        "reconfigure",
    }
)

# MySQL / PostgreSQL file and export side effects inside SELECT.
_FORBIDDEN_SELECT_MODIFIERS = frozenset(
    {
        "outfile",
        "dumpfile",
        "infile",
        "load_file",
    }
)

_KEYWORD_RE = re.compile(
    r"\b(" + "|".join(re.escape(k) for k in sorted(_FORBIDDEN_KEYWORDS, key=len, reverse=True)) + r")\b",
    re.IGNORECASE,
)
_SELECT_MODIFIER_RE = re.compile(
    r"\b(" + "|".join(re.escape(k) for k in _FORBIDDEN_SELECT_MODIFIERS) + r")\b",
    re.IGNORECASE,
)
_SELECT_INTO_RE = re.compile(r"\bSELECT\b[\s\S]+\bINTO\b", re.IGNORECASE)
_ALLOWED_START_RE = re.compile(r"^(WITH|SELECT)\b", re.IGNORECASE)


def _consume_dollar_quote(sql: str, start: int) -> int | None:
    tag_end = start + 1
    while tag_end < len(sql) and (sql[tag_end].isalnum() or sql[tag_end] == "_"):
        tag_end += 1
    if tag_end >= len(sql) or sql[tag_end] != "$":
        return None
    tag = sql[start : tag_end + 1]
    end = sql.find(tag, tag_end + 1)
    return len(sql) if end == -1 else end + len(tag)


def _mask_literals_and_comments(sql: str) -> str:
    """Replace string literals and comments with spaces so keyword scans ignore them."""
    result: list[str] = []
    i = 0
    while i < len(sql):
        char = sql[i]

        if char == "'":
            result.append(" ")
            i += 1
            while i < len(sql):
                if sql[i] == "'":
                    i += 1
                    if i < len(sql) and sql[i] == "'":
                        i += 1
                        continue
                    break
                i += 1
            continue

        if char == '"':
            result.append(" ")
            i += 1
            while i < len(sql):
                if sql[i] == '"':
                    i += 1
                    if i < len(sql) and sql[i] == '"':
                        i += 1
                        continue
                    break
                i += 1
            continue

        if char == "`":
            result.append(" ")
            i += 1
            while i < len(sql) and sql[i] != "`":
                i += 1
            i += 1 if i < len(sql) else 0
            continue

        if char == "-" and i + 1 < len(sql) and sql[i + 1] == "-":
            end = sql.find("\n", i + 2)
            if end == -1:
                result.append(" " * (len(sql) - i))
                break
            result.append(" " * (end - i))
            i = end
            continue

        if char == "/" and i + 1 < len(sql) and sql[i + 1] == "*":
            end = sql.find("*/", i + 2)
            if end == -1:
                result.append(" " * (len(sql) - i))
                break
            result.append(" " * (end + 2 - i))
            i = end + 2
            continue

        if char == "$":
            dollar_quote_end = _consume_dollar_quote(sql, i)
            if dollar_quote_end is not None:
                result.append(" " * (dollar_quote_end - i))
                i = dollar_quote_end
                continue

        result.append(char)
        i += 1

    return "".join(result)


def _has_multiple_statements(sql: str) -> bool:
    masked = _mask_literals_and_comments(sql)
    parts = [part.strip() for part in masked.split(";")]
    non_empty = [part for part in parts if part]
    return len(non_empty) > 1


def validate_select_only_sql(sql: str) -> tuple[bool, str | None]:
    """
    Return ``(True, None)`` when ``sql`` is a single read-only SELECT statement.

    Allows:
    - ``SELECT …``
    - ``WITH … SELECT …`` (including nested CTEs that remain SELECT-only)

    Rejects DML, DDL, transaction control, multi-statement batches, and
    ``SELECT … INTO`` (PostgreSQL table creation).
    """
    if not sql or not sql.strip():
        return False, "Empty SQL statement."

    if _has_multiple_statements(sql):
        return False, "Multiple SQL statements are not allowed."

    statement = strip_explain_prefix(sql.strip())
    masked = _mask_literals_and_comments(statement)
    normalized = re.sub(r"\s+", " ", masked).strip()

    if not normalized:
        return False, "Empty SQL statement."

    if not _ALLOWED_START_RE.match(normalized):
        return False, "Only SELECT queries are allowed."

    forbidden = _KEYWORD_RE.search(normalized)
    if forbidden:
        keyword = forbidden.group(1).upper()
        return False, f"Forbidden SQL keyword: {keyword}. Only SELECT queries are allowed."

    modifier = _SELECT_MODIFIER_RE.search(normalized)
    if modifier:
        keyword = modifier.group(1).upper()
        return False, f"Forbidden SQL construct: {keyword}. Only read-only SELECT queries are permitted."

    if _SELECT_INTO_RE.search(normalized):
        return False, "SELECT INTO is not allowed. Only read-only SELECT queries are permitted."

    return True, None
