import atexit
import sqlite3
from pathlib import Path
from typing import Any

from litestar import Litestar
from litestar import Response
from litestar.contrib.jinja import JinjaTemplateEngine
from litestar.di import Provide
from litestar.exceptions import NotAuthorizedException
from litestar.openapi.config import OpenAPIConfig
from litestar.static_files import create_static_files_router
from litestar.template.config import TemplateConfig

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
from compute_space.web.auth.middleware import login_required_redirect
from compute_space.web.auth.middleware import provide_accessor
from compute_space.web.middleware.auth_accessor import AuthAccessorMiddleware
from compute_space.web.middleware.auth_refresh import AuthRefreshMiddleware
from compute_space.web.middleware.subdomain_proxy import SubdomainProxyMiddleware
from compute_space.web.routes.api.settings import api_settings_routes
from compute_space.web.routes.pages.login import pages_login_routes
from compute_space.web.routes.pages.settings import pages_settings_routes


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

    atexit.register(cleanup_terminal)

    return Litestar(
        route_handlers=[
            static_router,
            api_settings_routes,
            pages_login_routes,
            pages_settings_routes,
        ],
        middleware=[SubdomainProxyMiddleware, AuthRefreshMiddleware, AuthAccessorMiddleware],
        template_config=template_config,
        after_request=_close_db_after,
        dependencies={
            "config": Provide(provide_config, sync_to_thread=False),
            "db": Provide(provide_db, sync_to_thread=False),
            "accessor": Provide(provide_accessor),
        },
        exception_handlers={NotAuthorizedException: login_required_redirect},
        on_startup=[_install_template_globals],
        openapi_config=OpenAPIConfig(title="compute_space", version="0.1.0", path="/openapi"),
    )
