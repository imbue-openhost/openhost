from typing import Any

import attr
from litestar import Response
from litestar import Router
from litestar import delete
from litestar import get

from oauth_provider.core.credentials import CredentialsNotAvailable
from oauth_provider.core.credentials import get_provider_creds
from oauth_provider.core.models import CredentialsRequiredResponse
from oauth_provider.core.models import OkResponse
from oauth_provider.core.models import TokenListResponse
from oauth_provider.core.providers import revoke_token
from oauth_provider.core.tokens import get_token_by_id
from oauth_provider.core.tokens import list_all_tokens
from oauth_provider.core.tokens import remove_token_by_id


@get("/api/tokens")
async def api_list_tokens() -> TokenListResponse:
    """List all stored tokens (without secrets) for the dashboard."""
    return TokenListResponse(tokens=list_all_tokens())


@delete("/api/tokens/{token_id:int}", status_code=200)
async def api_delete_token(token_id: int) -> OkResponse | Response[Any]:
    """Delete a stored token and revoke it with the upstream OAuth provider."""
    row = get_token_by_id(token_id)
    if not row:
        return OkResponse()

    try:
        client_id, client_secret = await get_provider_creds(row.provider)
    except CredentialsNotAvailable as e:
        return Response(
            content=attr.asdict(CredentialsRequiredResponse(error="credentials_required", message=e.message)),
            status_code=503,
        )

    remove_token_by_id(token_id)
    token_to_revoke = row.refresh_token or row.access_token
    await revoke_token(row.provider, token_to_revoke, client_id, client_secret)
    return OkResponse()


router = Router(path="", route_handlers=[api_list_tokens, api_delete_token])
