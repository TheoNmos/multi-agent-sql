from __future__ import annotations

import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import asyncpg

from app.auth.crypto import decrypt_payload, encrypt_payload
from app.auth.models import UserConnectionRecord, UserRecord
from app.auth.passwords import hash_password
from app.config import auth_settings
from app.db.metadata import get_metadata_pool, init_metadata_db, is_metadata_db_ready


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


async def init_app_db() -> None:
    """Initialize auth tables on the shared metadata database."""
    await init_metadata_db()


async def close_app_db() -> None:
    """No-op: the metadata pool is closed by close_analytics/close_metadata_db."""
    return None


def is_app_db_ready() -> bool:
    return is_metadata_db_ready()


def _require_pool() -> asyncpg.Pool:
    pool = get_metadata_pool()
    if pool is None:
        raise RuntimeError("Metadata database is not initialized")
    return pool


def _row_to_user(row: asyncpg.Record) -> UserRecord:
    return UserRecord(
        id=str(row["id"]),
        username=row["username"],
        password_hash=row["password_hash"],
        is_active=row["is_active"],
        created_at=row["created_at"],
    )


def _row_to_connection(row: asyncpg.Record) -> UserConnectionRecord:
    payload = decrypt_payload(bytes(row["encrypted_payload"]))
    return UserConnectionRecord(
        id=str(row["id"]),
        user_id=str(row["user_id"]),
        label=row["label"],
        connection_string=str(payload["connection_string"]),
        database_name=str(payload["database_name"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


async def create_user(username: str, password: str, *, is_active: bool = True) -> UserRecord:
    pool = _require_pool()
    user_id = uuid4()
    password_hash = hash_password(password)
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO users (id, username, password_hash, is_active)
            VALUES ($1, $2, $3, $4)
            RETURNING id, username, password_hash, is_active, created_at
            """,
            user_id,
            username,
            password_hash,
            is_active,
        )
    if row is None:
        raise RuntimeError("Failed to create user")
    return _row_to_user(row)


async def get_user_by_username(username: str) -> UserRecord | None:
    pool = _require_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, username, password_hash, is_active, created_at FROM users WHERE username = $1",
            username,
        )
    return _row_to_user(row) if row else None


async def get_user_by_id(user_id: str) -> UserRecord | None:
    pool = _require_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, username, password_hash, is_active, created_at FROM users WHERE id = $1::uuid",
            user_id,
        )
    return _row_to_user(row) if row else None


async def create_auth_session(user_id: str) -> tuple[str, datetime]:
    pool = _require_pool()
    session_id = uuid4()
    raw_token = secrets.token_urlsafe(32)
    token_hash = _hash_token(raw_token)
    expires_at = datetime.now(UTC) + timedelta(hours=auth_settings.auth_session_ttl_hours)
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO auth_sessions (id, user_id, token_hash, expires_at)
            VALUES ($1, $2::uuid, $3, $4)
            """,
            session_id,
            user_id,
            token_hash,
            expires_at,
        )
    return raw_token, expires_at


async def get_user_id_for_session_token(raw_token: str) -> str | None:
    pool = _require_pool()
    token_hash = _hash_token(raw_token)
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT s.user_id
            FROM auth_sessions s
            JOIN users u ON u.id = s.user_id
            WHERE s.token_hash = $1
              AND s.expires_at > NOW()
              AND u.is_active = TRUE
            """,
            token_hash,
        )
    return str(row["user_id"]) if row else None


async def revoke_auth_session(raw_token: str) -> None:
    pool = _require_pool()
    token_hash = _hash_token(raw_token)
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM auth_sessions WHERE token_hash = $1", token_hash)


async def save_user_connection(
    user_id: str,
    connection_string: str,
    database_name: str,
    *,
    label: str | None = None,
    connection_id: str | None = None,
) -> UserConnectionRecord:
    pool = _require_pool()
    conn_id = connection_id or str(uuid4())
    encrypted_payload = encrypt_payload(
        {"connection_string": connection_string, "database_name": database_name}
    )
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO user_connections (id, user_id, label, encrypted_payload)
            VALUES ($1::uuid, $2::uuid, $3, $4)
            RETURNING id, user_id, label, encrypted_payload, created_at, updated_at
            """,
            conn_id,
            user_id,
            label,
            encrypted_payload,
        )
    if row is None:
        raise RuntimeError("Failed to save connection")
    return _row_to_connection(row)


async def list_user_connections(user_id: str) -> list[UserConnectionRecord]:
    pool = _require_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, user_id, label, encrypted_payload, created_at, updated_at
            FROM user_connections
            WHERE user_id = $1::uuid
            ORDER BY created_at DESC
            """,
            user_id,
        )
    return [_row_to_connection(row) for row in rows]


async def get_user_connection(user_id: str, connection_id: str) -> UserConnectionRecord | None:
    pool = _require_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, user_id, label, encrypted_payload, created_at, updated_at
            FROM user_connections
            WHERE id = $1::uuid AND user_id = $2::uuid
            """,
            connection_id,
            user_id,
        )
    return _row_to_connection(row) if row else None


async def delete_user_connection(user_id: str, connection_id: str) -> bool:
    pool = _require_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            "DELETE FROM user_connections WHERE id = $1::uuid AND user_id = $2::uuid",
            connection_id,
            user_id,
        )
    return result.endswith("1")


async def ensure_dev_user() -> UserRecord:
    """Ensure a synthetic dev user exists when AUTH_DISABLED is enabled."""
    existing = await get_user_by_username("__dev__")
    if existing is not None:
        return existing
    return await create_user("__dev__", secrets.token_urlsafe(32), is_active=True)
