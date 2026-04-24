from typing import Any

import attr
from litestar import Request
from litestar import Response
from litestar import Router
from litestar import delete
from litestar import get
from litestar.response import Template

from oauth.core.credentials import DYNAMIC_CRED_PROVIDERS
from oauth.core.credentials import get_provider_creds
from oauth.core.credentials import provider_cred_keys
from oauth.core.models import CredentialsRequiredResponse
from oauth.core.models import OkResponse
from oauth.core.models import TokenInfo
from oauth.core.models import TokenListResponse
from oauth.core.providers import revoke_token
from oauth.core.tokens import get_token_by_id
from oauth.core.tokens import list_all_tokens
from oauth.core.tokens import remove_token_by_id


@get("/")
async def index(request: Request[Any, Any, Any]) -> Template:
    return Template(template_name="index.html")


@get("/api/tokens")
async def api_list_tokens() -> TokenListResponse:
    rows = list_all_tokens()
    return TokenListResponse(tokens=[TokenInfo(**r) for r in rows])


@delete("/api/tokens/{token_id:int}")
async def api_delete_token(token_id: int) -> OkResponse | Response[Any]:
    row = get_token_by_id(token_id)
    if not row:
        return OkResponse()

    client_id, client_secret = await get_provider_creds(row["provider"])
    if row["provider"] in DYNAMIC_CRED_PROVIDERS and (not client_id or not client_secret):
        id_key, secret_key = provider_cred_keys(row["provider"])
        return Response(
            content=attr.asdict(
                CredentialsRequiredResponse(
                    error="credentials_required",
                    message=(
                        f"Cannot revoke {row['provider']} token: {id_key} and "
                        f"{secret_key} must be set in the secrets app."
                    ),
                )
            ),
            status_code=503,
        )
    assert client_id is not None
    assert client_secret is not None

    remove_token_by_id(token_id)
    token_to_revoke = row.get("refresh_token") or row["access_token"]
    await revoke_token(row["provider"], token_to_revoke, client_id, client_secret)
    return OkResponse()


router = Router(path="", route_handlers=[index, api_list_tokens, api_delete_token])
