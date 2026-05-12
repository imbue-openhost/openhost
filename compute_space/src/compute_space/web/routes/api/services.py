from quart import Blueprint
from quart import Response
from quart import jsonify

from compute_space.db import get_db
from compute_space.web.auth.middleware import login_required

api_services_bp = Blueprint("api_services", __name__)


@api_services_bp.route("/api/services", methods=["GET"])
@login_required
async def api_services() -> Response:
    """List all registered services and their providers."""
    db = get_db()
    providers = db.execute(
        """SELECT sp.service_name, sp.app_id, a.name AS app_name, a.status
           FROM service_providers sp JOIN apps a ON a.app_id = sp.app_id"""
    ).fetchall()
    return jsonify([dict(r) for r in providers])
