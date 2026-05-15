import hashlib
import sqlite3
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from typing import Any

import bcrypt
from litestar import Request
from litestar import Response
from litestar import Router
from litestar import get
from litestar import post
from litestar.response import Redirect
from litestar.response import Template

from compute_space.core.auth.auth import AuthenticatedAccessor
from compute_space.core.auth.jwt_tokens import REFRESH_TOKEN_EXPIRY
from compute_space.core.auth.jwt_tokens import create_access_token
from compute_space.core.auth.jwt_tokens import create_refresh_token
from compute_space.web.auth.cookies import COOKIE_ACCESS
from compute_space.web.auth.cookies import COOKIE_REFRESH
from compute_space.web.auth.cookies import build_auth_cookies
from compute_space.web.auth.cookies import clear_auth_cookies


def _accessor(request: Request[Any, Any, Any]) -> AuthenticatedAccessor | None:
    state = request.scope.get("state") or {}
    return state.get("accessor")


def _attach_cookies(response: Response[Any], cookies: list[Any]) -> Response[Any]:
    for cookie in cookies:
        response.set_cookie(cookie)
    return response


@get("/login")
async def login_get(request: Request[Any, Any, Any]) -> Response[Any]:
    if _accessor(request) is not None:
        return Redirect(path="/")

    # Clear stale cookies (e.g. JWT signed by a now-rotated key) so they don't conflict
    # with the fresh ones set after a successful login.
    if request.cookies.get(COOKIE_ACCESS) is not None:
        host = request.headers.get("host", "")
        return _attach_cookies(Redirect(path="/login"), clear_auth_cookies(request_host=host))

    return Template(template_name="login.html")


@post("/login", status_code=200)
async def login_post(request: Request[Any, Any, Any], db: sqlite3.Connection) -> Response[Any]:
    owner = db.execute("SELECT * FROM owner").fetchone()
    if owner is None:
        return Redirect(path="/setup")

    form = await request.form()
    password = form.get("password", "")
    if not bcrypt.checkpw(password.encode(), owner["password_hash"].encode()):
        return Template(template_name="login.html", context={"error": "Invalid password"})

    refresh_token = create_refresh_token()
    refresh_token_hash = hashlib.sha256(refresh_token.encode()).hexdigest()
    expires_at = datetime.now(UTC) + timedelta(seconds=REFRESH_TOKEN_EXPIRY)
    db.execute(
        "INSERT INTO refresh_tokens (token_hash, expires_at) VALUES (?, ?)",
        (refresh_token_hash, expires_at.isoformat()),
    )
    db.commit()

    access_token = create_access_token(owner["username"])
    cookies = build_auth_cookies(access_token, refresh_token, request_host=request.headers.get("host", ""))
    return _attach_cookies(Redirect(path="/"), cookies)


@post("/logout", status_code=200)
async def logout(request: Request[Any, Any, Any], db: sqlite3.Connection) -> Response[Any]:
    refresh_tok = request.cookies.get(COOKIE_REFRESH)
    if refresh_tok:
        refresh_tok_hash = hashlib.sha256(refresh_tok.encode()).hexdigest()
        db.execute("UPDATE refresh_tokens SET revoked = 1 WHERE token_hash = ?", (refresh_tok_hash,))
        db.commit()

    host = request.headers.get("host", "")
    return _attach_cookies(Redirect(path="/login"), clear_auth_cookies(request_host=host))


pages_login_routes = Router(path="/", route_handlers=[login_get, login_post, logout])
