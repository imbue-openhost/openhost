from typing import Any

import attr
from litestar import Response
from litestar import get
from litestar import post

from compute_space.core.auth.permissions_v2 import get_all_permissions_v2
from compute_space.core.auth.permissions_v2 import grant_permission_v2
from compute_space.core.auth.permissions_v2 import revoke_permission_v2


@attr.s(auto_attribs=True, frozen=True)
class PermissionV2Request:
    app_id: str = ""
    service_url: str = ""
    grant: Any = None
    scope: str = "global"
    provider_app_id: str | None = None


@attr.s(auto_attribs=True, frozen=True)
class AppScopedGrantRequest:
    consumer_app_id: str = ""
    service_url: str = ""
    grant: Any = None


@get("/api/permissions/v2")
async def list_permissions_v2(user: dict[str, Any], app_id: str | None = None) -> list[Any]:
    return [attr.asdict(p) for p in get_all_permissions_v2(app_id)]


@post("/api/permissions/v2/grant_global_scoped", status_code=200)
async def grant_global_scoped(data: PermissionV2Request, user: dict[str, Any]) -> Response[dict[str, Any]]:
    if not data.app_id or not data.service_url or data.grant is None:
        return Response(content={"error": "app_id, service_url, and grant are required"}, status_code=400)
    grant_permission_v2(
        consumer_app_id=data.app_id,
        service_url=data.service_url,
        grant_payload=data.grant,
        scope="global",
    )
    return Response(content={"ok": True})


@post("/api/permissions/v2/revoke", status_code=200)
async def revoke_v2(data: PermissionV2Request, user: dict[str, Any]) -> Response[dict[str, Any]]:
    if not data.app_id or not data.service_url or data.grant is None:
        return Response(content={"error": "app_id, service_url, and grant are required"}, status_code=400)
    revoked = revoke_permission_v2(
        consumer_app_id=data.app_id,
        service_url=data.service_url,
        grant_payload=data.grant,
        scope=data.scope or "global",
        provider_app_id=data.provider_app_id,
    )
    if not revoked:
        return Response(content={"error": "Permission not found"}, status_code=404)
    return Response(content={"ok": True})


@post("/api/permissions/v2/grant_app_scoped", status_code=200)
async def grant_app_scoped(data: AppScopedGrantRequest, caller_app_id: str) -> Response[dict[str, Any]]:
    app_id = caller_app_id
    if not data.consumer_app_id or not data.service_url or data.grant is None:
        return Response(content={"error": "consumer_app_id, service_url, and grant are required"}, status_code=400)
    grant_permission_v2(
        consumer_app_id=data.consumer_app_id,
        service_url=data.service_url,
        grant_payload=data.grant,
        scope="app",
        provider_app_id=app_id,
    )
    return Response(content={"ok": True})


api_permissions_v2_routes = [list_permissions_v2, grant_global_scoped, revoke_v2, grant_app_scoped]
