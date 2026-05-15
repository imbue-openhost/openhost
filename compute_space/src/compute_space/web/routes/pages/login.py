import hashlib
import secrets
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from urllib.parse import quote
from urllib.parse import urlparse

import bcrypt
from quart import Blueprint
from quart import redirect
from quart import render_template
from quart import request
from quart import url_for
from quart.typing import ResponseReturnValue

from compute_space.config import get_config
from compute_space.core.auth.tokens import REFRESH_TOKEN_EXPIRY
from compute_space.core.auth.tokens import create_access_token
from compute_space.db import get_db
from compute_space.web.auth.auth import attach_refreshed_token
from compute_space.web.auth.cookies import COOKIE_ACCESS
from compute_space.web.auth.cookies import COOKIE_REFRESH
from compute_space.web.auth.cookies import clear_auth_cookies
from compute_space.web.auth.cookies import set_auth_cookies
from compute_space.web.auth.middleware import get_current_user_from_request

auth_bp = Blueprint("auth", __name__)
auth_bp.after_app_request(attach_refreshed_token)


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
        return redirect(url_for("setup.setup"))

    if request.method == "GET":
        if has_stale_cookies:
            next_param = request.args.get("next", "")
            login_url = url_for("auth.login")
            if next_param:
                login_url += f"?next={quote(next_param, safe='')}"
            response = redirect(login_url)
            clear_auth_cookies(response, request=request)  # type: ignore[arg-type]
            return response
        return await render_template("login.html", next=request.args.get("next", ""))

    form = await request.form
    password = form.get("password", "")
    next_url = form.get("next", "")

    if not bcrypt.checkpw(password.encode(), owner["password_hash"].encode()):
        return await render_template("login.html", error="Invalid password", next=next_url)

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

    dest = url_for("apps.dashboard")
    if next_url:
        parsed = urlparse(next_url)
        zone = get_config().zone_domain
        if parsed.scheme == "https" and (parsed.netloc == zone or parsed.netloc.endswith("." + zone)):
            dest = next_url
        elif not parsed.scheme and not parsed.netloc:
            dest = next_url

    response = redirect(dest)
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
