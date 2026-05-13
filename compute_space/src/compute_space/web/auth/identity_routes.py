import base64
import urllib.parse
from typing import Annotated
from typing import Any

import attr
from cryptography.hazmat.primitives.asymmetric import rsa as rsa_module
from cryptography.hazmat.primitives.serialization import load_pem_public_key
from litestar import Response
from litestar import get
from litestar import post
from litestar.enums import RequestEncodingType
from litestar.params import Body
from litestar.response import Redirect
from litestar.response import Template

from compute_space.core import auth
from compute_space.core.auth import identity
from compute_space.core.logging import logger


@attr.s(auto_attribs=True, frozen=True)
class IdentityApproveForm:
    callback: str = ""


@get("/.well-known/jwks.json", sync_to_thread=False)
def jwks() -> dict[str, Any]:
    public_key_pem = auth.get_public_key_pem()
    public_key = load_pem_public_key(public_key_pem.encode())
    assert isinstance(public_key, rsa_module.RSAPublicKey)
    numbers = public_key.public_numbers()

    def _b64url(num: int, length: int) -> str:
        b = num.to_bytes(length, byteorder="big")
        return base64.urlsafe_b64encode(b).rstrip(b"=").decode()

    n_bytes = (numbers.n.bit_length() + 7) // 8

    jwk = {
        "kty": "RSA",
        "alg": "RS256",
        "use": "sig",
        "n": _b64url(numbers.n, n_bytes),
        "e": _b64url(numbers.e, 3),
    }
    return {"keys": [jwk]}


@get("/.well-known/openhost-identity", sync_to_thread=False)
def openhost_identity() -> Response[Any]:
    try:
        return Response(content=identity.get_zone_identity())
    except RuntimeError:
        return Response(content=b"Identity not yet available", status_code=503, media_type="text/plain")


@get("/identity/approve", sync_to_thread=False)
def identity_approve(
    user: dict[str, Any],
    callback: str = "",
    app_name: str = "an app",
    requesting_domain: str = "unknown",
) -> Response[Any] | Template:
    callback = callback.strip()
    if not callback:
        return Response(content=b"Missing callback parameter", status_code=400, media_type="text/plain")

    parsed = urllib.parse.urlparse(callback)
    if parsed.scheme not in ("https", "http") or not parsed.netloc:
        return Response(content=b"Invalid callback URL", status_code=400, media_type="text/plain")

    return Template(
        template_name="identity_approve.html",
        context={
            "callback": callback,
            "app_name": app_name,
            "requesting_domain": requesting_domain,
        },
    )


@post("/identity/approve", status_code=200)
async def identity_approve_submit(
    data: Annotated[IdentityApproveForm, Body(media_type=RequestEncodingType.URL_ENCODED)],
    user: dict[str, Any],
) -> Response[Any] | Redirect:
    callback = (data.callback or "").strip()
    if not callback:
        return Response(content=b"Missing callback parameter", status_code=400, media_type="text/plain")

    parsed = urllib.parse.urlparse(callback)
    if parsed.scheme not in ("https", "http") or not parsed.netloc:
        return Response(content=b"Invalid callback URL", status_code=400, media_type="text/plain")

    try:
        token = identity.sign_identity_token(callback)
    except RuntimeError as e:
        logger.error("Failed to sign identity token: %s", e)
        return Response(content=b"Identity service unavailable", status_code=503, media_type="text/plain")

    separator = "&" if "?" in callback else "?"
    encoded_token = urllib.parse.quote(token, safe="")
    return Redirect(path=f"{callback}{separator}identity_token={encoded_token}")


identity_routes = [jwks, openhost_identity, identity_approve, identity_approve_submit]
