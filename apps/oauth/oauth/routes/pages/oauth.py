from typing import Any
from urllib.parse import urlencode

from litestar import MediaType
from litestar import Request
from litestar import Response
from litestar import Router
from litestar import get
from litestar.response import Redirect
from litestar.response import Template

import oauth.core.config as config
from oauth.core.credentials import DYNAMIC_CRED_PROVIDERS
from oauth.core.credentials import get_provider_creds
from oauth.core.credentials import provider_cred_keys
from oauth.core.models import DevicePollResponse
from oauth.core.permissions import grant_app_scoped_permission
from oauth.core.providers import PROVIDERS
from oauth.core.providers import active_device_flows
from oauth.core.providers import build_auth_url
from oauth.core.providers import create_device_flow
from oauth.core.providers import exchange_code
from oauth.core.providers import fetch_account_identity
from oauth.core.providers import normalize_scopes
from oauth.core.providers import pending_auth_flows
from oauth.core.providers import start_device_flow
from oauth.core.tokens import store_token


@get("/grant")
async def oauth_grant(
    request: Request[Any, Any, Any],
    provider: str = "",
    scopes: str = "",
    consumer: str = "",
    return_to: str = "",
) -> Redirect | Response[Any]:
    if not provider or provider not in PROVIDERS:
        return Response(
            content=f"Unknown provider: {provider}",
            status_code=400,
            media_type=MediaType.TEXT,
        )
    if not scopes:
        return Response(content="No scopes specified", status_code=400, media_type=MediaType.TEXT)
    if not consumer:
        return Response(
            content="No consumer app specified",
            status_code=400,
            media_type=MediaType.TEXT,
        )

    scopes_list = scopes.split(",")
    prov = PROVIDERS[provider]
    flow_type = prov.get("flow", "auth_code")

    if flow_type == "device":
        params = urlencode(
            {
                "provider": provider,
                "scopes": scopes,
                "return_to": return_to,
                "account": "default",
                "consumer": consumer,
            }
        )
        return Redirect(path=f"/device?{params}")

    client_id, client_secret = await get_provider_creds(provider)
    if provider in DYNAMIC_CRED_PROVIDERS and (not client_id or not client_secret):
        id_key, secret_key = provider_cred_keys(provider)
        return Response(
            content=(
                f"{provider.capitalize()} OAuth requires {id_key} and {secret_key} to be set in the secrets app."
            ),
            status_code=503,
            media_type=MediaType.TEXT,
        )
    assert client_id is not None
    assert client_secret is not None

    authorize_url = build_auth_url(
        provider,
        scopes_list,
        config.OAUTH_REDIRECT_URI,
        return_to,
        client_id,
        account="default",
    )
    state = list(pending_auth_flows.keys())[-1]
    pending_auth_flows[state]["consumer_app"] = consumer
    pending_auth_flows[state]["service_url"] = config.OAUTH_SERVICE_URL

    return Redirect(path=authorize_url)


@get("/callback")
async def oauth_callback(
    request: Request[Any, Any, Any],
    code: str = "",
    state: str = "",
    error: str = "",
    scope: str = "",
) -> Redirect | Response[Any]:
    if error:
        return Response(
            content=f"Authorization denied: {error}",
            status_code=400,
            media_type=MediaType.TEXT,
        )
    if not code or not state:
        return Response(
            content="Missing code or state parameter",
            status_code=400,
            media_type=MediaType.TEXT,
        )

    flow = pending_auth_flows.pop(state, None)
    if not flow:
        return Response(
            content="Invalid or expired authorization flow",
            status_code=400,
            media_type=MediaType.TEXT,
        )

    if scope and flow["scopes"]:
        granted = set(scope.split())
        missing = [s for s in flow["scopes"] if s not in granted]
        if missing:
            return Response(
                content=(
                    f"Authorization incomplete — the following permissions were not "
                    f"granted: {', '.join(missing)}. Please try again and make sure "
                    f"all permissions are checked."
                ),
                status_code=400,
                media_type=MediaType.TEXT,
            )

    client_id, client_secret = await get_provider_creds(flow["provider"])
    if flow["provider"] in DYNAMIC_CRED_PROVIDERS and (not client_id or not client_secret):
        id_key, secret_key = provider_cred_keys(flow["provider"])
        return Response(
            content=(f"OAuth credentials missing: set {id_key} and {secret_key} in the secrets app."),
            status_code=503,
            media_type=MediaType.TEXT,
        )
    assert client_id is not None
    assert client_secret is not None

    result = await exchange_code(flow["provider"], code, flow["redirect_uri"], client_id, client_secret)
    if "error" in result:
        return Response(
            content=f"Token exchange failed: {result.get('error_description', result['error'])}",
            status_code=502,
            media_type=MediaType.TEXT,
        )

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
            flow["service_url"],
            {
                "provider": flow["provider"],
                "scopes": flow["scopes"],
                "account": account,
            },
        )

    return_to = flow["return_to"]
    if not return_to.startswith("/"):
        return_to = "/"
    if return_to.startswith("//"):
        if not config.ZONE_DOMAIN:
            return_to = "/"
        else:
            parts = return_to.split("/")
            domain = parts[2] if len(parts) >= 3 else ""
            if domain != config.ZONE_DOMAIN and not domain.endswith(f".{config.ZONE_DOMAIN}"):
                return_to = "/"
    return Redirect(path=return_to)


@get("/device")
async def device_page(
    request: Request[Any, Any, Any],
    provider: str = "",
    scopes: str = "",
    return_to: str = "/",
    account: str = "default",
    consumer: str = "",
) -> Template | Response[Any]:
    if not provider or provider not in PROVIDERS:
        return Response(
            content=f"Unknown provider: {provider}",
            status_code=400,
            media_type=MediaType.TEXT,
        )
    if not scopes:
        return Response(content="No scopes specified", status_code=400, media_type=MediaType.TEXT)

    scopes_list = scopes.split(",")
    flow_data = await start_device_flow(provider, scopes_list)
    if "error" in flow_data:
        return Response(
            content=(f"Failed to start device flow: {flow_data.get('error_description', flow_data['error'])}"),
            status_code=502,
            media_type=MediaType.TEXT,
        )

    flow_id = create_device_flow(provider, scopes_list, flow_data, account=account, consumer_app=consumer)
    flow = active_device_flows[flow_id]

    return Template(
        template_name="device.html",
        context={
            "user_code": flow["user_code"],
            "verification_url": flow["verification_url"],
            "flow_id": flow_id,
            "provider": provider,
            "scopes": scopes_list,
            "return_to": return_to,
            "zone_domain": config.ZONE_DOMAIN,
        },
    )


@get("/device/poll/{flow_id:str}")
async def device_poll(flow_id: str) -> DevicePollResponse:
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


router = Router(path="", route_handlers=[oauth_grant, oauth_callback, device_page, device_poll])
