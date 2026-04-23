from quart import Blueprint
from quart import Response
from quart import jsonify
from quart import request

from compute_space.core.permissions import get_granted_permissions
from compute_space.core.permissions import grant_permissions
from compute_space.core.permissions import revoke_permissions
from compute_space.web.middleware import login_required

api_permissions_bp = Blueprint("api_permissions", __name__)


@api_permissions_bp.route("/api/permissions", methods=["GET"])
@login_required
async def list_permissions() -> Response:
    """List all granted permissions, optionally filtered by app."""
    app_name = request.args.get("app")
    if app_name:
        return jsonify(sorted(await get_granted_permissions(app_name)))
    return jsonify({app: sorted(keys) for app, keys in (await get_granted_permissions()).items()})


@api_permissions_bp.route("/api/permissions/grant", methods=["POST"])
@login_required
async def grant() -> Response | tuple[Response, int]:
    """Grant permissions to an app."""
    data = await request.get_json()
    if not data or not data.get("app") or not data.get("permissions"):
        return jsonify({"error": "app and permissions are required"}), 400
    await grant_permissions(data["app"], data["permissions"])
    return jsonify({"ok": True})


@api_permissions_bp.route("/api/permissions/revoke", methods=["POST"])
@login_required
async def revoke() -> Response | tuple[Response, int]:
    """Revoke permissions from an app."""
    data = await request.get_json()
    if not data or not data.get("app") or not data.get("permissions"):
        return jsonify({"error": "app and permissions are required"}), 400
    await revoke_permissions(data["app"], data["permissions"])
    return jsonify({"ok": True})
