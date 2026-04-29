from litestar import Router
from litestar import get

import oauth.core.config as config
from oauth.core.models import DevicePollResponse
from oauth.core.permissions import grant_app_scoped_permission
from oauth.core.providers import active_device_flows
from oauth.core.providers import fetch_account_identity
from oauth.core.providers import normalize_scopes
from oauth.core.tokens import store_token


@get("/device/poll/{flow_id:str}")
async def device_poll(flow_id: str) -> DevicePollResponse:
    """Poll a device flow's status. Returns completed/pending/error. Stores the token on completion."""
    flow = active_device_flows.get(flow_id)
    if not flow:
        return DevicePollResponse(status="expired", error="Flow not found or expired")

    if flow["status"] == "completed":
        result = flow["result"]
        scopes_key = normalize_scopes(flow["scopes"])
        account = flow.get("account", "default")
        identity = await fetch_account_identity(flow["provider"], result["access_token"])
        if identity:
            account = identity
        store_token(
            flow["provider"],
            scopes_key,
            account,
            result["access_token"],
            result.get("refresh_token"),
            result.get("expires_at"),
        )
        consumer_app = flow.get("consumer_app")
        if consumer_app:
            await grant_app_scoped_permission(
                consumer_app,
                config.OAUTH_SERVICE_URL,
                {
                    "provider": flow["provider"],
                    "scopes": flow["scopes"],
                    "account": account,
                },
            )
        active_device_flows.pop(flow_id, None)
        return DevicePollResponse(status="completed")

    if flow["status"] == "error":
        active_device_flows.pop(flow_id, None)
        return DevicePollResponse(
            status="error",
            error=flow["result"].get("message", "Unknown error"),
        )

    return DevicePollResponse(status="pending")


router = Router(path="", route_handlers=[device_poll])
