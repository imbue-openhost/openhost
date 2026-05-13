import atexit
import os
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
from compute_space.core import auth
from compute_space.core.logging import setup_file_logging
from compute_space.core.startup import init_app
from compute_space.core.terminal import cleanup_all as cleanup_terminal
from compute_space.db import close_db
from compute_space.db import get_db
from compute_space.db import init_db
from compute_space.web.auth.api_system import api_system_routes
from compute_space.web.auth.identity_routes import identity_routes
from compute_space.web.auth.middleware import login_required_redirect
from compute_space.web.auth.middleware import provide_app_id
from compute_space.web.auth.middleware import provide_user
from compute_space.web.auth.pages import auth_pages_routes
from compute_space.web.middleware.auth_refresh import AuthRefreshMiddleware
from compute_space.web.middleware.subdomain_proxy import SubdomainProxyMiddleware
from compute_space.web.routes.api.apps import api_apps_routes
from compute_space.web.routes.api.archive_backend import api_archive_backend_routes
from compute_space.web.routes.api.permissions import api_permissions_routes
from compute_space.web.routes.api.permissions_v2 import api_permissions_v2_routes
from compute_space.web.routes.api.services import api_services_routes
from compute_space.web.routes.api.services_v2 import api_services_v2_routes
from compute_space.web.routes.api.settings import api_settings_routes
from compute_space.web.routes.pages.apps import pages_apps_routes
from compute_space.web.routes.pages.permissions import pages_permissions_routes
from compute_space.web.routes.pages.permissions_v2 import pages_permissions_v2_routes
from compute_space.web.routes.pages.settings import pages_settings_routes
from compute_space.web.routes.pages.system import pages_system_routes
from compute_space.web.routes.services import services_routes
from compute_space.web.routes.services_v2 import services_v2_routes


def _public_paths() -> set[str]:
    return {"/setup", "/health", "/.well-known/jwks.json", "/.well-known/openhost-identity"}


async def _require_owner(request: Request[Any, Any, Any]) -> Response[Any] | None:
    """Redirect to /setup if no owner exists yet, except for the public bootstrap paths."""
    state: State = request.app.state
    if getattr(state, "owner_verified", False):
        return None
    if request.url.path in _public_paths():
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


_ROUTE_NAME_TO_PATH: dict[str, str] = {
    "auth.setup": "/setup",
    "auth.login": "/login",
    "auth.logout": "/logout",
    "apps.dashboard": "/dashboard",
    "apps.add_app": "/add_app",
    "apps.app_detail": "/app_detail/{app_id}",
    "api_apps.api_apps": "/api/apps",
    "api_apps.app_status": "/api/app_status/{app_id}",
    "api_apps.app_logs": "/app_logs/{app_id}",
    "api_apps.stop_app": "/stop_app/{app_id}",
    "api_apps.reload_app": "/reload_app/{app_id}",
    "api_apps.remove_app": "/remove_app/{app_id}",
    "api_apps.rename_app": "/rename_app/{app_id}",
    "api_system.api_tokens_list": "/api/tokens",
    "api_system.api_tokens_create": "/api/tokens",
    "api_system.security_audit": "/api/security-audit",
    "api_system.listening_ports": "/api/listening-ports",
    "api_system.api_storage_status": "/api/storage-status",
    "api_system.restart_router": "/restart_router",
    "api_system.drop_docker_cache": "/api/drop-docker-cache",
    "api_system.ssh_status": "/api/ssh-status",
    "api_system.toggle_ssh": "/toggle-ssh",
    "api_system.toggle_storage_guard": "/api/storage-guard",
    "api_system.compute_space_logs": "/api/compute_space_logs",
    "api_archive_backend.get_archive_backend": "/api/storage/archive_backend",
    "api_archive_backend.test_connection": "/api/storage/archive_backend/test_connection",
    "api_archive_backend.configure_archive_backend": "/api/storage/archive_backend/configure",
    "pages_system.system_page": "/system/",
    "pages_system.logs_page": "/logs/",
    "pages_system.terminal_page": "/terminal/",
    "pages_settings.settings_page": "/settings",
    "pages_permissions.approve_permissions": "/approve-permissions",
    "pages_permissions_v2.approve_permissions_v2": "/approve-permissions-v2",
}


