import atexit
import os
import sqlite3
from pathlib import Path
from typing import Any
from typing import cast

from litestar import HttpMethod
from litestar import Litestar
from litestar import MediaType
from litestar import Request
from litestar import Response
from litestar import route
from litestar.contrib.jinja import JinjaTemplateEngine
from litestar.di import Provide
from litestar.exceptions import NotAuthorizedException
from litestar.handlers import asgi
from litestar.response import Redirect
from litestar.static_files import create_static_files_router
from litestar.template.config import TemplateConfig
from litestar.types import Receive
from litestar.types import Scope
from litestar.types import Send
from quart import Blueprint
from quart import Quart
from quart import current_app
from quart import url_for as quart_url_for

from compute_space.config import Config
from compute_space.config import provide_config
from compute_space.core import archive_backend
from compute_space.core.auth.identity import load_identity_keys
from compute_space.core.startup import check_app_status
from compute_space.core.startup import retry_pending_default_apps
from compute_space.core.storage import start_storage_guard
from compute_space.core.terminal import cleanup_all as cleanup_terminal
from compute_space.db import close_db
from compute_space.db import provide_db
from compute_space.web.auth.auth import AuthMiddleware
from compute_space.web.middleware.subdomain_proxy import SubdomainProxyMiddleware
from compute_space.web.routes.api.permissions_v2 import api_permissions_v2_routes
from compute_space.web.routes.api.settings import api_settings_routes
from compute_space.web.routes.pages.login import pages_login_routes
from compute_space.web.routes.pages.settings import pages_settings_routes
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


async def _close_db_after(response: Response[Any]) -> Response[Any]:
    close_db()
    return response


@route("/setup", http_method=[HttpMethod.GET, HttpMethod.POST], status_code=403, sync_to_thread=False)
def setup_already_done() -> Response[str]:
    return Response(
        content="This instance has already been set up.",
        status_code=403,
        media_type=MediaType.TEXT,
    )


def _login_required_redirect(request: Request[Any, Any, Any], exc: NotAuthorizedException) -> Response[Any]:
    """Exception handler: redirect HTML clients to /setup or /login; JSON clients get 401.

    websocket-type requests should never get here - they start as HTTP requests with `Upgrade: websocket`, and should fail then.
    """
    if "application/json" in request.headers.get("Accept", ""):
        return Response(content={"error": exc.detail}, status_code=401)

    return Redirect(path="/login")


def _build_quart_fallback(config: Config, static_dir: Path) -> Quart:
    """Build a Quart app holding the blueprints not yet ported to Litestar.

    Mounted at "/" under Litestar via ``@asgi(is_mount=True)``; specific Litestar
    handlers win over the mount, so migrating a route is just removing the
    blueprint registration and adding the new Litestar handler.

    The outer ``AuthMiddleware`` has already populated ``scope["state"]`` with
    the accessor + origin by the time requests reach these blueprints, so the
    legacy ``@login_required`` / ``@app_auth_required`` decorators (see
    ``web/auth/middleware.py``) just read that state instead of doing any
    JWT/cookie work themselves.
    """
    # Imports are scoped to this builder because each module side-effectfully
    # constructs a Quart Blueprint at import time and we don't want those to
    # exist if a caller (e.g. the setup-only app) builds Litestar alone.
    from compute_space.web.routes.api.apps import api_apps_bp  # noqa: PLC0415
    from compute_space.web.routes.api.archive_backend import api_archive_backend_bp  # noqa: PLC0415
    from compute_space.web.routes.api.identity import identity_bp  # noqa: PLC0415
    from compute_space.web.routes.api.services_v2 import api_services_v2_bp  # noqa: PLC0415
    from compute_space.web.routes.api.system import system_bp  # noqa: PLC0415
    from compute_space.web.routes.docs import docs_bp  # noqa: PLC0415
    from compute_space.web.routes.pages.apps import apps_bp  # noqa: PLC0415
    from compute_space.web.routes.pages.permissions_v2 import pages_permissions_v2_bp  # noqa: PLC0415
    from compute_space.web.routes.pages.system import pages_system_bp  # noqa: PLC0415

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
    quart_app.teardown_appcontext(close_db)

    # Stubs for routes that have been migrated to Litestar.  Quart's
    # ``url_for`` resolves against blueprint-registered endpoints, and several
    # of the layout/dashboard templates reference migrated routes by name
    # (``pages_settings.settings_page`` -> /settings, etc.).  The handlers
    # are never actually invoked because the corresponding specific Litestar
    # routes win over the catch-all mount; they exist only so ``url_for``
    # produces the right URL.

    migrated_stub_bp = Blueprint("pages_settings", __name__)

    @migrated_stub_bp.route("/settings")
    async def settings_page() -> str:  # pragma: no cover — Litestar handles
        return ""

    quart_app.register_blueprint(migrated_stub_bp)
    quart_app.register_blueprint(apps_bp)
    quart_app.register_blueprint(pages_system_bp)
    quart_app.register_blueprint(pages_permissions_v2_bp)
    quart_app.register_blueprint(api_apps_bp)
    quart_app.register_blueprint(api_archive_backend_bp)
    quart_app.register_blueprint(system_bp)
    quart_app.register_blueprint(api_services_v2_bp)
    quart_app.register_blueprint(identity_bp)
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


def create_app(config: Config) -> Litestar:
    """Build the full Litestar app. Caller must have already initialized DB, keys, logging, and config."""
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

    @asgi(path="/", is_mount=True)
    async def quart_fallback(scope: Scope, receive: Receive, send: Send) -> None:
        """Catch-all for anything Litestar didn't match — defers to the Quart sub-app.

        Litestar rewrites ``scope["path"]`` when delegating to a mount (strips
        the mount prefix, normalises trailing slashes); reconstitute it from
        ``raw_path`` so Quart's URL matcher sees the original request path
        (e.g. "/health", not "health/").
        """
        raw_path = scope.get("raw_path")
        if raw_path is not None:
            patched = dict(scope)
            patched["path"] = raw_path.decode("ascii")
            scope = cast(Scope, patched)
        # Quart is typed against hypercorn's ASGI aliases while Litestar uses
        # its own; the runtime objects are interchangeable.
        await quart_fallback_app(scope, receive, send)  # type: ignore[arg-type]

    atexit.register(cleanup_terminal)

    return Litestar(
        route_handlers=[
            static_router,
            api_permissions_v2_routes,
            api_settings_routes,
            pages_login_routes,
            pages_settings_routes,
            services_v2_routes,
            setup_already_done,
            quart_fallback,
        ],
        middleware=[AuthMiddleware, SubdomainProxyMiddleware],
        template_config=template_config,
        after_request=_close_db_after,
        dependencies={
            "config": Provide(provide_config, sync_to_thread=False),
            "db": Provide(provide_db, sync_to_thread=False),
        },
        exception_handlers={NotAuthorizedException: _login_required_redirect},
        on_startup=[_install_template_globals],
    )
