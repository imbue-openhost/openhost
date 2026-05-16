import sqlite3
from typing import Any

import bcrypt
from litestar import Request
from litestar import Response
from litestar import Router
from litestar import get
from litestar import post
from litestar.response import Redirect
from litestar.response import Template

from compute_space.core.auth.auth import SESSION_COOKIE_NAME
from compute_space.core.auth.auth import AuthenticatedAccessor
from compute_space.core.auth.auth import create_session
from compute_space.core.auth.auth import revoke_session
from compute_space.web.auth.cookies import build_session_cookie
from compute_space.web.auth.cookies import clear_session_cookie


def _accessor(request: Request[Any, Any, Any]) -> AuthenticatedAccessor | None:
    state = request.scope.get("state") or {}
    return state.get("accessor")


@get("/login")
async def login_get(request: Request[Any, Any, Any]) -> Response[Any]:
    if _accessor(request) is not None:
        return Redirect(path="/")

    # Clear a stale session cookie (e.g. one whose row was deleted server-side) so it
    # doesn't conflict with the fresh one set after a successful login.
    if request.cookies.get(SESSION_COOKIE_NAME) is not None:
        host = request.headers.get("host", "")
        response: Response[Any] = Redirect(path="/login")
        response.set_cookie(clear_session_cookie(request_host=host))
        return response

    return Template(template_name="login.html")


@post("/login", status_code=200)
async def login_post(request: Request[Any, Any, Any], db: sqlite3.Connection) -> Response[Any]:
    user = db.execute("SELECT user_id, password_hash FROM users LIMIT 1").fetchone()
    if user is None:
        return Redirect(path="/setup")

    form = await request.form()
    password = form.get("password", "")
    if not bcrypt.checkpw(password.encode(), user["password_hash"].encode()):
        return Template(template_name="login.html", context={"error": "Invalid password"})

    session_token = create_session(user["user_id"], db)
    db.commit()

    response: Response[Any] = Redirect(path="/")
    response.set_cookie(build_session_cookie(session_token, request_host=request.headers.get("host", "")))
    return response


@post("/logout", status_code=200)
async def logout(request: Request[Any, Any, Any], db: sqlite3.Connection) -> Response[Any]:
    if session_token := request.cookies.get(SESSION_COOKIE_NAME):
        revoke_session(session_token, db)
        db.commit()

    host = request.headers.get("host", "")
    response: Response[Any] = Redirect(path="/login")
    response.set_cookie(clear_session_cookie(request_host=host))
    return response


pages_login_routes = Router(path="/", route_handlers=[login_get, login_post, logout])
