import hashlib
import secrets
from datetime import UTC
from datetime import datetime
from datetime import timedelta

import bcrypt
from quart import Blueprint
from quart import redirect
from quart import render_template
from quart import request
from quart import url_for
from quart.typing import ResponseReturnValue

from compute_space.core.auth.auth import get_current_user_from_request
from compute_space.core.auth.tokens import REFRESH_TOKEN_EXPIRY
from compute_space.core.auth.tokens import create_access_token
from compute_space.db import get_db
from compute_space.web.auth.cookies import COOKIE_ACCESS
from compute_space.web.auth.cookies import COOKIE_REFRESH
from compute_space.web.auth.cookies import clear_auth_cookies
from compute_space.web.auth.cookies import set_auth_cookies
from compute_space.web.auth.middleware import _try_refresh  # noqa: F401 — re-exported
from compute_space.web.auth.middleware import login_required  # noqa: F401 — re-exported

auth_bp = Blueprint("auth", __name__)


@auth_bp.route("/login", methods=["GET", "POST"])
async def login() -> ResponseReturnValue:
    if get_current_user_from_request(request):
        return redirect(url_for("apps.dashboard"))

    # If stale auth cookies are present (invalid JWT, e.g. after key rotation on
    # reboot or TLS mode change), clear them now so they don't conflict with the
    # fresh cookies we'll set after successful login.
    has_stale_cookies = request.cookies.get(COOKIE_ACCESS) is not None

    db = get_db()
    owner = db.execute("SELECT * FROM owner").fetchone()
    if owner is None:
        return redirect(url_for("auth.setup"))

    if request.method == "GET":
        if has_stale_cookies:
            response = redirect(url_for("auth.login"))
            clear_auth_cookies(response, request=request)  # type: ignore[arg-type]
            return response
        return await render_template("login.html")

    form = await request.form
    password = form.get("password", "")

    if not bcrypt.checkpw(password.encode(), owner["password_hash"].encode()):
        return await render_template("login.html", error="Invalid password")

    access_token = create_access_token("owner")
    refresh_token = secrets.token_urlsafe(48)
    refresh_token_hash = hashlib.sha256(refresh_token.encode()).hexdigest()
    expires_at = datetime.now(UTC) + timedelta(
        seconds=REFRESH_TOKEN_EXPIRY,
    )
    db.execute(
        "INSERT INTO refresh_tokens (token_hash, expires_at) VALUES (?, ?)",
        (refresh_token_hash, expires_at.isoformat()),
    )
    db.commit()

    response = redirect(url_for("apps.dashboard"))
    set_auth_cookies(response, access_token, refresh_token, request=request)  # type: ignore[arg-type]
    return response


@auth_bp.route("/logout", methods=["POST"])
def logout() -> ResponseReturnValue:
    refresh_tok = request.cookies.get(COOKIE_REFRESH)
    if refresh_tok:
        refresh_tok_hash = hashlib.sha256(refresh_tok.encode()).hexdigest()
        db = get_db()
        db.execute(
            "UPDATE refresh_tokens SET revoked = 1 WHERE token_hash = ?",
            (refresh_tok_hash,),
        )
        db.commit()

    response = redirect(url_for("auth.login"))
    clear_auth_cookies(response, request=request)  # type: ignore[arg-type]
    return response
