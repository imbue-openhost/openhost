import attr
from quart import Blueprint
from quart import Response
from quart import jsonify
from quart import request

from compute_space.core.auth.permissions_v2 import get_all_permissions_v2
from compute_space.core.auth.permissions_v2 import grant_permission_v2
from compute_space.core.auth.permissions_v2 import revoke_permission_v2
from compute_space.web.auth.middleware import app_auth_required
from compute_space.web.auth.middleware import login_required

api_permissions_v2_bp = Blueprint("api_permissions_v2", __name__)


@api_permissions_v2_bp.route("/api/permissions/v2", methods=["GET"])
@login_required
async def list_permissions_v2() -> Response:
    """List all V2 permissions, optionally filtered by app."""
    app_name = request.args.get("app")
    return jsonify([attr.asdict(p) for p in get_all_permissions_v2(app_name)])


@api_permissions_v2_bp.route("/api/permissions/v2/grant_global_scoped", methods=["POST"])
@login_required
async def grant_global_scoped() -> Response | tuple[Response, int]:
    """Grant a global-scoped V2 permission (owner-authed)."""
    data = await request.get_json()
    if not data:
        return jsonify({"error": "JSON body required"}), 400

    app = data.get("app")
    service_url = data.get("service_url")
    grant_payload = data.get("grant")
    if not app or not service_url or grant_payload is None:
        return jsonify({"error": "app, service_url, and grant are required"}), 400

    grant_permission_v2(
        consumer_app=app,
        service_url=service_url,
        grant_payload=grant_payload,
        scope="global",
    )
    return jsonify({"ok": True})


@api_permissions_v2_bp.route("/api/permissions/v2/revoke", methods=["POST"])
@login_required
async def revoke_v2() -> Response | tuple[Response, int]:
    """Revoke a V2 permission."""
    data = await request.get_json()
    if not data:
        return jsonify({"error": "JSON body required"}), 400

    app = data.get("app")
    service_url = data.get("service_url")
    grant_payload = data.get("grant")
    if not app or not service_url or grant_payload is None:
        return jsonify({"error": "app, service_url, and grant are required"}), 400

    revoked = revoke_permission_v2(
        consumer_app=app,
        service_url=service_url,
        grant_payload=grant_payload,
        scope=data.get("scope", "global"),
        provider_app=data.get("provider_app"),
    )
    if not revoked:
        return jsonify({"error": "Permission not found"}), 404
    return jsonify({"ok": True})


@api_permissions_v2_bp.route("/api/permissions/v2/grant_app_scoped", methods=["POST"])
@app_auth_required
async def grant_app_scoped(app_name: str) -> Response | tuple[Response, int]:
    """Grant an app-scoped V2 permission, authenticated with the provider's app token.

    The calling app must be a registered provider for the specified service.
    The permission is automatically scoped to the calling provider app.
    """
    provider_app = app_name

    data = await request.get_json()
    if not data:
        return jsonify({"error": "JSON body required"}), 400

    consumer_app = data.get("consumer_app")
    service_url = data.get("service_url")
    grant_payload = data.get("grant")
    if not consumer_app or not service_url or grant_payload is None:
        return jsonify({"error": "consumer_app, service_url, and grant are required"}), 400

    grant_permission_v2(
        consumer_app=consumer_app,
        service_url=service_url,
        grant_payload=grant_payload,
        scope="app",
        provider_app=provider_app,
    )
    return jsonify({"ok": True})
