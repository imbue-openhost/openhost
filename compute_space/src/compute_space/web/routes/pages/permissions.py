from typing import Any

from litestar import Request
from litestar import Response
from litestar import get
from litestar.response import Template

from compute_space.core.auth.permissions import get_granted_permissions
from compute_space.db import get_db


@get("/approve-permissions", sync_to_thread=False)
def approve_permissions(request: Request[Any, Any, Any], user: dict[str, Any]) -> Response[bytes] | Template:
    app_id = request.query_params.get("app")
    permissions_arg = request.query_params.get("permissions")
    if not app_id or not permissions_arg:
        return Response(
            content=b"app and permissions query params are required",
            status_code=400,
            media_type="text/plain",
        )

    row = get_db().execute("SELECT name FROM apps WHERE app_id = ?", (app_id,)).fetchone()
    if not row:
        return Response(content=b"App not found", status_code=404, media_type="text/plain")
    app_name = row["name"]

    requested = permissions_arg.split(",")
    granted = get_granted_permissions(app_id)
    permissions_needed = [k for k in requested if k not in granted]
    return Template(
        template_name="approve_permissions.html",
        context={
            "permissions": permissions_needed,
            "app_id": app_id,
            "app_name": app_name,
            "redirect_url": request.query_params.get("return_to"),
        },
    )


pages_permissions_routes = [approve_permissions]
