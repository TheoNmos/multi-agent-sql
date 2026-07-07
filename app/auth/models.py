from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class CurrentUser:
    id: str
    username: str
    is_active: bool = True


@dataclass(frozen=True, slots=True)
class UserRecord:
    id: str
    username: str
    password_hash: str
    is_active: bool
    created_at: datetime


@dataclass(frozen=True, slots=True)
class UserConnectionRecord:
    id: str
    user_id: str
    label: str | None
    connection_string: str
    database_name: str
    created_at: datetime
    updated_at: datetime
