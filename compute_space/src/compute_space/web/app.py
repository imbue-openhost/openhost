import atexit
import os
from pathlib import Path
from typing import Any

from quart import Quart
from quart import current_app
from quart import redirect
from quart import request
from quart import url_for
from quart.typing import ResponseReturnValue

from compute_space.config import Config
from compute_space.config import load_config
from compute_space.core import auth
from compute_space.core.logging import setup_file_logging
from compute_space.core.startup import init_app
from compute_space.core.terminal import cleanup_all as cleanup_terminal
from compute_space.db import close_db
from compute_space.db import get_db

# ─── App Factory ───


def create_app(config: Config | None = None) -> Quart:
    if config is None:
        config = load_config()

    app = Quart(__name__)
    app.openhost_config = config  # type: ignore[attr-defined]
    app.secret_key = os.environ.get("SECRET_KEY", os.urandom(24))
    app.config["DB_PATH"] = config.db_path
    # Allow large request bodies for migration and data transfers
    app.config["MAX_CONTENT_LENGTH"] = None

    setup_file_logging(Path(os.path.dirname(config.db_path)) / "compute_space.log")

    # Load auth keys
    auth.load_keys(config.keys_dir)

    # Register teardown
    app.teardown_appcontext(close_db)

    # Register blueprints (imported here per Flask/Quart convention - blueprints have globals/side effects)
    from compute_space.web.auth.api_system import api_system_bp  # noqa: PLC0415
    from compute_space.web.auth.identity_routes import identity_bp  # noqa: PLC0415
    from compute_space.web.auth.pages import auth_bp  # noqa: PLC0415
    from compute_space.web.routes.api.apps import api_apps_bp  # noqa: PLC0415
    from compute_space.web.routes.api.archive_backend import api_archive_backend_bp  # noqa: PLC0415
    from compute_space.web.routes.api.permissions import api_permissions_bp  # noqa: PLC0415
    from compute_space.web.routes.api.permissions_v2 import api_permissions_v2_bp  # noqa: PLC0415
    from compute_space.web.routes.api.services import api_services_bp  # noqa: PLC0415
    from compute_space.web.routes.api.services_v2 import api_services_v2_bp  # noqa: PLC0415
    from compute_space.web.routes.api.settings import api_settings_bp  # noqa: PLC0415
    from compute_space.web.routes.docs import docs_bp  # noqa: PLC0415
    from compute_space.web.routes.pages.apps import apps_bp  # noqa: PLC0415
    from compute_space.web.routes.pages.permissions import pages_permissions_bp  # noqa: PLC0415
    from compute_space.web.routes.pages.permissions_v2 import pages_permissions_v2_bp  # noqa: PLC0415
    from compute_space.web.routes.pages.settings import pages_settings_bp  # noqa: PLC0415
    from compute_space.web.routes.pages.system import pages_system_bp  # noqa: PLC0415
    from compute_space.web.routes.proxy import proxy_bp  # noqa: PLC0415
    from compute_space.web.routes.services import services_bp  # noqa: PLC0415
    from compute_space.web.routes.services_v2 import services_v2_bp  # noqa: PLC0415

    app.register_blueprint(auth_bp)
    app.register_blueprint(apps_bp)
    app.register_blueprint(pages_settings_bp)
    app.register_blueprint(pages_system_bp)
    app.register_blueprint(pages_permissions_bp)
    app.register_blueprint(api_apps_bp)
    app.register_blueprint(api_archive_backend_bp)
    app.register_blueprint(api_settings_bp)
    app.register_blueprint(api_system_bp)
    app.register_blueprint(api_services_bp)
    app.register_blueprint(api_permissions_bp)
    app.register_blueprint(services_bp)
    app.register_blueprint(services_v2_bp)
    app.register_blueprint(api_services_v2_bp)
    app.register_blueprint(api_permissions_v2_bp)
    app.register_blueprint(pages_permissions_v2_bp)
    app.register_blueprint(identity_bp)
    # Register docs BEFORE the catch-all proxy so /docs/... is
    # always handled by the docs blueprint regardless of whether
    # a user happens to have deployed an app named "docs".
    app.register_blueprint(docs_bp)
    app.register_blueprint(
        proxy_bp
    )  # last — has catch-all; registers before_app_request / before_app_websocket subdomain hooks

    # Initialize DB and app state
    init_app(app)

    # ─── Before-request hooks ───

    @app.before_request
    async def _require_owner() -> ResponseReturnValue | None:
        """Redirect to setup if no owner has been created yet."""
        if getattr(app, "_owner_verified", False):
            return None
        if request.path in ("/setup", "/health"):
            return None
        # Docs ("/docs/" landing + "/docs/<anything>") must be
        # readable even before the zone owner has been provisioned
        # — the docs are usually what an operator consults BEFORE
        # finishing setup.  The docs blueprint itself is public
        # (no @login_required), but the before-request hook above
        # would otherwise redirect every pre-setup request to
        # /setup.  Whitelist /docs explicitly here.
        if request.path == "/docs" or request.path.startswith("/docs/"):
            return None
        db = get_db()
        owner = db.execute("SELECT 1 FROM owner LIMIT 1").fetchone()
        if owner is not None:
            app._owner_verified = True  # type: ignore[attr-defined]
            return None
        claim = request.args.get("claim", "")
        return redirect(url_for("auth.setup", claim=claim) if claim else url_for("auth.setup"))

    # ─── Context processor ───

    @app.context_processor
    async def _inject_zone_name() -> dict[str, Any]:
        """Make zone_name and app_url helper available in all templates."""
        zone_domain: str | None = config.zone_domain
        if zone_domain:
            zone_name: str | None = zone_domain.split(".")[0]
        else:
            ext_host: str = request.headers.get("X-Forwarded-Host", request.host).split(":")[0]
            parts: list[str] = ext_host.split(".")
            zone_name = parts[0] if len(parts) > 2 else None

        proto: str = request.headers.get("X-Forwarded-Proto", request.scheme)
        request_host: str = request.headers.get("X-Forwarded-Host", request.host)
        base_host: str = zone_domain or request_host
        if zone_domain and ":" not in zone_domain and ":" in request_host:
            _, _, port = request_host.partition(":")
            if port:
                base_host = f"{zone_domain}:{port}"

        def app_url(app_name: str) -> str:
            return f"{proto}://{app_name}.{base_host}/"

        def static_url(filename: str) -> str:
            """Like ``url_for('static', ...)`` but cache-busted by file mtime.

            Browsers aggressively cache static JS/CSS, so a deploy that ships a
            new template + JS would otherwise leave returning visitors running
            stale JS against new HTML (silently no-op'ing on missing element
            ids).  Appending ``?v=<mtime>`` forces a fresh fetch whenever the
            file changes.
            """
            base = url_for("static", filename=filename)
            try:
                static_root = Path(current_app.static_folder or "")
                mtime = int((static_root / filename).stat().st_mtime)
            except OSError:
                return base
            sep = "&" if "?" in base else "?"
            return f"{base}{sep}v={mtime}"

        return {
            "zone_name": zone_name,
            "zone_domain": base_host,
            "app_url": app_url,
            "static_url": static_url,
        }

    # Terminal cleanup
    atexit.register(cleanup_terminal)

    return app


if __name__ == "__main__":
    app = create_app()
    cfg = app.openhost_config  # type: ignore[attr-defined]
    app.run(host=cfg.host, port=cfg.port)
