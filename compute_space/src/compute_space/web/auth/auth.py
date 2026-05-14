from quart import Response
from quart import g
from quart import request

from compute_space.web.auth.cookies import set_auth_cookies


async def attach_refreshed_token(response: Response) -> Response:
    """If a token was refreshed or created during this request, set the new cookie."""
    new_token = getattr(g, "new_access_token", None)
    if new_token:
        refresh_tok = getattr(g, "refresh_token", None)
        set_auth_cookies(response, new_token, refresh_tok, request=request)
    return response
