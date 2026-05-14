import atexit
import os
import sqlite3
from pathlib import Path
from typing import Any

from litestar import Litestar
from litestar import Request
from litestar import Response
from litestar.contrib.jinja import JinjaTemplateEngine
from litestar.datastructures import State
from litestar.di import Provide
from litestar.exceptions import NotAuthorizedException
from litestar.openapi.config import OpenAPIConfig
from litestar.response import Redirect
from litestar.static_files import create_static_files_router
from litestar.template.config import TemplateConfig

from compute_space.config import Config
from compute_space.config import load_config
from compute_space.config import set_active_config
from compute_space.core import archive_backend
from compute_space.core.auth.identity import load_identity_keys
from compute_space.core.auth.keys import load_keys
from compute_space.core.logging import setup_file_logging
from compute_space.core.startup import check_app_status
from compute_space.core.startup import retry_pending_default_apps
from compute_space.core.storage import start_storage_guard
from compute_space.core.terminal import cleanup_all as cleanup_terminal
from compute_space.db import close_db
from compute_space.db import get_db
from compute_space.db import init_db
from compute_space.web.auth.middleware import login_required_redirect
from compute_space.web.auth.middleware import provide_app_id
from compute_space.web.auth.middleware import provide_user
from compute_space.web.middleware.auth_refresh import AuthRefreshMiddleware
from compute_space.web.middleware.subdomain_proxy import SubdomainProxyMiddleware
from compute_space.web.routes.api.settings import api_settings_routes
from compute_space.web.routes.pages.settings import pages_settings_routes

# Endpoint-name → URL-path map used by the templating ``url_for`` shim.
# Subsequent PRs will add entries as they port more routes; entries here for
# unmigrated routes ensure templates still render plausible hrefs even though
# clicking them will 404 until the route is ported.
_ROUTE_NAME_TO_PATH: dict[str, str] = {
    "pages_settings.settings_page": "/settings",
    # Layout nav — paths kept identical to the unmigrated Quart routes so the
    # nav doesn't visibly break when those pages are migrated later.
    "apps.dashboard": "/dashboard",
    "apps.add_app": "/add_app",
    "apps.app_detail": "/app_detail/{app_id}",
    "pages_system.system_page": "/system",
    "pages_system.logs_page": "/logs",
    "pages_system.terminal_page": "/terminal",
}


def _url_for(endpoint: str, **kwargs: Any) -> str:
    """Jinja shim for Quart's ``url_for``.

    Returns the path for ``endpoint`` from ``_ROUTE_NAME_TO_PATH`` with any
    keyword args interpolated.  Unknown endpoints fall back to ``"#"`` so the
    template renders rather than crashes.
    """
    template = _ROUTE_NAME_TO_PATH.get(endpoint, "#")
    if kwargs:
        try:
            return template.format(**kwargs)
        except (KeyError, IndexError):
            return template
    return template


def _make_static_url(static_dir: Path) -> Any:
    """Build a Jinja ``static_url`` global that appends ``?v=<mtime>`` for cache-busting.

    Browsers aggressively cache static JS/CSS, so a deploy that ships a new
    template + JS would otherwise leave returning visitors running stale JS
    against new HTML.  Appending the file's mtime forces a fresh fetch.
    """

    def static_url(filename: str) -> str:
        base = f"/static/{filename}"
        try:
            mtime = int((static_dir / filename).stat().st_mtime)
        except OSError:
            return base
        return f"{base}?v={mtime}"

    return static_url


def _template_globals(config: Config, static_dir: Path) -> dict[str, Any]:
    zone_domain = config.zone_domain
    zone_name = zone_domain.split(".")[0] if zone_domain else None

    def app_url(app_name: str) -> str:
        proto = "https" if config.tls_enabled else "http"
        return f"{proto}://{app_name}.{zone_domain}/"

    return {
        "zone_name": zone_name,
        "zone_domain": zone_domain,
        "app_url": app_url,
        "static_url": _make_static_url(static_dir),
        "url_for": _url_for,
    }


def _bootstrap(config: Config) -> None:
    """Initialize DB and on-startup app state.  Must be called before the Litestar app handles requests."""
    init_db(config.db_path)
    db = sqlite3.connect(config.db_path)
    try:
        archive_backend.attach_on_startup(config, db)
    finally:
        db.close()
    check_app_status(config)
    load_identity_keys(config.persistent_data_dir)
    start_storage_guard(config)
    retry_pending_default_apps(config)


_PUBLIC_PATHS: frozenset[str] = frozenset(
    {
        "/setup",
        "/health",
        "/.well-known/jwks.json",
        "/.well-known/openhost-identity",
    }
)


async def _require_owner(request: Request[Any, Any, Any]) -> Response[Any] | None:
    """Redirect to /setup if no owner exists yet, except for public bootstrap paths."""
    state: State = request.app.state
    if getattr(state, "owner_verified", False):
        return None

    path = request.url.path
    if path in _PUBLIC_PATHS:
        return None
    # Docs (``/docs/`` landing + ``/docs/<anything>``) must be readable
    # before the zone owner is provisioned — operators usually consult them
    # *before* finishing setup.
    if path == "/docs" or path.startswith("/docs/"):
        return None
    # OpenAPI schema and explorer UIs are public.
    if path == "/openapi" or path.startswith("/openapi/"):
        return None
    # Static assets are public — the dashboard will fetch CSS/JS before login.
    if path.startswith("/static/"):
        return None

    db = get_db()
    owner = db.execute("SELECT 1 FROM owner LIMIT 1").fetchone()
    if owner is not None:
        state.owner_verified = True
        return None

    claim = request.query_params.get("claim", "")
    target = f"/setup?claim={claim}" if claim else "/setup"
    return Redirect(path=target)


async def _close_db_after(response: Response[Any]) -> Response[Any]:
    close_db()
    return response


def create_app(config: Config | None = None) -> Litestar:
    if config is None:
        config = load_config()
    set_active_config(config)

    setup_file_logging(Path(os.path.dirname(config.db_path)) / "compute_space.log")
    load_keys(config.keys_dir)

    _bootstrap(config)

    web_dir = Path(__file__).parent
    static_dir = web_dir / "static"
    template_dir = web_dir / "templates"

    template_config: TemplateConfig[JinjaTemplateEngine] = TemplateConfig(
        directory=template_dir,
        engine=JinjaTemplateEngine,
    )

    def _install_template_globals(app: Litestar) -> None:
        engine = app.template_engine
        if isinstance(engine, JinjaTemplateEngine):
            engine.engine.globals.update(_template_globals(config, static_dir))

    static_router = create_static_files_router(path="/static", directories=[static_dir])

    atexit.register(cleanup_terminal)

    return Litestar(
        route_handlers=[
            static_router,
            api_settings_routes,
            pages_settings_routes,
        ],
        middleware=[SubdomainProxyMiddleware, AuthRefreshMiddleware],
        template_config=template_config,
        before_request=_require_owner,
        after_request=_close_db_after,
        dependencies={
            "user": Provide(provide_user),
            "app_id": Provide(provide_app_id),
        },
        exception_handlers={NotAuthorizedException: login_required_redirect},
        on_startup=[_install_template_globals],
        state=State({"owner_verified": False}),
        openapi_config=OpenAPIConfig(title="compute_space", version="0.1.0", path="/openapi"),
    )
