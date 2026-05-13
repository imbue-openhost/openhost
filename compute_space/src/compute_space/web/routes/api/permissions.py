from typing import Any

import attr
from litestar import Response
from litestar import get
from litestar import post

from compute_space.core.auth.permissions import get_granted_permissions
from compute_space.core.auth.permissions import grant_permissions
from compute_space.core.auth.permissions import revoke_permissions


@attr.s(auto_attribs=True, frozen=True)
class PermissionsRequest:
    app_id: str = ""
    permissions: list[str] = attr.Factory(list)


@get("/api/permissions")
async def list_permissions(user: dict[str, Any], app_id: str | None = None) -> Any:
    if app_id:
        return sorted(get_granted_permissions(app_id))
    return {app: sorted(keys) for app, keys in get_granted_permissions().items()}


@post("/api/permissions/grant", status_code=200)
async def grant(data: PermissionsRequest, user: dict[str, Any]) -> Response[dict[str, Any]]:
    if not data.app_id or not data.permissions:
        return Response(content={"error": "app_id and permissions are required"}, status_code=400)
    grant_permissions(data.app_id, list(data.permissions))
    return Response(content={"ok": True})


@post("/api/permissions/revoke", status_code=200)
async def revoke(data: PermissionsRequest, user: dict[str, Any]) -> Response[dict[str, Any]]:
    if not data.app_id or not data.permissions:
        return Response(content={"error": "app_id and permissions are required"}, status_code=400)
    revoke_permissions(data.app_id, list(data.permissions))
    return Response(content={"ok": True})


api_permissions_routes = [list_permissions, grant, revoke]
