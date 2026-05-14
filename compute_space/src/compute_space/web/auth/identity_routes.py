import base64
import urllib.parse

from cryptography.hazmat.primitives.asymmetric import rsa as rsa_module
from cryptography.hazmat.primitives.serialization import load_pem_public_key
from quart import Blueprint
from quart import Response
from quart import jsonify
from quart import redirect
from quart import render_template
from quart import request
from quart.typing import ResponseReturnValue

from compute_space.core.auth import identity
from compute_space.core.auth.keys import get_public_key_pem
from compute_space.core.logging import logger
from compute_space.web.auth.middleware import login_required

identity_bp = Blueprint("identity", __name__)


# ─── JWKS endpoint ───


@identity_bp.route("/.well-known/jwks.json")
def jwks() -> Response:
    """Expose the public key in JWKS format for app JWT verification."""
    public_key_pem = get_public_key_pem()
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
    return jsonify({"keys": [jwk]})


# ─── Federated Identity ───


@identity_bp.route("/.well-known/openhost-identity")
def openhost_identity() -> Response:
    """Public endpoint: expose this zone's identity (domain + public key)."""
    try:
        return jsonify(identity.get_zone_identity())
    except RuntimeError:
        return Response("Identity not yet available", status=503)


@identity_bp.route("/identity/approve")
@login_required
async def identity_approve() -> ResponseReturnValue:
    """Show the owner an approval page for a federated login request."""
    callback = request.args.get("callback", "").strip()
    app_name = request.args.get("app_name", "an app")
    requesting_domain = request.args.get("requesting_domain", "unknown")

    if not callback:
        return Response("Missing callback parameter", status=400)

    parsed = urllib.parse.urlparse(callback)
    if parsed.scheme not in ("https", "http") or not parsed.netloc:
        return Response("Invalid callback URL", status=400)

    return await render_template(
        "identity_approve.html",
        callback=callback,
        app_name=app_name,
        requesting_domain=requesting_domain,
    )


@identity_bp.route("/identity/approve", methods=["POST"])
@login_required
async def identity_approve_submit() -> ResponseReturnValue:
    """Owner approved the login — sign an identity token and redirect back."""
    form = await request.form
    callback = form.get("callback", "").strip()
    if not callback:
        return Response("Missing callback parameter", status=400)

    parsed = urllib.parse.urlparse(callback)
    if parsed.scheme not in ("https", "http") or not parsed.netloc:
        return Response("Invalid callback URL", status=400)

    try:
        token = identity.sign_identity_token(callback)
    except RuntimeError as e:
        logger.error("Failed to sign identity token: %s", e)
        return Response("Identity service unavailable", status=503)

    separator = "&" if "?" in callback else "?"
    encoded_token = urllib.parse.quote(token, safe="")
    return redirect(f"{callback}{separator}identity_token={encoded_token}")
