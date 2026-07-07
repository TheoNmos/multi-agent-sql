from __future__ import annotations

from app.auth import db as auth_db
from app.auth.models import UserConnectionRecord

__all__ = [
    "UserConnectionRecord",
    "delete_connection",
    "get_connection",
    "list_connections",
    "save_connection",
]


async def save_connection(
    user_id: str,
    connection_string: str,
    database_name: str,
    *,
    label: str | None = None,
) -> UserConnectionRecord:
    return await auth_db.save_user_connection(
        user_id,
        connection_string,
        database_name,
        label=label,
    )


async def list_connections(user_id: str) -> list[UserConnectionRecord]:
    return await auth_db.list_user_connections(user_id)


async def get_connection(user_id: str, connection_id: str) -> UserConnectionRecord | None:
    return await auth_db.get_user_connection(user_id, connection_id)


async def delete_connection(user_id: str, connection_id: str) -> bool:
    return await auth_db.delete_user_connection(user_id, connection_id)
