import atexit
import os
import sqlite3
from pathlib import Path
from typing import Any

from litestar import HttpMethod
from litestar import Litestar
from litestar import MediaType
from litestar import Request
from litestar import Response
from litestar import route
from litestar.contrib.jinja import JinjaTemplateEngine
from litestar.di import Provide
from litestar.exceptions import HTTPException
from litestar.exceptions import NotAuthorizedException
from litestar.exceptions.responses import create_exception_response
from litestar.static_files import create_static_files_router
from litestar.template.config import TemplateConfig
from litestar.types import ASGIApp
from litestar.types import Message
from litestar.types import Receive
from litestar.types import Scope
from litestar.types import Send
from quart import Blueprint
from quart import Quart
from quart import current_app
from quart import url_for as quart_url_for

from compute_space.config import Config
from compute_space.config import get_config
from compute_space.config import provide_config
from compute_space.core import archive_backend
from compute_space.core.auth.identity import load_identity_keys
from compute_space.core.logging import logger
from compute_space.core.startup import check_app_status
from compute_space.core.startup import retry_pending_default_apps
from compute_space.core.storage import start_storage_guard
from compute_space.core.terminal import cleanup_all as cleanup_terminal
from compute_space.db import provide_db
from compute_space.web.auth.auth import login_required_redirect
from compute_space.web.middleware.subdomain_proxy import SubdomainProxyMiddleware
from compute_space.web.routes.api.archive_backend import api_archive_backend_routes
from compute_space.web.routes.api.identity import identity_routes
from compute_space.web.routes.api.permissions_v2 import api_permissions_v2_routes
from compute_space.web.routes.api.services_v2 import api_services_v2_routes
from compute_space.web.routes.api.settings import api_settings_routes
from compute_space.web.routes.api.system import system_routes
from compute_space.web.routes.pages.apps import pages_apps_routes
from compute_space.web.routes.pages.login import pages_login_routes
from compute_space.web.routes.pages.permissions_v2 import pages_permissions_v2_routes
from compute_space.web.routes.pages.settings import pages_settings_routes
from compute_space.web.routes.pages.system import pages_system_routes
from compute_space.web.routes.services_v2 import services_v2_routes


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


# Templates use ``url_for(endpoint, **kwargs)`` with the legacy Quart blueprint
# endpoint names.  Litestar's own ``url_for`` only knows about Litestar routes,
# so we map the legacy endpoint names to their paths directly here.  Values may
# use ``str.format``-style placeholders for routes with path params; the
# ``url_for`` shim runs them through ``str.format(**kwargs)``.
#
# Add a new entry whenever a template gains a ``url_for(...)`` call to an
# endpoint that isn't already listed.
_TEMPLATE_ENDPOINT_PATHS: dict[str, str] = {
    # layout.html (rendered for every page)
    "apps.dashboard": "/dashboard",
    "apps.add_app": "/add_app",
    "pages_system.system_page": "/system/",
    "pages_system.logs_page": "/logs/",
    "pages_system.terminal_page": "/terminal/",
    "pages_settings.settings_page": "/settings",
    # dashboard.html
    "apps.app_detail": "/app_detail/{app_id}",
    "api_apps.api_apps": "/api/apps",
    "system.api_tokens_list": "/api/tokens",
    "system.api_tokens_create": "/api/tokens",
    # app_detail.html
    "api_apps.stop_app": "/stop_app/{app_id}",
    "api_apps.reload_app": "/reload_app/{app_id}",
    "api_apps.remove_app": "/remove_app/{app_id}",
    "api_apps.rename_app": "/rename_app/{app_id}",
    "api_apps.app_logs": "/app_logs/{app_id}",
    "api_apps.app_status": "/api/app_status/{app_id}",
    "system.drop_docker_cache": "/api/drop-docker-cache",
    # system.html
    "system.security_audit": "/api/security-audit",
    "system.listening_ports": "/api/listening-ports",
    "system.api_storage_status": "/api/storage-status",
    "system.restart_router": "/restart_router",
    "system.ssh_status": "/api/ssh-status",
    "system.toggle_ssh": "/toggle-ssh",
    "system.toggle_storage_guard": "/api/storage-guard",
    "api_archive_backend.get_archive_backend": "/api/storage/archive_backend",
    "api_archive_backend.test_connection": "/api/storage/archive_backend/test_connection",
    "api_archive_backend.configure_archive_backend": "/api/storage/archive_backend/configure",
    # logs.html
    "system.compute_space_logs": "/api/compute_space_logs",
}


def _template_globals(config: Config, static_dir: Path) -> dict[str, Any]:
    zone_domain = config.zone_domain
    zone_name = zone_domain.split(".")[0] if zone_domain else None

    def app_url(app_name: str) -> str:
        proto = "https" if config.tls_enabled else "http"
        return f"{proto}://{app_name}.{zone_domain}/"

    def url_for(endpoint: str, **kwargs: Any) -> str:
        try:
            path = _TEMPLATE_ENDPOINT_PATHS[endpoint]
        except KeyError as e:
            raise KeyError(f"No template path mapping for endpoint {endpoint!r}") from e
        return path.format(**kwargs) if kwargs else path

    return {
        "zone_name": zone_name,
        "zone_domain": zone_domain,
        "app_url": app_url,
        "static_url": _make_static_url(static_dir),
        "url_for": url_for,
    }


