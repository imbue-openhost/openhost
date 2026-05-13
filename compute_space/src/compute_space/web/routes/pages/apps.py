import urllib.parse
from typing import Any

from litestar import Request
from litestar import Response
from litestar import get
from litestar.response import Redirect
from litestar.response import Template

from compute_space.config import get_config
from compute_space.core.app_id import is_valid_app_id
from compute_space.core.apps import list_builtin_apps
from compute_space.core.containers import get_docker_logs
from compute_space.core.manifest import parse_manifest_from_string
from compute_space.db import get_db


@get("/", sync_to_thread=False)
def dashboard_root(user: dict[str, Any]) -> Template:
    db = get_db()
    apps_list = db.execute("SELECT * FROM apps ORDER BY name").fetchall()
    return Template(template_name="dashboard.html", context={"apps": apps_list})


@get("/dashboard", sync_to_thread=False)
def dashboard(user: dict[str, Any]) -> Template:
    db = get_db()
    apps_list = db.execute("SELECT * FROM apps ORDER BY name").fetchall()
    return Template(template_name="dashboard.html", context={"apps": apps_list})


@get("/app_detail/{app_id:str}", sync_to_thread=False)
def app_detail(app_id: str, user: dict[str, Any], next: str = "") -> Response[Any] | Template:
    if not is_valid_app_id(app_id):
        return Response(content="Invalid app_id", status_code=400, media_type="text/plain")
    config = get_config()
    db = get_db()
    app_row = db.execute("SELECT * FROM apps WHERE app_id = ?", (app_id,)).fetchone()
    if not app_row:
        return Response(content="App not found", status_code=404, media_type="text/plain")
    app_name = app_row["name"]
    databases = db.execute("SELECT * FROM app_databases WHERE app_id = ?", (app_id,)).fetchall()
    port_mappings = db.execute(
        "SELECT label, container_port, host_port FROM app_port_mappings WHERE app_id = ? ORDER BY label",
        (app_id,),
    ).fetchall()
    permissions = db.execute(
        "SELECT consumer_app_id, permission_key FROM permissions WHERE consumer_app_id = ? ORDER BY permission_key",
        (app_id,),
    ).fetchall()
    logs = get_docker_logs(app_name, config.temporary_data_dir, app_row["container_id"])

    granted_keys = {row["permission_key"] for row in permissions}
    missing_permissions: list[dict[str, Any]] = []
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
            pass

    return Template(
        template_name="app_detail.html",
        context={
            "app": app_row,
            "databases": databases,
            "port_mappings": port_mappings,
            "permissions": permissions,
            "missing_permissions": missing_permissions,
            "logs": logs,
            "next_url": next,
        },
    )


@get("/handle_invite", sync_to_thread=False)
def handle_invite(request: Request[Any, Any, Any], user: dict[str, Any]) -> Redirect:
    app_name = request.query_params.get("app", "")
    repo_url = request.query_params.get("repo", "")

    invite_params = {k: v for k, v in request.query_params.items() if k not in ("app", "repo")}
    invite_qs = urllib.parse.urlencode(invite_params)

    db = get_db()
    app_row = db.execute("SELECT app_id, name, status FROM apps WHERE name = ?", (app_name,)).fetchone()

    if app_row:
        if app_row["status"] == "running":
            ext_host = request.headers.get("X-Forwarded-Host", request.headers.get("host", ""))
            proto = request.headers.get("X-Forwarded-Proto", request.scope.get("scheme", "http"))
            return Redirect(path=f"{proto}://{app_name}.{ext_host}/handle_invite?{invite_qs}")
        return Redirect(
            path=f"/app_detail/{app_row['app_id']}?next=/handle_invite?"
            + urllib.parse.urlencode(dict(request.query_params))
        )

    next_url = "/handle_invite?" + urllib.parse.urlencode(dict(request.query_params))
    return Redirect(path=f"/add_app?repo={repo_url}&next={urllib.parse.quote(next_url)}")


@get("/add_app", sync_to_thread=False)
def add_app(user: dict[str, Any], repo: str = "", next: str = "") -> Template:
    config = get_config()
    return Template(
        template_name="add_app.html",
        context={
            "builtin_apps": list_builtin_apps(config),
            "initial_repo": repo,
            "next_url": next,
        },
    )


pages_apps_routes = [dashboard_root, dashboard, app_detail, handle_invite, add_app]
