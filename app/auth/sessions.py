from __future__ import annotations

from app.auth import db as auth_db
from app.auth.models import CurrentUser


async def authenticate_user(username: str, password: str) -> CurrentUser | None:
    from app.auth.passwords import verify_password

    user = await auth_db.get_user_by_username(username)
    if user is None or not user.is_active:
        return None
    if not verify_password(password, user.password_hash):
        return None
    return CurrentUser(id=user.id, username=user.username, is_active=user.is_active)


async def create_login_session(user: CurrentUser) -> tuple[str, str]:
    raw_token, expires_at = await auth_db.create_auth_session(user.id)
    return raw_token, expires_at.isoformat()


async def resolve_user_from_token(raw_token: str | None) -> CurrentUser | None:
    if not raw_token:
        return None
    user_id = await auth_db.get_user_id_for_session_token(raw_token)
    if user_id is None:
        return None
    user = await auth_db.get_user_by_id(user_id)
    if user is None or not user.is_active:
        return None
    return CurrentUser(id=user.id, username=user.username, is_active=user.is_active)


async def logout_session(raw_token: str | None) -> None:
    if raw_token:
        await auth_db.revoke_auth_session(raw_token)
