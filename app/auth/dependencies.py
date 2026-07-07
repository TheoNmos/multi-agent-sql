from __future__ import annotations

from typing import Annotated

from fastapi import Depends, HTTPException, Request, Response
from fastapi.responses import RedirectResponse

from app.auth import db as auth_db
from app.auth.models import CurrentUser
from app.auth.sessions import resolve_user_from_token
from app.config import auth_settings

_DEV_USER = CurrentUser(id="__dev__", username="dev", is_active=True)


def _cookie_secure(request: Request) -> bool:
    if auth_settings.auth_cookie_secure is not None:
        return auth_settings.auth_cookie_secure
    host = request.url.hostname or ""
    return host not in {"localhost", "127.0.0.1", "0.0.0.0"}


def _read_session_token(request: Request) -> str | None:
    return request.cookies.get(auth_settings.auth_cookie_name)


def _unauthorized_response(request: Request) -> None:
    accept = request.headers.get("accept", "")
    hx_request = request.headers.get("hx-request")
    if hx_request or "application/json" in accept or request.url.path.startswith("/api/"):
        raise HTTPException(status_code=401, detail="Not authenticated", headers={"HX-Redirect": "/login"})
    raise HTTPException(status_code=401, detail="Not authenticated")


async def get_optional_user(request: Request) -> CurrentUser | None:
    if auth_settings.auth_disabled:
        if auth_db.is_app_db_ready():
            user = await auth_db.ensure_dev_user()
            return CurrentUser(id=user.id, username=user.username, is_active=user.is_active)
        return _DEV_USER
    if not auth_db.is_app_db_ready():
        return None
    return await resolve_user_from_token(_read_session_token(request))


async def require_user(request: Request) -> CurrentUser:
    user = await get_optional_user(request)
    if user is None:
        _unauthorized_response(request)
    assert user is not None
    return user


async def require_user_or_redirect(request: Request) -> CurrentUser | RedirectResponse:
    user = await get_optional_user(request)
    if user is None:
        return RedirectResponse(url="/login", status_code=303)
    return user


def set_session_cookie(response: Response, request: Request, token: str) -> None:
    max_age = auth_settings.auth_session_ttl_hours * 3600
    response.set_cookie(
        key=auth_settings.auth_cookie_name,
        value=token,
        httponly=True,
        secure=_cookie_secure(request),
        samesite="lax",
        max_age=max_age,
        path="/",
    )


def clear_session_cookie(response: Response, request: Request) -> None:
    response.delete_cookie(
        key=auth_settings.auth_cookie_name,
        path="/",
        secure=_cookie_secure(request),
        httponly=True,
        samesite="lax",
    )


CurrentUserDep = Annotated[CurrentUser, Depends(require_user)]
