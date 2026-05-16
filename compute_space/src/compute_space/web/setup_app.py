"""Setup-only Litestar app: served once at first boot to provision the owner.

When the setup handler successfully creates the owner row, it triggers shutdown via
``trigger_restart()``; ``start.py`` then proceeds to boot the full app.
"""

import os
import secrets
from pathlib import Path
from typing import Any

import bcrypt
from litestar import Litestar
from litestar import MediaType
from litestar import Request
from litestar import Response
from litestar import get
from litestar import post
from litestar.contrib.jinja import JinjaTemplateEngine
from litestar.di import Provide
from litestar.response import Redirect
from litestar.response import Template
from litestar.static_files import create_static_files_router
from litestar.template.config import TemplateConfig

from compute_space.config import Config
from compute_space.config import provide_config
from compute_space.core.auth.auth import create_session
from compute_space.core.default_apps import deploy_default_apps
from compute_space.core.logging import logger
from compute_space.core.updates import trigger_restart
from compute_space.db import close_db
from compute_space.db import get_db
from compute_space.web.auth.cookies import build_session_cookie


def _verify_claim_token(claim_token: str, claim_token_path: str) -> bool:
    """Compare ``claim_token`` against the token written to ``claim_token_path``."""
    if not claim_token:
        return False
    try:
        with open(claim_token_path) as f:
            content = f.read().strip()
    except FileNotFoundError:
        return False
    stored_token = content.split(":", 1)[0]
    return secrets.compare_digest(claim_token, stored_token)


def _claim_token_required(config: Config) -> bool:
    return os.path.isfile(config.claim_token_path)


def _claim_unauthorized() -> Response[str]:
    return Response(content="Invalid or missing claim token.", status_code=403, media_type=MediaType.TEXT)


@get("/setup")
async def setup_get(request: Request[Any, Any, Any], config: Config) -> Template | Response[str]:
    claim_token = request.query_params.get("claim", "")
    if _claim_token_required(config) and not _verify_claim_token(claim_token, config.claim_token_path):
        return _claim_unauthorized()
    return Template(template_name="setup.html", context={"claim": claim_token})


@post("/setup", status_code=200)
async def setup_post(request: Request[Any, Any, Any], config: Config) -> Response[Any]:
    form = await request.form()
    form_claim = form.get("claim", "")
    if _claim_token_required(config) and not _verify_claim_token(form_claim, config.claim_token_path):
        return _claim_unauthorized()

    password = form.get("password", "")
    confirm = form.get("confirm_password", "")
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

    db = get_db()
    password_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
    cursor = db.execute(
        "INSERT INTO users (username, password_hash) VALUES ('owner', ?)",
        (password_hash,),
    )
    user_id = cursor.lastrowid
    assert user_id is not None
    session_token = create_session(user_id, db)
    db.commit()

    try:
        os.remove(config.claim_token_path)
    except OSError:
        pass

    try:
        deploy_default_apps(config, db)
    except Exception as exc:
        logger.error("default_apps deploy raised unexpectedly: %s", exc)

    request_host = request.headers.get("host", "")
    response: Redirect = Redirect(path="/")
    response.set_cookie(build_session_cookie(session_token, request_host=request_host))

    # Shutdown the setup server; start.py will boot the full app next.
    trigger_restart()
    return response


async def _close_db_after(response: Response[Any]) -> Response[Any]:
    close_db()
    return response


def create_setup_app(config: Config) -> Litestar:
    """Build the minimal Litestar app served until the owner is provisioned."""
    del config  # unused; the config singleton is set in start.py before this is called
    web_dir = Path(__file__).parent
    template_dir = web_dir / "templates"
    static_dir = web_dir / "static"

    template_config: TemplateConfig[JinjaTemplateEngine] = TemplateConfig(
        directory=template_dir,
        engine=JinjaTemplateEngine,
    )
    static_router = create_static_files_router(path="/static", directories=[static_dir])

    return Litestar(
        route_handlers=[setup_get, setup_post, static_router],
        template_config=template_config,
        after_request=_close_db_after,
        dependencies={"config": Provide(provide_config, sync_to_thread=False)},
    )
