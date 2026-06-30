"""Helpers to detect and normalize database connection URLs across dialects."""

from __future__ import annotations

from urllib.parse import urlparse, urlunparse

from app.db.adapter import Dialect

_POSTGRES_SCHEMES = {"postgres", "postgresql", "postgres+asyncpg", "postgresql+asyncpg"}
_MYSQL_SCHEMES = {"mysql", "mysql+asyncmy", "mysql+pymysql", "mysql+aiomysql"}


def detect_dialect(url: str) -> Dialect:
    """Return the dialect for ``url`` based on its scheme.

    Raises ``ValueError`` for unsupported schemes.
    """
    parsed = urlparse(url)
    scheme = parsed.scheme.lower()
    if scheme in _POSTGRES_SCHEMES:
        return "postgres"
    if scheme in _MYSQL_SCHEMES:
        return "mysql"
    raise ValueError(
        f"Unsupported database scheme: {parsed.scheme!r}. "
        "Expected one of: postgres, postgresql, mysql, mysql+asyncmy."
    )


def split_url_and_database(url: str) -> tuple[str, str]:
    """Split ``url`` into ``(server_dsn, database_name)``.

    The server DSN is the URL with the database stripped from its path,
    suitable for passing to ``database_connect`` alongside an explicit
    database name. Returns an empty string for the database when the URL
    does not embed one.
    """
    parsed = urlparse(url)
    database = parsed.path.lstrip("/") if parsed.path else ""
    server_dsn = urlunparse(
        (
            parsed.scheme,
            parsed.netloc,
            "",
            parsed.params,
            parsed.query,
            parsed.fragment,
        )
    )
    return server_dsn, database


def build_full_url(server_dsn: str, database: str) -> str:
    """Reattach ``database`` to ``server_dsn`` to produce a full URL."""
    parsed = urlparse(server_dsn)
    return urlunparse(
        (
            parsed.scheme,
            parsed.netloc,
            f"/{database}" if database else "",
            parsed.params,
            parsed.query,
            parsed.fragment,
        )
    )


def normalize_postgres_url(server_dsn: str, database: str) -> str:
    """Return an asyncpg-friendly ``postgres://`` URL for ``server_dsn``/``database``."""
    parsed = urlparse(server_dsn)
    scheme = "postgres"
    if parsed.scheme.lower() not in _POSTGRES_SCHEMES:
        raise ValueError(f"Not a PostgreSQL DSN: {parsed.scheme!r}")
    return urlunparse(
        (
            scheme,
            parsed.netloc,
            f"/{database}" if database else "",
            parsed.params,
            parsed.query,
            parsed.fragment,
        )
    )


def normalize_asyncpg_url(url: str) -> str:
    """Return a full PostgreSQL URL with a scheme accepted by asyncpg."""
    parsed = urlparse(url)
    if parsed.scheme.lower() not in _POSTGRES_SCHEMES:
        raise ValueError(f"Not a PostgreSQL DSN: {parsed.scheme!r}")
    return urlunparse(
        (
            "postgresql",
            parsed.netloc,
            parsed.path,
            parsed.params,
            parsed.query,
            parsed.fragment,
        )
    )


def parse_mysql_dsn(server_dsn: str, database: str) -> dict[str, object]:
    """Parse a MySQL ``server_dsn`` into kwargs suitable for ``asyncmy.connect``.

    ``server_dsn`` may use ``mysql://``, ``mysql+asyncmy://``, ``mysql+pymysql://``,
    or ``mysql+aiomysql://`` schemes. Username, password, host, and port are
    extracted from the netloc. ``database`` is passed via the ``db`` kwarg.
    """
    parsed = urlparse(server_dsn)
    if parsed.scheme.lower() not in _MYSQL_SCHEMES:
        raise ValueError(f"Not a MySQL DSN: {parsed.scheme!r}")

    kwargs: dict[str, object] = {
        "host": parsed.hostname or "localhost",
        "port": parsed.port or 3306,
    }
    if parsed.username:
        kwargs["user"] = parsed.username
    if parsed.password:
        kwargs["password"] = parsed.password
    if database:
        kwargs["db"] = database
    return kwargs
