import base64
import urllib.parse
from typing import Any

import attr
from cryptography.hazmat.primitives.asymmetric import rsa as rsa_module
from cryptography.hazmat.primitives.serialization import load_pem_public_key
from litestar import Request
from litestar import Router
from litestar import get
from litestar import post
from litestar.exceptions import HTTPException
from litestar.response import Redirect
from litestar.response import Template

from compute_space.core.auth import identity
from compute_space.core.auth.keys import get_public_key_pem
from compute_space.core.logging import logger
from compute_space.web.auth.auth import require_owner_auth


@attr.s(auto_attribs=True, frozen=True)
class JwkRSA:
    kty: str
    alg: str
    use: str
    n: str
    e: str


@attr.s(auto_attribs=True, frozen=True)
class JwksResponse:
    keys: list[JwkRSA]


@attr.s(auto_attribs=True, frozen=True)
class ZoneIdentityResponse:
    domain: str
    public_key_pem: str
    protocol: str


@get("/.well-known/jwks.json", sync_to_thread=False)
def jwks() -> JwksResponse:
    """Expose the public key in JWKS format for app JWT verification."""
    public_key_pem = get_public_key_pem()
    public_key = load_pem_public_key(public_key_pem.encode())
    assert isinstance(public_key, rsa_module.RSAPublicKey)
    numbers = public_key.public_numbers()

    def _b64url(num: int, length: int) -> str:
        b = num.to_bytes(length, byteorder="big")
        return base64.urlsafe_b64encode(b).rstrip(b"=").decode()

    n_bytes = (numbers.n.bit_length() + 7) // 8
    return JwksResponse(
        keys=[
            JwkRSA(
                kty="RSA",
                alg="RS256",
                use="sig",
                n=_b64url(numbers.n, n_bytes),
                e=_b64url(numbers.e, 3),
            )
        ]
    )


@get("/.well-known/openhost-identity", sync_to_thread=False)
def openhost_identity() -> ZoneIdentityResponse:
    """Public endpoint: expose this zone's identity (domain + public key)."""
    try:
        data = identity.get_zone_identity()
    except RuntimeError as e:
        raise HTTPException(detail="Identity not yet available", status_code=503) from e
    return ZoneIdentityResponse(
        domain=data["domain"],
        public_key_pem=data["public_key_pem"],
        protocol=data["protocol"],
    )


@get("/identity/approve", guards=[require_owner_auth])
async def identity_approve(request: Request[Any, Any, Any]) -> Template:
    """Show the owner an approval page for a federated login request."""
    callback = request.query_params.get("callback", "").strip()
    app_name = request.query_params.get("app_name", "an app")
    requesting_domain = request.query_params.get("requesting_domain", "unknown")

    if not callback:
        raise HTTPException(detail="Missing callback parameter", status_code=400)

    parsed = urllib.parse.urlparse(callback)
    if parsed.scheme not in ("https", "http") or not parsed.netloc:
        raise HTTPException(detail="Invalid callback URL", status_code=400)

    return Template(
        template_name="identity_approve.html",
        context={
            "callback": callback,
            "app_name": app_name,
            "requesting_domain": requesting_domain,
        },
    )


@post("/identity/approve", status_code=302, guards=[require_owner_auth])
async def identity_approve_submit(request: Request[Any, Any, Any]) -> Redirect:
    """Owner approved the login — sign an identity token and redirect back."""
    form = await request.form()
    callback = (form.get("callback") or "").strip()
    if not callback:
        raise HTTPException(detail="Missing callback parameter", status_code=400)

    parsed = urllib.parse.urlparse(callback)
    if parsed.scheme not in ("https", "http") or not parsed.netloc:
        raise HTTPException(detail="Invalid callback URL", status_code=400)

    try:
        token = identity.sign_identity_token(callback)
    except RuntimeError as e:
        logger.error("Failed to sign identity token: %s", e)
        raise HTTPException(detail="Identity service unavailable", status_code=503) from e

    separator = "&" if "?" in callback else "?"
    encoded_token = urllib.parse.quote(token, safe="")
    return Redirect(path=f"{callback}{separator}identity_token={encoded_token}")


identity_routes = Router(
    path="/",
    route_handlers=[jwks, openhost_identity, identity_approve, identity_approve_submit],
)
