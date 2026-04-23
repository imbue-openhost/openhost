from quart import Blueprint
from quart import Response
from quart import jsonify
from sqlalchemy import select

from compute_space.db import get_session
from compute_space.db.models import App
from compute_space.db.models import ServiceProvider
from compute_space.web.middleware import login_required

api_services_bp = Blueprint("api_services", __name__)


@api_services_bp.route("/api/services", methods=["GET"])
@login_required
async def api_services() -> Response:
    """List all registered services and their providers."""
    session = get_session()
    rows = (
        await session.execute(
            select(ServiceProvider.service_name, ServiceProvider.app_name, App.status).join(
                App, App.name == ServiceProvider.app_name
            )
        )
    ).all()
    return jsonify([{"service_name": r.service_name, "app_name": r.app_name, "status": r.status} for r in rows])
