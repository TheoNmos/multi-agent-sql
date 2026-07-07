from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from app.auth.dependencies import (
    CurrentUserDep,
    clear_session_cookie,
    set_session_cookie,
)
from app.auth.sessions import authenticate_user, create_login_session, logout_session
from app.config import auth_settings

router = APIRouter(tags=["auth"])
templates = Jinja2Templates(directory="app/templates")


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse(request, "login.html", {"request": request})


@router.post("/api/auth/login")
async def login(
    request: Request,
    response: Response,
    username: Annotated[str, Form()],
    password: Annotated[str, Form()],
):
    user = await authenticate_user(username.strip(), password)
    if user is None:
        raise HTTPException(status_code=401, detail="Invalid username or password")
    token, _expires_at = await create_login_session(user)
    json_response = JSONResponse(content={"username": user.username, "user_id": user.id})
    set_session_cookie(json_response, request, token)
    return json_response


@router.post("/api/auth/logout")
async def logout(request: Request):
    await logout_session(request.cookies.get(auth_settings.auth_cookie_name))
    json_response = JSONResponse(content={"success": True})
    clear_session_cookie(json_response, request)
    return json_response


@router.get("/api/auth/me")
async def me(user: CurrentUserDep):
    return {"user_id": user.id, "username": user.username}
