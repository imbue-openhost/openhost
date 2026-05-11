from quart import Blueprint
from quart import Response
from quart import jsonify
from quart import request

from compute_space.db import get_db
from compute_space.web.middleware import login_required

api_services_v2_bp = Blueprint("api_services_v2", __name__)


@api_services_v2_bp.route("/api/services/v2", methods=["GET"])
@login_required
async def list_services_v2() -> Response:
    """List all registered V2 service providers."""
    db = get_db()
    rows = db.execute(
        """SELECT sp.service_url, sp.app_id, a.name AS app_name, sp.service_version, sp.endpoint, a.status
           FROM service_providers_v2 sp
           JOIN apps a ON a.app_id = sp.app_id"""
    ).fetchall()
    return jsonify([dict(r) for r in rows])


@api_services_v2_bp.route("/api/services/v2/providers", methods=["GET"])
@login_required
async def discover_providers() -> Response | tuple[Response, int]:
    """Discover providers for a service, optionally filtered by version specifier."""
    service_url = request.args.get("service")
    if not service_url:
        return jsonify({"error": "service query param is required"}), 400

    db = get_db()
    rows = db.execute(
        """SELECT sp.app_id, a.name AS app_name, sp.service_version, sp.endpoint, a.status
           FROM service_providers_v2 sp
           JOIN apps a ON a.app_id = sp.app_id
           WHERE sp.service_url = ?""",
        (service_url,),
    ).fetchall()

    default = db.execute(
        "SELECT app_id FROM service_defaults WHERE service_url = ?",
        (service_url,),
    ).fetchone()
    default_app_id = default["app_id"] if default else None

    return jsonify(
        {
            "providers": [
                {
                    "app_id": r["app_id"],
                    "app_name": r["app_name"],
                    "service_version": r["service_version"],
                    "endpoint": r["endpoint"],
                    "status": r["status"],
                    "is_default": r["app_id"] == default_app_id,
                }
                for r in rows
            ]
        }
    )


@api_services_v2_bp.route("/api/services/v2/defaults", methods=["GET"])
@login_required
async def list_defaults() -> Response:
    """List all default provider settings."""
    db = get_db()
    rows = db.execute(
        """SELECT sd.service_url, sd.app_id, a.name AS app_name
           FROM service_defaults sd
           JOIN apps a ON a.app_id = sd.app_id"""
    ).fetchall()
    return jsonify([dict(r) for r in rows])


@api_services_v2_bp.route("/api/services/v2/defaults", methods=["POST"])
@login_required
async def set_default() -> Response | tuple[Response, int]:
    """Set the default provider for a service."""
    data = await request.get_json()
    if not data or not data.get("service_url") or not data.get("app_id"):
        return jsonify({"error": "service_url and app_id are required"}), 400

    db = get_db()
    # Verify the provider actually exists
    row = db.execute(
        "SELECT 1 FROM service_providers_v2 WHERE service_url = ? AND app_id = ?",
        (data["service_url"], data["app_id"]),
    ).fetchone()
    if not row:
        return jsonify({"error": "No such provider"}), 404

    db.execute(
        "INSERT OR REPLACE INTO service_defaults (service_url, app_id) VALUES (?, ?)",
        (data["service_url"], data["app_id"]),
    )
    db.commit()
    return jsonify({"ok": True})


@api_services_v2_bp.route("/api/services/v2/defaults", methods=["DELETE"])
@login_required
async def remove_default() -> Response | tuple[Response, int]:
    """Remove the default provider for a service (falls back to highest version)."""
    data = await request.get_json()
    if not data or not data.get("service_url"):
        return jsonify({"error": "service_url is required"}), 400

    db = get_db()
    db.execute("DELETE FROM service_defaults WHERE service_url = ?", (data["service_url"],))
    db.commit()
    return jsonify({"ok": True})