def _full_app_bootstrap(config: Config) -> None:
    """Side-effects required before the full app handles requests.

    DB / keys / logging are already initialized in ``start.py``; this only covers the
    heavier setup steps that don't make sense for the setup-only app.
    """
    db = sqlite3.connect(config.db_path)
    try:
        archive_backend.attach_on_startup(config, db)
    finally:
        db.close()
    check_app_status(config)
    load_identity_keys(config.persistent_data_dir)
    start_storage_guard(config)
    retry_pending_default_apps(config)


@route("/setup", http_method=[HttpMethod.GET, HttpMethod.POST], status_code=403, sync_to_thread=False)
def setup_already_done() -> Response[str]:
    return Response(
        content="This instance has already been set up.",
        status_code=403,
        media_type=MediaType.TEXT,
    )


def _login_required_redirect(request: Request[Any, Any, Any], exc: NotAuthorizedException) -> Response[Any]:
    """Exception handler: redirect HTML clients to /login; JSON clients get 401.

    websocket-type requests should never get here - they start as HTTP requests with `Upgrade: websocket`, and should fail then.
    """
    if "application/json" in request.headers.get("Accept", ""):
        return Response(content={"error": exc.detail}, status_code=401)

    return login_required_redirect(request)


def _log_unhandled_exception(request: Request[Any, Any, Any], exc: Exception) -> Response[Any]:
    """Log a traceback for any exception not caught by a more specific handler.

    Litestar's default behaviour serialises the exception into a 500 JSON response
    but doesn't log it, so genuine bugs disappear silently.  Stay quiet for
    intentional 4xx HTTPException responses; log everything else (including 5xx
    HTTPException like NoRouteMatchFoundException which wraps real bugs).
    """
    status_code = getattr(exc, "status_code", 500)
    if not isinstance(exc, HTTPException) or status_code >= 500:
        logger.opt(exception=exc).error("Unhandled exception in {} {}", request.method, request.url.path)
    return create_exception_response(request=request, exc=exc)


def _reject_app_subdomain_requests(request: Request[Any, Any, Any]) -> Response[Any] | None:
    """Defense-in-depth: refuse any request whose Host is an app subdomain.

    App-subdomain traffic is supposed to be intercepted by SubdomainProxyMiddleware
    (outer ASGI) before Litestar ever sees it.  If a request reaches Litestar with
    a ``*.zone_domain`` Host — e.g. the middleware was bypassed in a test or a
    deployment variant — refuse it rather than accidentally serve a router route
    (like /health) under the app's hostname.
    """
    host = request.url.netloc.split(":", 1)[0]
    zone = get_config().zone_domain
    if zone and host.endswith("." + zone):
        return Response(content=None, status_code=404)
    return None


