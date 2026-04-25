from datetime import UTC
from datetime import datetime
from datetime import timedelta
from typing import Any
from urllib.parse import urlencode

import attr
from litestar import Request
from litestar import Response
from litestar import Router
from litestar import post

import oauth.core.config as config
from oauth.core.credentials import DYNAMIC_CRED_PROVIDERS
from oauth.core.credentials import get_provider_creds
from oauth.core.credentials import provider_cred_keys
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
from oauth.core.providers import refresh_access_token
from oauth.core.providers import revoke_token
from oauth.core.tokens import find_and_remove_token
from oauth.core.tokens import get_accounts
from oauth.core.tokens import get_token
from oauth.core.tokens import get_tokens_for_provider_scopes
from oauth.core.tokens import update_token_access


@post("/_svc/token", status_code=200)
async def svc_token(request: Request[Any, Any, Any], data: TokenRequest) -> TokenResponse | Response[Any]:
    if not data.provider or data.provider not in PROVIDERS:
        return Response(
            content=attr.asdict(ErrorResponse(error="unknown_provider", provider=data.provider)),
            status_code=400,
        )
    if not data.scopes:
        return Response(
            content=attr.asdict(ErrorResponse(error="missing_scopes", message="At least one scope is required")),
            status_code=400,
        )

    if data.account == "NEW":
        consumer_app = request.headers.get("x-openhost-consumer", "")
        return await _authorize_response(data.provider, data.scopes, data.return_to, consumer_app=consumer_app)

    missing = check_oauth_v2_permission(request, data.provider, data.scopes, data.account)
    if missing:
        body = permission_denied_response(request, data.provider, data.scopes, missing, data.return_to)
        return Response(content=attr.asdict(body), status_code=403)

    return await _get_or_refresh_token(data.provider, data.scopes, data.account, data.return_to)


@post("/_svc/accounts", status_code=200)
async def svc_accounts(request: Request[Any, Any, Any], data: AccountsRequest) -> AccountsResponse | Response[Any]:
    if not data.provider or data.provider not in PROVIDERS:
        return Response(
            content=attr.asdict(ErrorResponse(error="unknown_provider", provider=data.provider)),
            status_code=400,
        )
    if not data.scopes:
        return Response(
            content=attr.asdict(ErrorResponse(error="missing_scopes", message="At least one scope is required")),
            status_code=400,
        )
    missing = check_oauth_v2_permission(request, data.provider, data.scopes)
    if missing:
        body = permission_denied_response(request, data.provider, data.scopes, missing)
        return Response(content=attr.asdict(body), status_code=403)

    scopes_key = normalize_scopes(data.scopes)
    return AccountsResponse(accounts=get_accounts(data.provider, scopes_key))


@post("/_svc/revoke", status_code=200)
async def svc_revoke(request: Request[Any, Any, Any], data: RevokeRequest) -> OkResponse | Response[Any]:
    if not data.provider or data.provider not in PROVIDERS:
        return Response(
            content=attr.asdict(ErrorResponse(error="unknown_provider", provider=data.provider)),
            status_code=400,
        )
    if not data.scopes:
        return Response(
            content=attr.asdict(ErrorResponse(error="missing_scopes", message="At least one scope is required")),
            status_code=400,
        )
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

    client_id, client_secret = await get_provider_creds(data.provider)
    if client_id and client_secret:
        token_to_revoke = token_row.get("refresh_token") or token_row["access_token"]
        await revoke_token(data.provider, token_to_revoke, client_id, client_secret)

    return OkResponse()


# ─── Helpers ───


async def _get_or_refresh_token(
    provider_name: str, scopes: list[str], account: str, return_to: str
) -> TokenResponse | Response[Any]:
    scopes_key = normalize_scopes(scopes)

    row = get_token(provider_name, scopes_key, account)
    if not row and account == "default":
        rows = get_tokens_for_provider_scopes(provider_name, scopes_key)
        if len(rows) == 1:
            row = rows[0]

    if row:
        expires_at = row["expires_at"]
        if not expires_at:
            return TokenResponse(access_token=row["access_token"], expires_at=None)

        exp = datetime.fromisoformat(expires_at)
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=UTC)
        if exp > datetime.now(UTC) + timedelta(seconds=60):
            return TokenResponse(access_token=row["access_token"], expires_at=row["expires_at"])

        if row["refresh_token"]:
            client_id, client_secret = await get_provider_creds(provider_name)
            if provider_name in DYNAMIC_CRED_PROVIDERS and (not client_id or not client_secret):
                return _credentials_required_response(provider_name)
            assert client_id is not None
            assert client_secret is not None
            refreshed = await refresh_access_token(provider_name, row["refresh_token"], client_id, client_secret)
            if refreshed and "access_token" in refreshed:
                new_expires_at = None
                if refreshed.get("expires_in"):
                    new_expires_at = (datetime.now(UTC) + timedelta(seconds=refreshed["expires_in"])).isoformat()
                update_token_access(row["id"], refreshed["access_token"], new_expires_at)
                return TokenResponse(access_token=refreshed["access_token"], expires_at=new_expires_at)

    return await _authorize_response(provider_name, scopes, return_to, account=account)


async def _authorize_response(
    provider_name: str,
    scopes: list[str],
    return_to: str,
    account: str = "NEW",
    consumer_app: str = "",
) -> Response[Any]:
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
        client_id, client_secret = await get_provider_creds(provider_name)
        if provider_name in DYNAMIC_CRED_PROVIDERS and (not client_id or not client_secret):
            return _credentials_required_response(provider_name)
        assert client_id is not None
        assert client_secret is not None
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


def _credentials_required_response(provider_name: str) -> Response[Any]:
    id_key, secret_key = provider_cred_keys(provider_name)
    return Response(
        content=attr.asdict(
            CredentialsRequiredResponse(
                error="credentials_required",
                message=(
                    f"{provider_name.capitalize()} OAuth requires {id_key} and "
                    f"{secret_key} to be set in the secrets app."
                ),
            )
        ),
        status_code=503,
    )


router = Router(path="", route_handlers=[svc_token, svc_accounts, svc_revoke])
