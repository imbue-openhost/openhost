import json

from quart import Blueprint
from quart import Response
from quart import render_template
from quart import request

from compute_space.core.auth.permissions_v2 import get_granted_permissions_v2
from compute_space.db import get_db
from compute_space.web.auth.middleware import login_required

pages_permissions_v2_bp = Blueprint("pages_permissions_v2", __name__)


@pages_permissions_v2_bp.route("/approve-permissions-v2")
@login_required
async def approve_permissions_v2() -> str | Response:
    """Owner-facing page: grant or deny a V2 permission request."""
    app_id = request.args.get("app")
    service_url = request.args.get("service")
    grant_json = request.args.get("grant")
    if not app_id or not service_url or not grant_json:
        return Response("app, service, and grant query params are required", status=400)

    row = get_db().execute("SELECT name FROM apps WHERE app_id = ?", (app_id,)).fetchone()
    if not row:
        return Response("App not found", status=404)
    app_name = row["name"]

    try:
        grant = json.loads(grant_json)
    except json.JSONDecodeError:
        return Response("Invalid grant JSON", status=400)

    existing = get_granted_permissions_v2(app_id, service_url)
    already_granted = any(g.grant == grant and g.scope == "global" for g in existing)

    return await render_template(
        "approve_permissions_v2.html",
        app_id=app_id,
        app_name=app_name,
        service_url=service_url,
        grant=grant,
        grant_json=grant_json,
        already_granted=already_granted,
        redirect_url=request.args.get("return_to"),
    )
