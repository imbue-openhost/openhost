import sqlite3
import urllib.parse
from typing import Any

from litestar import Request
from litestar import Router
from litestar import get
from litestar.exceptions import HTTPException
from litestar.response import Redirect
from litestar.response import Template

from compute_space.config import Config
from compute_space.core.app_id import is_valid_app_id
from compute_space.core.apps import list_builtin_apps
from compute_space.core.containers import get_docker_logs
from compute_space.web.auth.auth import require_owner_auth


@get(["/", "/dashboard"], guards=[require_owner_auth])
async def dashboard(db: sqlite3.Connection) -> Template:
    apps_list = db.execute("SELECT * FROM apps ORDER BY name").fetchall()
    return Template(template_name="dashboard.html", context={"apps": apps_list})


@get("/app_detail/{app_id:str}", guards=[require_owner_auth])
async def app_detail(app_id: str, request: Request[Any, Any, Any], db: sqlite3.Connection, config: Config) -> Template:
    if not is_valid_app_id(app_id):
        raise HTTPException(detail="Invalid app_id", status_code=400)
    app_row = db.execute("SELECT * FROM apps WHERE app_id = ?", (app_id,)).fetchone()
    if not app_row:
        raise HTTPException(detail="App not found", status_code=404)
    app_name = app_row["name"]
    databases = db.execute("SELECT * FROM app_databases WHERE app_id = ?", (app_id,)).fetchall()
    port_mappings = db.execute(
        "SELECT label, container_port, host_port FROM app_port_mappings WHERE app_id = ? ORDER BY label",
        (app_id,),
    ).fetchall()
    logs = get_docker_logs(app_name, config.temporary_data_dir, app_row["container_id"])
    next_url = request.query_params.get("next", "")

    return Template(
        template_name="app_detail.html",
        context={
            "app": app_row,
            "databases": databases,
            "port_mappings": port_mappings,
            "logs": logs,
            "next_url": next_url,
        },
    )


@get("/handle_invite", guards=[require_owner_auth])
async def handle_invite(request: Request[Any, Any, Any], db: sqlite3.Connection) -> Redirect:
    """Route invite links: if the app is installed, redirect to it; otherwise install first."""
    params = dict(request.query_params)
    app_name = params.get("app", "")
    repo_url = params.get("repo", "")

    invite_params = {k: v for k, v in params.items() if k not in ("app", "repo")}
    invite_qs = urllib.parse.urlencode(invite_params)
    all_qs = urllib.parse.urlencode(params)

    app_row = db.execute("SELECT app_id, name, status FROM apps WHERE name = ?", (app_name,)).fetchone()

    if app_row:
        if app_row["status"] == "running":
            ext_host = request.headers.get("X-Forwarded-Host", request.url.netloc)
            proto = request.headers.get("X-Forwarded-Proto", request.url.scheme)
            return Redirect(path=f"{proto}://{app_name}.{ext_host}/handle_invite?{invite_qs}")
        next_url = "/handle_invite?" + all_qs
        return Redirect(path=f"/app_detail/{app_row['app_id']}?{urllib.parse.urlencode({'next': next_url})}")

    next_url = "/handle_invite?" + all_qs
    return Redirect(path="/add_app?" + urllib.parse.urlencode({"repo": repo_url, "next": next_url}))


@get("/add_app", guards=[require_owner_auth])
async def add_app(request: Request[Any, Any, Any], config: Config) -> Template:
    # initial_repo/next_url: passed from query params so JS can auto-start
    # cloning when landing here via invite link (/add_app?repo=...&next=...)
    return Template(
        template_name="add_app.html",
        context={
            "builtin_apps": list_builtin_apps(config),
            "initial_repo": request.query_params.get("repo", ""),
            "next_url": request.query_params.get("next", ""),
        },
    )


pages_apps_routes = Router(
    path="/",
    route_handlers=[dashboard, app_detail, handle_invite, add_app],
)
