import json

from quart import Blueprint
from quart import Response
from quart import render_template
from quart import request

from compute_space.core.permissions_v2 import get_granted_permissions_v2
from compute_space.core.permissions_v2 import grant_permission_v2
from compute_space.web.middleware import login_required

pages_permissions_v2_bp = Blueprint("pages_permissions_v2", __name__)


@pages_permissions_v2_bp.route("/approve-permissions-v2")
@login_required
async def approve_permissions_v2() -> str | Response:
    """Owner-facing page: grant or deny a V2 permission request."""
    app_name = request.args.get("app")
    service_url = request.args.get("service")
    grant_json = request.args.get("grant")
    if not app_name or not service_url or not grant_json:
        return Response("app, service, and grant query params are required", status=400)

    try:
        grant = json.loads(grant_json)
    except json.JSONDecodeError:
        return Response("Invalid grant JSON", status=400)

    existing = get_granted_permissions_v2(app_name, service_url)
    already_granted = any(g.grant == grant and g.scope == "global" for g in existing)

    return await render_template(
        "approve_permissions_v2.html",
        app_name=app_name,
        service_url=service_url,
        grant=grant,
        grant_json=grant_json,
        already_granted=already_granted,
        redirect_url=request.args.get("return_to"),
    )


@pages_permissions_v2_bp.route("/approve-permissions-v2", methods=["POST"])
@login_required
async def approve_permissions_v2_post() -> Response:
    """Handle the grant approval form submission."""
    form = await request.form
    app_name = form.get("app")
    service_url = form.get("service")
    grant_json = form.get("grant")
    redirect_url = form.get("return_to", "")

    if not app_name or not service_url or not grant_json:
        return Response("Missing required fields", status=400)

    try:
        grant = json.loads(grant_json)
    except json.JSONDecodeError:
        return Response("Invalid grant JSON", status=400)

    grant_permission_v2(
        consumer_app=app_name,
        service_url=service_url,
        grant_payload=grant,
        scope="global",
    )

    if redirect_url:
        return Response("", status=302, headers={"Location": redirect_url})
    return Response("Permission granted", status=200)
