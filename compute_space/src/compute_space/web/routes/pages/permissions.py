from quart import Blueprint
from quart import Response
from quart import render_template
from quart import request

from compute_space.core.permissions import get_granted_permissions
from compute_space.db import get_db
from compute_space.web.middleware import login_required

pages_permissions_bp = Blueprint("pages_permissions", __name__)


@pages_permissions_bp.route("/approve-permissions")
@login_required
async def approve_permissions() -> str | Response:
    """Owner-facing page: grant or deny requested permissions."""
    app_id = request.args.get("app")
    permissions_arg = request.args.get("permissions")
    if not app_id or not permissions_arg:
        return Response("app and permissions query params are required", status=400)

    row = get_db().execute("SELECT name FROM apps WHERE app_id = ?", (app_id,)).fetchone()
    if not row:
        return Response("App not found", status=404)
    app_name = row["name"]

    requested = permissions_arg.split(",")
    granted = get_granted_permissions(app_id)
    permissions_needed = [k for k in requested if k not in granted]
    return await render_template(
        "approve_permissions.html",
        permissions=permissions_needed,
        app_id=app_id,
        app_name=app_name,
        redirect_url=request.args.get("return_to"),
    )
