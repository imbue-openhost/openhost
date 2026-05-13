from typing import Any

import attr
from litestar import Response
from litestar import delete
from litestar import get
from litestar import post

from compute_space.db import get_db


@attr.s(auto_attribs=True, frozen=True)
class DefaultProviderRequest:
    service_url: str = ""
    app_id: str = ""


@attr.s(auto_attribs=True, frozen=True)
class RemoveDefaultRequest:
    service_url: str = ""


@get("/api/services/v2")
async def list_services_v2(user: dict[str, Any]) -> list[dict[str, Any]]:
    db = get_db()
    rows = db.execute(
        """SELECT sp.service_url, sp.app_id, a.name AS app_name, sp.service_version, sp.endpoint, a.status
           FROM service_providers_v2 sp
           JOIN apps a ON a.app_id = sp.app_id"""
    ).fetchall()
    return [dict(r) for r in rows]


@get("/api/services/v2/providers")
async def discover_providers(user: dict[str, Any], service: str | None = None) -> Response[dict[str, Any]]:
    if not service:
        return Response(content={"error": "service query param is required"}, status_code=400)
    db = get_db()
    rows = db.execute(
        """SELECT sp.app_id, a.name AS app_name, sp.service_version, sp.endpoint, a.status
           FROM service_providers_v2 sp
           JOIN apps a ON a.app_id = sp.app_id
           WHERE sp.service_url = ?""",
        (service,),
    ).fetchall()
    default = db.execute(
        "SELECT app_id FROM service_defaults WHERE service_url = ?",
        (service,),
    ).fetchone()
    default_app_id = default["app_id"] if default else None
    return Response(
        content={
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


@get("/api/services/v2/defaults")
async def list_defaults(user: dict[str, Any]) -> list[dict[str, Any]]:
    db = get_db()
    rows = db.execute(
        """SELECT sd.service_url, sd.app_id, a.name AS app_name
           FROM service_defaults sd
           JOIN apps a ON a.app_id = sd.app_id"""
    ).fetchall()
    return [dict(r) for r in rows]


@post("/api/services/v2/defaults", status_code=200)
async def set_default(data: DefaultProviderRequest, user: dict[str, Any]) -> Response[dict[str, Any]]:
    if not data.service_url or not data.app_id:
        return Response(content={"error": "service_url and app_id are required"}, status_code=400)
    db = get_db()
    row = db.execute(
        "SELECT 1 FROM service_providers_v2 WHERE service_url = ? AND app_id = ?",
        (data.service_url, data.app_id),
    ).fetchone()
    if not row:
        return Response(content={"error": "No such provider"}, status_code=404)
    db.execute(
        "INSERT OR REPLACE INTO service_defaults (service_url, app_id) VALUES (?, ?)",
        (data.service_url, data.app_id),
    )
    db.commit()
    return Response(content={"ok": True})


@delete("/api/services/v2/defaults", status_code=200)
async def remove_default(data: RemoveDefaultRequest, user: dict[str, Any]) -> Response[dict[str, Any]]:
    if not data.service_url:
        return Response(content={"error": "service_url is required"}, status_code=400)
    db = get_db()
    db.execute("DELETE FROM service_defaults WHERE service_url = ?", (data.service_url,))
    db.commit()
    return Response(content={"ok": True})


api_services_v2_routes = [list_services_v2, discover_providers, list_defaults, set_default, remove_default]