def _build_quart_fallback(config: Config, static_dir: Path) -> Quart:
    """Build a Quart app holding the blueprints not yet ported to Litestar.

    Mounted at "/" under Litestar via ``@asgi(is_mount=True)``; specific Litestar
    handlers win over the mount, so migrating a route is just removing the
    blueprint registration and adding the new Litestar handler.

    The legacy ``@login_required`` decorator on these blueprints (see
    ``web/auth/quart_compat.py``) defers to ``verify_owner_auth``, which
    authenticates the request on demand against the connection's cookies /
    Bearer header — no shared state from an outer middleware required.
    """
    # Imports are scoped to this builder because each module side-effectfully
    # constructs a Quart Blueprint at import time and we don't want those to
    # exist if a caller (e.g. the setup-only app) builds Litestar alone.
    from compute_space.web.routes.api.apps import api_apps_bp  # noqa: PLC0415
    from compute_space.web.routes.docs import docs_bp  # noqa: PLC0415

    web_dir = Path(__file__).parent
    quart_app = Quart(
        __name__,
        template_folder=str(web_dir / "templates"),
        static_folder=str(static_dir),
    )
    quart_app.openhost_config = config  # type: ignore[attr-defined]
    quart_app.secret_key = os.environ.get("SECRET_KEY", os.urandom(24))
    # App installs and data migrations can ship large request bodies.
    quart_app.config["MAX_CONTENT_LENGTH"] = None

    # Stubs for routes that have been migrated to Litestar.  Quart's
    # ``url_for`` resolves against blueprint-registered endpoints, and several
    # of the layout/dashboard templates reference migrated routes by name
    # (``pages_settings.settings_page`` -> /settings, etc.).  The handlers
    # are never actually invoked because the corresponding specific Litestar
    # routes win over the catch-all mount; they exist only so ``url_for``
    # produces the right URL.

    pages_settings_stub_bp = Blueprint("pages_settings", __name__)

    @pages_settings_stub_bp.route("/settings")
    async def settings_page() -> str:  # pragma: no cover — Litestar handles
        return ""

    # Stubs for endpoints whose handler moved to Litestar but are still
    # referenced from unmigrated Quart routes via ``url_for(...)`` (e.g.
    # /api/clone_and_get_app_info builds a redirect to ``apps.add_app``).
    # Same rationale as ``pages_settings_stub_bp`` above.
    apps_stub_bp = Blueprint("apps", __name__)

    @apps_stub_bp.route("/add_app")
    async def add_app() -> str:  # pragma: no cover — Litestar handles
        return ""

    @apps_stub_bp.route("/app_detail/<app_id>")
    async def app_detail(app_id: str) -> str:  # pragma: no cover — Litestar handles
        return ""

    quart_app.register_blueprint(pages_settings_stub_bp)
    quart_app.register_blueprint(apps_stub_bp)
    quart_app.register_blueprint(api_apps_bp)
    quart_app.register_blueprint(docs_bp)

    # Quart-side templating helpers, matching the Litestar Jinja globals so the
    # shared layout.html renders the same way regardless of which framework
    # handled the request.  static_url uses Quart's own ``url_for`` so the
    # blueprint-registered ``/static`` endpoint resolves correctly.
    @quart_app.context_processor
    async def _inject_template_globals() -> dict[str, Any]:
        zone_domain = config.zone_domain
        zone_name = zone_domain.split(".")[0] if zone_domain else None

        def app_url(app_name: str) -> str:
            proto = "https" if config.tls_enabled else "http"
            return f"{proto}://{app_name}.{zone_domain}/"

        def static_url(filename: str) -> str:
            base = quart_url_for("static", filename=filename)
            try:
                static_root = Path(current_app.static_folder or "")
                mtime = int((static_root / filename).stat().st_mtime)
            except OSError:
                return base
            sep = "&" if "?" in base else "?"
            return f"{base}{sep}v={mtime}"

        return {
            "zone_name": zone_name,
            "zone_domain": zone_domain,
            "app_url": app_url,
            "static_url": static_url,
        }

    return quart_app


def create_app(config: Config) -> ASGIApp:
    """Build the full router ASGI app.  The returned app is the Litestar app wrapped
    in ``SubdomainProxyMiddleware`` so app-subdomain requests are diverted to backend
    containers before Litestar attempts any routing.  Caller must have already
    initialized DB, keys, logging, and config."""
    _full_app_bootstrap(config)

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

    quart_fallback_app = _build_quart_fallback(config, static_dir)

    atexit.register(cleanup_terminal)

    litestar_app = Litestar(
        route_handlers=[
            static_router,
            api_archive_backend_routes,
            api_permissions_v2_routes,
            api_services_v2_routes,
            api_settings_routes,
            system_routes,
            identity_routes,
            pages_apps_routes,
            pages_login_routes,
            pages_permissions_v2_routes,
            pages_settings_routes,
            pages_system_routes,
            services_v2_routes,
            setup_already_done,
        ],
        template_config=template_config,
        before_request=_reject_app_subdomain_requests,
        dependencies={
            "config": Provide(provide_config, sync_to_thread=False),
            "db": Provide(provide_db),
        },
        exception_handlers={
            NotAuthorizedException: _login_required_redirect,
            Exception: _log_unhandled_exception,
        },
        on_startup=[_install_template_globals],
    )
    return SubdomainProxyMiddleware(_wrap_with_quart_fallback(litestar_app, quart_fallback_app))


def _wrap_with_quart_fallback(litestar_app: ASGIApp, quart_app: Quart) -> ASGIApp:
    """Wrap a Litestar ASGI app so that requests it doesn't match (404) are
    re-served by the Quart fallback ``quart_app``.

    Why not just register the Quart sub-app via ``@asgi(path="/", is_mount=True)``?
    Litestar 2.x routes path-parameterised handlers (``/app_detail/{app_id}``,
    ``/api/tokens/{token_id:int}``) at lower precedence than a mount at "/", so
    the mount silently shadows them — the specific Litestar route never runs and
    every such request 404s out of Quart instead.  Wrapping Litestar in an outer
    middleware sidesteps the routing precedence rule: Litestar runs first, the
    mount-style fallback only kicks in when Litestar genuinely had no match.
    """

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        # Buffering only makes sense for HTTP; pass websocket lifespan etc. straight through.
        if scope["type"] != "http":  # type: ignore[comparison-overlap]
            await litestar_app(scope, receive, send)
            return

        buffered: list[Message] = []
        status_code = 0

        async def buffer_send(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = int(message["status"])
            buffered.append(message)

        await litestar_app(scope, receive, buffer_send)

        if status_code == 404:
            # Litestar consumes the request body only when a handler runs; on
            # a routing 404 the body is still available for Quart to re-read.
            await quart_app(scope, receive, send)  # type: ignore[arg-type]
            return
        for message in buffered:
            await send(message)

    return app
