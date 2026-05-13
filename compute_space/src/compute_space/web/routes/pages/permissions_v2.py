import json
from typing import Any

from litestar import Request
from litestar import Response
from litestar import get
from litestar.response import Template

from compute_space.core.auth.permissions_v2 import get_granted_permissions_v2
from compute_space.db import get_db


@get("/approve-permissions-v2", sync_to_thread=False)
def approve_permissions_v2(request: Request[Any, Any, Any], user: dict[str, Any]) -> Response[bytes] | Template:
    app_id = request.query_params.get("app")
    service_url = request.query_params.get("service")
    grant_json = request.query_params.get("grant")
    if not app_id or not service_url or not grant_json:
        return Response(
            content=b"app, service, and grant query params are required",
            status_code=400,
            media_type="text/plain",
        )

    row = get_db().execute("SELECT name FROM apps WHERE app_id = ?", (app_id,)).fetchone()
    if not row:
        return Response(content=b"App not found", status_code=404, media_type="text/plain")
    app_name = row["name"]

    try:
        grant = json.loads(grant_json)
    except json.JSONDecodeError:
        return Response(content=b"Invalid grant JSON", status_code=400, media_type="text/plain")

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


pages_permissions_v2_routes = [approve_permissions_v2]
