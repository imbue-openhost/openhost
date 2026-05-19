import sqlite3
from typing import Any

from litestar import Request
from litestar import Response
from litestar import Router
from litestar import get
from litestar import post
from litestar.response import Redirect
from litestar.response import Template

from compute_space.config import Config
from compute_space.core.auth.auth import SESSION_COOKIE_NAME
from compute_space.core.auth.auth import create_session
from compute_space.core.auth.auth import revoke_session
from compute_space.core.auth.auth import validate_password
from compute_space.web.auth.auth import authenticate
from compute_space.web.auth.cookies import build_session_cookie
from compute_space.web.auth.cookies import clear_session_cookie


@get("/login")
async def login_get(request: Request[Any, Any, Any], db: sqlite3.Connection) -> Response[Any]:
    if authenticate(request, db=db) is not None:
        return Redirect(path="/")
    return Template(template_name="login.html")


@post("/login", status_code=200)
async def login_post(request: Request[Any, Any, Any], db: sqlite3.Connection, config: Config) -> Response[Any]:
    form = await request.form()
    password = form.get("password")
    if password is None or not (user_id := validate_password(password, db)):
        return Template(template_name="login.html", context={"error": "Invalid password"})

    session_token = create_session(user_id, db)
    db.commit()

    response = Redirect(path="/")
    # cookie domain is zone_domain_no_port, ie `host.example.com` (no port); this will cover also `app.host.example.com`
    response.set_cookie(build_session_cookie(session_token, cookie_domain=config.zone_domain_no_port))
    return response


@post("/logout", status_code=200)
async def logout(request: Request[Any, Any, Any], db: sqlite3.Connection, config: Config) -> Response[Any]:
    if session_token := request.cookies.get(SESSION_COOKIE_NAME):
        revoke_session(session_token, db)
        db.commit()

    response: Response[Any] = Redirect(path="/login")
    response.set_cookie(clear_session_cookie(cookie_domain=config.zone_domain_no_port))
    return response


pages_login_routes = Router(path="/", route_handlers=[login_get, login_post, logout])
