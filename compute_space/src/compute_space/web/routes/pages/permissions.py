from quart import Blueprint
from quart import Response
from quart import render_template
from quart import request

from compute_space.core.auth.permissions import get_granted_permissions
from compute_space.web.auth.middleware import login_required

pages_permissions_bp = Blueprint("pages_permissions", __name__)


@pages_permissions_bp.route("/approve-permissions")
@login_required
async def approve_permissions() -> str | Response:
    """Owner-facing page: grant or deny requested permissions."""
    app_name = request.args.get("app")
    permissions_arg = request.args.get("permissions")
    if not app_name or not permissions_arg:
        return Response("app and permissions query params are required", status=400)

    requested = permissions_arg.split(",")
    granted = get_granted_permissions(app_name)
    permissions_needed = [k for k in requested if k not in granted]
    return await render_template(
        "approve_permissions.html",
        permissions=permissions_needed,
        app_name=app_name,
        redirect_url=request.args.get("return_to"),
    )