def _url_for(endpoint: str, **kwargs: Any) -> str:
    """Jinja shim: translate the old Quart blueprint names used in templates into Litestar paths."""
    if endpoint == "static":
        filename = kwargs.get("filename", "")
        return f"/static/{filename}"
    template = _ROUTE_NAME_TO_PATH.get(endpoint)
    if template is None:
        raise KeyError(f"Unknown route name: {endpoint}")
    path = template
    for key, value in kwargs.items():
        path = path.replace("{" + key + "}", str(value))
    return path


def _set_template_globals(app: Litestar) -> None:
    cfg: Config = app.state.config
    zone_domain = cfg.zone_domain
    zone_name = zone_domain.split(".")[0] if zone_domain else None
    base_host = zone_domain or ""
    static_dir: Path = app.state.static_dir

    def app_url(app_name: str) -> str:
        return f"https://{app_name}.{base_host}/"

    def static_url(filename: str) -> str:
        base = f"/static/{filename.lstrip('/')}"
        try:
            mtime = int((static_dir / filename).stat().st_mtime)
        except OSError:
            return base
        sep = "&" if "?" in base else "?"
        return f"{base}{sep}v={mtime}"

    engine = app.template_engine.engine  # type: ignore[union-attr]
    engine.globals["zone_name"] = zone_name
    engine.globals["zone_domain"] = base_host
    engine.globals["app_url"] = app_url
    engine.globals["static_url"] = static_url
    engine.globals["url_for"] = _url_for


def _on_startup(app: Litestar) -> None:
    cfg: Config = app.state.config
    set_active_config(cfg)
    init_db(cfg.db_path)
    auth.load_keys(cfg.keys_dir)
    init_app(cfg)
    db = get_db()
    owner = db.execute("SELECT 1 FROM owner LIMIT 1").fetchone()
    app.state.owner_verified = owner is not None
    _set_template_globals(app)


def _build_route_handlers(static_dir: Path) -> list[Any]:
    handlers: list[Any] = []
    handlers.extend(api_system_routes)
    handlers.extend(identity_routes)
    handlers.extend(auth_pages_routes)
    handlers.extend(pages_apps_routes)
    handlers.extend(pages_settings_routes)
    handlers.extend(pages_system_routes)
    handlers.extend(pages_permissions_routes)
    handlers.extend(pages_permissions_v2_routes)
    handlers.extend(api_apps_routes)
    handlers.extend(api_archive_backend_routes)
    handlers.extend(api_settings_routes)
    handlers.extend(api_services_routes)
    handlers.extend(api_permissions_routes)
    handlers.extend(api_permissions_v2_routes)
    handlers.extend(api_services_v2_routes)
    handlers.extend(services_routes)
    handlers.extend(services_v2_routes)
    handlers.append(create_static_files_router(path="/static", directories=[static_dir]))
    return handlers


def create_app(config: Config | None = None) -> Litestar:
    if config is None:
        config = load_config()

    setup_file_logging(Path(os.path.dirname(config.db_path)) / "compute_space.log")

    base_dir = Path(__file__).parent
    template_dir = base_dir / "templates"
    static_dir = base_dir / "static"

    template_config = TemplateConfig(directory=template_dir, engine=JinjaTemplateEngine)

    state = State({"config": config, "owner_verified": False, "static_dir": static_dir})

    app = Litestar(
        route_handlers=_build_route_handlers(static_dir),
        middleware=[SubdomainProxyMiddleware, AuthRefreshMiddleware],
        template_config=template_config,
        exception_handlers={NotAuthorizedException: login_required_redirect},
        dependencies={
            "user": Provide(provide_user),
            "caller_app_id": Provide(provide_app_id),
        },
        before_request=_require_owner,
        after_request=_close_db_after,
        on_startup=[_on_startup],
        state=state,
        openapi_config=OpenAPIConfig(title="compute_space", version="0.1.0", path="/openapi"),
    )

    atexit.register(cleanup_terminal)
    return app


if __name__ == "__main__":
    app = create_app()
    cfg: Config = app.state.config
    import asyncio

    import hypercorn.asyncio
    import hypercorn.config

    cfg_h = hypercorn.config.Config()
    cfg_h.bind = [f"{cfg.host}:{cfg.port}"]
    asyncio.run(hypercorn.asyncio.serve(app, cfg_h))  # type: ignore[arg-type]
