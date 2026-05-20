import json
import sqlite3
from typing import Any

from litestar import Request
from litestar import Router
from litestar import get
from litestar.exceptions import HTTPException
from litestar.response import Template

from compute_space.core.auth.permissions_v2 import get_granted_permissions_v2
from compute_space.web.auth.auth import require_owner_auth


@get("/approve-permissions-v2", guards=[require_owner_auth])
async def approve_permissions_v2(request: Request[Any, Any, Any], db: sqlite3.Connection) -> Template:
    """Owner-facing page: grant or deny a V2 permission request."""
    app_id = request.query_params.get("app")
    service_url = request.query_params.get("service")
    grant_json = request.query_params.get("grant")
    if not app_id or not service_url or not grant_json:
        raise HTTPException(detail="app, service, and grant query params are required", status_code=400)

    row = db.execute("SELECT name FROM apps WHERE app_id = ?", (app_id,)).fetchone()
    if not row:
        raise HTTPException(detail="App not found", status_code=404)
    app_name = row["name"]

    try:
        grant = json.loads(grant_json)
    except json.JSONDecodeError as e:
        raise HTTPException(detail="Invalid grant JSON", status_code=400) from e

    existing = get_granted_permissions_v2(app_id, service_url)
    already_granted = any(g.grant == grant and g.scope == "global" for g in existing)

    return Template(
        template_name="approve_permissions_v2.html",
        context={
            "app_id": app_id,
            "app_name": app_name,
            "service_url": service_url,
            "grant": grant,
            "grant_json": grant_json,
            "already_granted": already_granted,
            "redirect_url": request.query_params.get("return_to"),
        },
    )


pages_permissions_v2_routes = Router(path="/", route_handlers=[approve_permissions_v2])
