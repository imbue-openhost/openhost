import json
from typing import Any
from urllib.parse import urlencode

import httpx
from litestar import Request

from oauth.core.config import APP_NAME
from oauth.core.config import APP_TOKEN
from oauth.core.config import ROUTER_URL
from oauth.core.config import ZONE_DOMAIN
from oauth.core.models import GrantPayload
from oauth.core.models import PermissionDeniedResponse
from oauth.core.models import RequiredGrant


def parse_oauth_v2_grants(request: Request[Any, Any, Any]) -> list[dict[str, Any]]:
    perms_header = request.headers.get("x-openhost-permissions", "[]")
    try:
        grants = json.loads(perms_header)
    except json.JSONDecodeError:
        return []

    result = []
    for g in grants:
        payload = g.get("grant", {})
        if isinstance(payload, dict) and "provider" in payload:
            result.append(payload)
    return result


def check_oauth_v2_permission(
    request: Request[Any, Any, Any], provider: str, scopes: list[str], account: str | None = None
) -> list[str]:
    grants = parse_oauth_v2_grants(request)
    granted_scopes: set[str] = set()
    for g in grants:
        if g["provider"] != provider:
            continue
        grant_account = g.get("account")
        if grant_account is not None and account is not None and grant_account != account:
            continue
        granted_scopes.update(g.get("scopes", []))
    return [s for s in scopes if s not in granted_scopes]


def permission_denied_response(
    request: Request[Any, Any, Any],
    provider: str,
    scopes: list[str],
    missing_scopes: list[str],
    return_to: str = "",
) -> PermissionDeniedResponse:
    consumer_app = request.headers.get("x-openhost-consumer", "")
    params = urlencode(
        {
            "provider": provider,
            "scopes": ",".join(scopes),
            "consumer": consumer_app,
            "return_to": return_to,
        }
    )
    return PermissionDeniedResponse(
        error="permission_required",
        required_grant=RequiredGrant(
            grant_payload=GrantPayload(provider=provider, scopes=missing_scopes),
            scope="app",
            grant_url=f"//{APP_NAME}.{ZONE_DOMAIN}/grant?{params}",
        ),
    )


async def grant_app_scoped_permission(consumer_app: str, service_url: str, grant: dict[str, Any]) -> bool:
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            f"{ROUTER_URL}/api/permissions_v2/grant-app-scoped",
            json={
                "consumer_app": consumer_app,
                "service_url": service_url,
                "grant": grant,
            },
            headers={"Authorization": f"Bearer {APP_TOKEN}"},
        )
        return resp.status_code == 200
