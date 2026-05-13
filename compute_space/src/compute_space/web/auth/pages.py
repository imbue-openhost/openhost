import hashlib
import os
import secrets
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from typing import Annotated
from typing import Any

import attr
import bcrypt
from litestar import Request
from litestar import Response
from litestar import get
from litestar import post
from litestar.enums import RequestEncodingType
from litestar.params import Body
from litestar.response import Redirect
from litestar.response import Template

from compute_space.config import get_config
from compute_space.core import auth
from compute_space.core.default_apps import deploy_default_apps
from compute_space.core.logging import logger
from compute_space.db import get_db
from compute_space.web.auth.cookies import auth_cookies
from compute_space.web.auth.cookies import cleared_auth_cookies
from compute_space.web.auth.inputs import auth_inputs_from_connection


@attr.s(auto_attribs=True, frozen=True)
class SetupForm:
    password: str = ""
    confirm_password: str = ""
    claim: str = ""


@attr.s(auto_attribs=True, frozen=True)
class LoginForm:
    password: str = ""


def _verify_claim_token(claim_token: str) -> bool | None:
    if not claim_token:
        return None
    config = get_config()
    token_path = config.claim_token_path
    try:
        with open(token_path) as f:
            content = f.read().strip()
    except FileNotFoundError:
        return None
    stored_token = content.split(":", 1)[0]
    if not secrets.compare_digest(claim_token, stored_token):
        return None
    return True


def _request_host(request: Request[Any, Any, Any]) -> str:
    return request.headers.get("host", "")


@get("/setup", sync_to_thread=False)
def setup_get(request: Request[Any, Any, Any], claim: str = "") -> Response[Any] | Template:
    config = get_config()
    db = get_db()
    owner = db.execute("SELECT * FROM owner").fetchone()
    if owner is not None:
        return Response(content="This instance has already been set up.", status_code=403, media_type="text/plain")
    if os.path.isfile(config.claim_token_path):
        if _verify_claim_token(claim) is None:
            return Response(content="Invalid or missing claim token.", status_code=403, media_type="text/plain")
    return Template(template_name="setup.html", context={"claim": claim})


@post("/setup", status_code=200)
async def setup_post(
    request: Request[Any, Any, Any],
    data: Annotated[SetupForm, Body(media_type=RequestEncodingType.URL_ENCODED)],
) -> Response[Any] | Template | Redirect:
    config = get_config()
    db = get_db()
    owner = db.execute("SELECT * FROM owner").fetchone()
    if owner is not None:
        return Response(content="This instance has already been set up.", status_code=403, media_type="text/plain")

    form_claim = data.claim or ""
    if os.path.isfile(config.claim_token_path):
        if _verify_claim_token(form_claim) is None:
            return Response(content="Invalid or missing claim token.", status_code=403, media_type="text/plain")

    password = data.password or ""
    confirm = data.confirm_password or ""
    if not password:
        return Template(
            template_name="setup.html",
            context={"error": "Password is required", "claim": form_claim},
        )
    if password != confirm:
        return Template(
            template_name="setup.html",
            context={"error": "Passwords do not match", "claim": form_claim},
        )

    password_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
    db.execute(
        "INSERT INTO owner (id, username, password_hash) VALUES (1, 'owner', ?)",
        (password_hash,),
    )

    access_token = auth.create_access_token("owner")
    refresh_token = secrets.token_urlsafe(48)
    refresh_token_hash = hashlib.sha256(refresh_token.encode()).hexdigest()
    expires_at = datetime.now(UTC) + timedelta(seconds=auth.REFRESH_TOKEN_EXPIRY)
    db.execute(
        "INSERT INTO refresh_tokens (token_hash, expires_at) VALUES (?, ?)",
        (refresh_token_hash, expires_at.isoformat()),
    )
    db.commit()

    try:
        os.remove(config.claim_token_path)
    except OSError:
        pass

    request.app.state.owner_verified = True

    try:
        deploy_default_apps(config, db)
    except Exception as exc:
        logger.error("default_apps deploy raised unexpectedly: %s", exc)

    response: Response[Any] = Response(content=b"", status_code=200, media_type="text/plain")
    for cookie in auth_cookies(access_token, refresh_token, request_host=_request_host(request)):
        response.set_cookie(cookie)
    return response


@get("/login", sync_to_thread=False)
def login_get(request: Request[Any, Any, Any]) -> Response[Any] | Template | Redirect:
    if auth.get_current_user(auth_inputs_from_connection(request)):
        return Redirect(path="/dashboard")

    has_stale_cookies = request.cookies.get(auth.COOKIE_ACCESS) is not None
    db = get_db()
    owner = db.execute("SELECT * FROM owner").fetchone()
    if owner is None:
        return Redirect(path="/setup")

    if has_stale_cookies:
        response: Redirect = Redirect(path="/login")
        for cookie in cleared_auth_cookies(_request_host(request)):
            response.set_cookie(cookie)
        return response
    return Template(template_name="login.html")


@post("/login", status_code=200)
async def login_post(
    request: Request[Any, Any, Any],
    data: Annotated[LoginForm, Body(media_type=RequestEncodingType.URL_ENCODED)],
) -> Response[Any] | Template | Redirect:
    db = get_db()
    owner = db.execute("SELECT * FROM owner").fetchone()
    if owner is None:
        return Redirect(path="/setup")

    password = data.password or ""
    if not bcrypt.checkpw(password.encode(), owner["password_hash"].encode()):
        return Template(template_name="login.html", context={"error": "Invalid password"})

    access_token = auth.create_access_token("owner")
    refresh_token = secrets.token_urlsafe(48)
    refresh_token_hash = hashlib.sha256(refresh_token.encode()).hexdigest()
    expires_at = datetime.now(UTC) + timedelta(seconds=auth.REFRESH_TOKEN_EXPIRY)
    db.execute(
        "INSERT INTO refresh_tokens (token_hash, expires_at) VALUES (?, ?)",
        (refresh_token_hash, expires_at.isoformat()),
    )
    db.commit()

    response: Redirect = Redirect(path="/dashboard")
    for cookie in auth_cookies(access_token, refresh_token, request_host=_request_host(request)):
        response.set_cookie(cookie)
    return response


@post("/logout", sync_to_thread=False, status_code=200)
def logout(request: Request[Any, Any, Any]) -> Redirect:
    refresh_tok = request.cookies.get(auth.COOKIE_REFRESH)
    if refresh_tok:
        refresh_tok_hash = hashlib.sha256(refresh_tok.encode()).hexdigest()
        db = get_db()
        db.execute(
            "UPDATE refresh_tokens SET revoked = 1 WHERE token_hash = ?",
            (refresh_tok_hash,),
        )
        db.commit()
    response: Redirect = Redirect(path="/login")
    for cookie in cleared_auth_cookies(_request_host(request)):
        response.set_cookie(cookie)
    return response


auth_pages_routes = [setup_get, setup_post, login_get, login_post, logout]
