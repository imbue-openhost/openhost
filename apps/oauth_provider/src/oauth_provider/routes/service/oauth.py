from typing import Any
from urllib.parse import urlencode

import attr
from litestar import Request
from litestar import Response
from litestar import Router
from litestar import post

import oauth.core.config as config
from oauth.core.credentials import CredentialsNotAvailable
from oauth.core.credentials import get_provider_creds
from oauth.core.models import AccountsRequest
from oauth.core.models import AccountsResponse
from oauth.core.models import AuthRequiredResponse
from oauth.core.models import CredentialsRequiredResponse
from oauth.core.models import ErrorResponse
from oauth.core.models import OkResponse
from oauth.core.models import RevokeRequest
from oauth.core.models import TokenRequest
from oauth.core.models import TokenResponse
from oauth.core.permissions import check_oauth_v2_permission
from oauth.core.permissions import permission_denied_response
from oauth.core.providers import PROVIDERS
from oauth.core.providers import build_auth_url
from oauth.core.providers import normalize_scopes
from oauth.core.providers import pending_auth_flows
from oauth.core.providers import revoke_token
from oauth.core.tokens import find_and_remove_token
from oauth.core.tokens import get_accounts
from oauth.core.tokens import get_valid_token


@post("/oauth_service/token", status_code=200)
async def get_token_request(request: Request[Any, Any, Any], data: TokenRequest) -> TokenResponse | Response[Any]:
    """Return a valid access token, auto-refreshing if expired. Returns 401 with authorize_url if no token exists."""
    if data.account == "NEW":
        # If the client explicitly wants a new token, send them through the auth flow.
        # this is used eg to get a second account for the same provider.
        consumer_app = request.headers.get("x-openhost-consumer", "")
        return await _authorize_response(data.provider, data.scopes, data.return_to, consumer_app=consumer_app)

    missing = check_oauth_v2_permission(request, data.provider, data.scopes, data.account)
    if missing:
        body = permission_denied_response(request, data.provider, data.scopes, missing, data.return_to)
        return Response(content=attr.asdict(body), status_code=403)

    try:
        result = await get_valid_token(data.provider, data.scopes, data.account)
    except CredentialsNotAvailable as e:
        return _credentials_required_response(e)
    if result:
        return result
    return await _authorize_response(data.provider, data.scopes, data.return_to, account=data.account)


@post("/oauth_service/accounts", status_code=200)
async def svc_accounts(request: Request[Any, Any, Any], data: AccountsRequest) -> AccountsResponse | Response[Any]:
    """List account labels (emails/usernames) with stored tokens for a provider+scopes combo."""
    missing = check_oauth_v2_permission(request, data.provider, data.scopes)
    if missing:
        body = permission_denied_response(request, data.provider, data.scopes, missing)
        return Response(content=attr.asdict(body), status_code=403)

    scopes_key = normalize_scopes(data.scopes)
    return AccountsResponse(accounts=get_accounts(data.provider, scopes_key))


@post("/oauth_service/revoke", status_code=200)
async def svc_revoke(request: Request[Any, Any, Any], data: RevokeRequest) -> OkResponse | Response[Any]:
    """Revoke a stored token and attempt upstream revocation with the OAuth provider."""
    missing = check_oauth_v2_permission(request, data.provider, data.scopes, data.account)
    if missing:
        body = permission_denied_response(request, data.provider, data.scopes, missing)
        return Response(content=attr.asdict(body), status_code=403)

    scopes_key = normalize_scopes(data.scopes)
    token_row = find_and_remove_token(data.provider, scopes_key, data.account)
    if not token_row:
        return Response(
            content=attr.asdict(ErrorResponse(error="token_not_found")),
            status_code=404,
        )

    try:
        client_id, client_secret = await get_provider_creds(data.provider)
        token_to_revoke = token_row.refresh_token or token_row.access_token
        await revoke_token(data.provider, token_to_revoke, client_id, client_secret)
    except CredentialsNotAvailable:
        pass

    return OkResponse()


# ─── Helpers ───


async def _authorize_response(
    provider_name: str,
    scopes: list[str],
    return_to: str,
    account: str = "NEW",
    consumer_app: str = "",
) -> Response[Any]:
    """Build a 401 response with an authorize_url pointing to the provider's auth page or device flow."""
    provider = PROVIDERS[provider_name]
    flow_type = provider.get("flow", "auth_code")

    if flow_type == "device":
        if not config.ZONE_DOMAIN:
            return Response(
                content=attr.asdict(ErrorResponse(error="ZONE_DOMAIN not configured")),
                status_code=500,
            )
        params = urlencode(
            {
                "provider": provider_name,
                "scopes": ",".join(scopes),
                "return_to": return_to,
                "account": account,
                "consumer": consumer_app,
            }
        )
        authorize_url = f"https://{config.APP_NAME}.{config.ZONE_DOMAIN}/device?{params}"
    else:
        try:
            client_id, client_secret = await get_provider_creds(provider_name)
        except CredentialsNotAvailable as e:
            return _credentials_required_response(e)
        authorize_url = build_auth_url(
            provider_name,
            scopes,
            config.OAUTH_REDIRECT_URI,
            return_to,
            client_id,
            account=account,
        )
        if consumer_app:
            state = list(pending_auth_flows.keys())[-1]
            pending_auth_flows[state]["consumer_app"] = consumer_app
            pending_auth_flows[state]["service_url"] = config.OAUTH_SERVICE_URL

    return Response(
        content=attr.asdict(AuthRequiredResponse(status="authorization_required", authorize_url=authorize_url)),
        status_code=401,
    )


def _credentials_required_response(exc: CredentialsNotAvailable) -> Response[Any]:
    return Response(
        content=attr.asdict(CredentialsRequiredResponse(error="credentials_required", message=exc.message)),
        status_code=503,
    )


router = Router(path="", route_handlers=[get_token_request, svc_accounts, svc_revoke])
