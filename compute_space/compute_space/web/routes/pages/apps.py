import urllib.parse

from quart import Blueprint
from quart import redirect
from quart import render_template
from quart import request
from quart import url_for
from quart.typing import ResponseReturnValue

from compute_space.config import get_config
from compute_space.core.apps import list_builtin_apps
from compute_space.core.containers import get_docker_logs
from compute_space.core.manifest import parse_manifest_from_string
from compute_space.db import get_db
from compute_space.web.middleware import login_required

apps_bp = Blueprint("apps", __name__)


# ─── Dashboard ───


@apps_bp.route("/")
@apps_bp.route("/dashboard")
@login_required
async def dashboard() -> str:
    db = get_db()
    apps_list = db.execute("SELECT * FROM apps ORDER BY name").fetchall()
    return await render_template("dashboard.html", apps=apps_list)


@apps_bp.route("/app_detail/<app_name>")
@login_required
async def app_detail(app_name: str) -> str | tuple[str, int]:
    config = get_config()
    db = get_db()
    app_row = db.execute("SELECT * FROM apps WHERE name = ?", (app_name,)).fetchone()
    if not app_row:
        return "App not found", 404
    databases = db.execute("SELECT * FROM app_databases WHERE app_name = ?", (app_name,)).fetchall()
    port_mappings = db.execute(
        "SELECT label, container_port, host_port FROM app_port_mappings WHERE app_name = ? ORDER BY label",
        (app_name,),
    ).fetchall()
    permissions = db.execute(
        "SELECT consumer_app, permission_key FROM permissions WHERE consumer_app = ? ORDER BY permission_key",
        (app_name,),
    ).fetchall()
    logs = get_docker_logs(app_name, config.temporary_data_dir, app_row["container_id"])
    next_url = request.args.get("next", "")

    # Compute permissions the manifest declares but that haven't been granted yet,
    # so the owner can grant them retroactively (e.g. after installing the secrets app).
    granted_keys = {row["permission_key"] for row in permissions}
    missing_permissions: list[dict[str, str]] = []
    if app_row["manifest_raw"]:
        try:
            manifest = parse_manifest_from_string(app_row["manifest_raw"])
            for svc_name, keys in manifest.requires_services.items():
                for key_spec in keys:
                    perm_key = f"{svc_name}/{key_spec['key']}"
                    if perm_key not in granted_keys:
                        missing_permissions.append(
                            {
                                "permission_key": perm_key,
                                "reason": key_spec.get("reason", ""),
                                "required": key_spec.get("required", True),
                            }
                        )
        except Exception:
            pass  # Don't break the page if the manifest is malformed

    return await render_template(
        "app_detail.html",
        app=app_row,
        databases=databases,
        port_mappings=port_mappings,
        permissions=permissions,
        missing_permissions=missing_permissions,
        logs=logs,
        next_url=next_url,
    )


# ─── Add App ───


@apps_bp.route("/handle_invite")
@login_required
def handle_invite() -> ResponseReturnValue:
    """Route invite links: if the app is installed, redirect to it; otherwise install first."""
    app_name = request.args.get("app", "")
    repo_url = request.args.get("repo", "")

    invite_params = {k: v for k, v in request.args.items() if k not in ("app", "repo")}
    invite_qs = urllib.parse.urlencode(invite_params)

    db = get_db()
    app_row = db.execute("SELECT name, status FROM apps WHERE name = ?", (app_name,)).fetchone()

    if app_row:
        if app_row["status"] == "running":
            ext_host = request.headers.get("X-Forwarded-Host", request.host)
            proto = request.headers.get("X-Forwarded-Proto", request.scheme)
            return redirect(f"{proto}://{app_name}.{ext_host}/handle_invite?{invite_qs}")
        return redirect(
            url_for(
                "apps.app_detail",
                app_name=app_name,
                next="/handle_invite?" + urllib.parse.urlencode(request.args),
            )
        )

    next_url = "/handle_invite?" + urllib.parse.urlencode(request.args)
    return redirect(url_for("apps.add_app", repo=repo_url, next=next_url))


@apps_bp.route("/add_app")
@login_required
async def add_app() -> ResponseReturnValue:
    config = get_config()
    # initial_repo/next_url: passed from query params so JS can auto-start
    # cloning when landing here via invite link (/add_app?repo=...&next=...)
    return await render_template(
        "add_app.html",
        builtin_apps=list_builtin_apps(config),
        initial_repo=request.args.get("repo", ""),
        next_url=request.args.get("next", ""),
    )
