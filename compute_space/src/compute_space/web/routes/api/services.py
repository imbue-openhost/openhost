from typing import Any

from litestar import get

from compute_space.db import get_db


@get("/api/services")
async def api_services(user: dict[str, Any]) -> list[dict[str, Any]]:
    db = get_db()
    providers = db.execute(
        """SELECT sp.service_name, sp.app_id, a.name AS app_name, a.status
           FROM service_providers sp JOIN apps a ON a.app_id = sp.app_id"""
    ).fetchall()
    return [dict(r) for r in providers]


api_services_routes = [api_services]
