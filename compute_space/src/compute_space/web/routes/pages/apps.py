import sqlite3

from litestar import Router
from litestar import get
from litestar.exceptions import HTTPException
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
async def app_detail(app_id: str, db: sqlite3.Connection, config: Config, next: str = "") -> Template:
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

    return Template(
        template_name="app_detail.html",
        context={
            "app": app_row,
            "databases": databases,
            "port_mappings": port_mappings,
            "logs": logs,
            "next_url": next,
        },
    )


@get("/add_app", guards=[require_owner_auth])
async def add_app(config: Config, repo: str = "", next: str = "") -> Template:
    return Template(
        template_name="add_app.html",
        context={
            "builtin_apps": list_builtin_apps(config),
            "initial_repo": repo,
            "next_url": next,
        },
    )


pages_apps_routes = Router(
    path="/",
    route_handlers=[dashboard, app_detail, add_app],
)
