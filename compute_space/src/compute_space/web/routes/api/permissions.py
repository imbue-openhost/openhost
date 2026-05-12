from quart import Blueprint
from quart import Response
from quart import jsonify
from quart import request

from compute_space.core.auth.permissions import get_granted_permissions
from compute_space.core.auth.permissions import grant_permissions
from compute_space.core.auth.permissions import revoke_permissions
from compute_space.web.auth.middleware import login_required

api_permissions_bp = Blueprint("api_permissions", __name__)


@api_permissions_bp.route("/api/permissions", methods=["GET"])
@login_required
async def list_permissions() -> Response:
    """List all granted permissions, optionally filtered by app_id."""
    app_id = request.args.get("app_id")
    if app_id:
        return jsonify(sorted(get_granted_permissions(app_id)))
    return jsonify({app: sorted(keys) for app, keys in get_granted_permissions().items()})


@api_permissions_bp.route("/api/permissions/grant", methods=["POST"])
@login_required
async def grant() -> Response | tuple[Response, int]:
    """Grant permissions to an app."""
    data = await request.get_json()
    if not data or not data.get("app_id") or not data.get("permissions"):
        return jsonify({"error": "app_id and permissions are required"}), 400
    grant_permissions(data["app_id"], data["permissions"])
    return jsonify({"ok": True})


@api_permissions_bp.route("/api/permissions/revoke", methods=["POST"])
@login_required
async def revoke() -> Response | tuple[Response, int]:
    """Revoke permissions from an app."""
    data = await request.get_json()
    if not data or not data.get("app_id") or not data.get("permissions"):
        return jsonify({"error": "app_id and permissions are required"}), 400
    revoke_permissions(data["app_id"], data["permissions"])
    return jsonify({"ok": True})
