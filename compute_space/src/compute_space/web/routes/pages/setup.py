import hashlib
import os
import secrets
from datetime import UTC
from datetime import datetime
from datetime import timedelta

import bcrypt
from quart import Blueprint
from quart import current_app
from quart import redirect
from quart import render_template
from quart import request
from quart import url_for
from quart.typing import ResponseReturnValue

from compute_space.config import get_config
from compute_space.core.auth.tokens import REFRESH_TOKEN_EXPIRY
from compute_space.core.auth.tokens import create_access_token
from compute_space.core.default_apps import deploy_default_apps
from compute_space.core.logging import logger
from compute_space.db import get_db
from compute_space.web.auth.auth import attach_refreshed_token
from compute_space.web.auth.cookies import set_auth_cookies

setup_bp = Blueprint("setup", __name__)
setup_bp.after_app_request(attach_refreshed_token)


def verify_claim_token(claim_token: str) -> bool | None:
    """Verify a claim token against the on-disk claim file.

    Returns True if valid, None if invalid.
    """
    if not claim_token:
        return None

    config = get_config()
    token_path = config.claim_token_path
    try:
        with open(token_path) as f:
            content = f.read().strip()
    except FileNotFoundError:
        return None

    # Claim file format is "token:username" (username is legacy, ignored)
    stored_token = content.split(":", 1)[0]

    if not secrets.compare_digest(claim_token, stored_token):
        return None

    return True


@setup_bp.route("/setup", methods=["GET", "POST"])
async def setup() -> ResponseReturnValue:
    """First-time owner setup. Only accessible when no owner exists.

    If a claim token file exists on disk (provider mode), the claim token
    must be provided as ?claim=<token> to access this page.
    """
    config = get_config()

    # If owner already exists, setup is complete
    db = get_db()
    owner = db.execute("SELECT * FROM owner").fetchone()
    if owner is not None:
        return "This instance has already been set up.", 403

    # If a claim token file exists, validate the claim token from the URL
    claim_token = request.args.get("claim", "")
    if os.path.isfile(config.claim_token_path):
        if verify_claim_token(claim_token) is None:
            return "Invalid or missing claim token.", 403

    if request.method == "GET":
        return await render_template("setup.html", claim=claim_token)

    form = await request.form
    # Re-validate claim token on POST
    form_claim = form.get("claim", "")
    if os.path.isfile(config.claim_token_path):
        if verify_claim_token(form_claim) is None:
            return "Invalid or missing claim token.", 403

    password = form.get("password", "")
    confirm = form.get("confirm_password", "")

    if not password:
        return await render_template("setup.html", error="Password is required", claim=form_claim)
    if password != confirm:
        return await render_template("setup.html", error="Passwords do not match", claim=form_claim)

    password_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()

    db.execute(
        "INSERT INTO owner (id, username, password_hash) VALUES (1, 'owner', ?)",
        (password_hash,),
    )

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

    # Delete the claim token file so it can't be reused
    try:
        os.remove(config.claim_token_path)
    except OSError:
        pass

    # Mark owner as verified so subdomain routing activates
    current_app._owner_verified = True  # type: ignore[attr-defined]

    try:
        deploy_default_apps(config, db)
    except Exception as exc:
        logger.error("default_apps deploy raised unexpectedly: %s", exc)

    response = redirect(url_for("apps.dashboard"))
    set_auth_cookies(response, access_token, refresh_token, request=request)  # type: ignore[arg-type]
    return response
