"""JWT authentication for the OpenHost router.

The router always generates/loads its own RSA 2048-bit keypair and signs/verifies
JWTs locally.
"""

import hashlib
import secrets
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from pathlib import Path
from typing import Any

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from quart import Request
from quart import Response

from compute_space.config import get_config
from compute_space.core.logging import logger
from compute_space.core.util import write_restricted
from compute_space.db import get_db

_private_key: str | None = None
_public_key: str | None = None

ACCESS_TOKEN_EXPIRY = 3600  # 60 minutes
REFRESH_TOKEN_EXPIRY = 2592000  # 30 days

COOKIE_ACCESS = "zone_auth"
COOKIE_REFRESH = "zone_refresh"


def _generate_keypair(keys_dir: str) -> tuple[str, str]:
    """Generate RS256 keypair, write PEM files, return (private_pem, public_pem)."""
    keys_path = Path(keys_dir)
    keys_path.mkdir(parents=True, exist_ok=True)

    private_key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=2048,
    )
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()

    public_pem = (
        private_key.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )

    write_restricted(keys_path / "private.pem", private_pem)
    (keys_path / "public.pem").write_text(public_pem)

    return private_pem, public_pem


def load_keys(keys_dir: str) -> None:
    """Load or generate local keypair. Call once at startup."""
    global _private_key, _public_key

    priv_path = Path(keys_dir) / "private.pem"
    pub_path = Path(keys_dir) / "public.pem"

    if priv_path.exists() and pub_path.exists():
        _private_key = priv_path.read_text()
        _public_key = pub_path.read_text()
    else:
        _private_key, _public_key = _generate_keypair(keys_dir)


def get_public_key_pem() -> str:
    """Return the PEM-encoded public key string."""
    if _public_key is None:
        raise RuntimeError("Keys not loaded. Call load_keys() first.")
    return _public_key


def _zone_audience() -> str:
    """Return the audience/issuer identifier for this zone."""
    return get_config().zone_domain or "localhost"


def create_access_token(username: str) -> str:
    """Create a signed RS256 JWT."""
    if _private_key is None:
        raise RuntimeError("Keys not loaded. Call load_keys() first.")
    now = datetime.now(UTC)
    zone = _zone_audience()
    payload = {
        "sub": username,
        "username": username,
        "iss": zone,
        "aud": zone,
        "iat": now,
        "exp": now + timedelta(seconds=ACCESS_TOKEN_EXPIRY),
    }
    return jwt.encode(payload, _private_key, algorithm="RS256")


def create_refresh_token() -> str:
    """Generate an opaque refresh token string."""
    return secrets.token_urlsafe(48)


def decode_access_token(token: str) -> dict[str, Any] | None:
    """Verify and decode a JWT. Returns claims dict or None."""
    try:
        if _public_key is None:
            raise RuntimeError("public key not loaded")
        zone = _zone_audience()
        return jwt.decode(token, _public_key, algorithms=["RS256"], audience=zone, issuer=zone)
    except jwt.InvalidTokenError:
        return None


REFRESH_GRACE_PERIOD = timedelta(hours=2)


def decode_access_token_allow_expired(token: str) -> dict[str, Any] | None:
    """Decode a JWT allowing up to REFRESH_GRACE_PERIOD past expiry -- used during token refresh."""
    try:
        if _public_key is None:
            raise RuntimeError("public key not loaded")
        zone = _zone_audience()
        return jwt.decode(
            token,
            _public_key,
            algorithms=["RS256"],
            audience=zone,
            issuer=zone,
            leeway=REFRESH_GRACE_PERIOD,
        )
    except jwt.InvalidTokenError:
        return None


def _cookie_domain(request_host: str | None = None) -> str | None:
    """Return the cookie domain, or None to use the default (request host).

    When zone_domain is set (e.g. "user.dev-host.imbue.com") and the request
    is coming from that domain (or a subdomain of it), the cookie domain is
    set explicitly so cookies are shared with app subdomains like
    "dau-tracker.user.dev-host.imbue.com".

    When the request comes from a different host (e.g. 127.0.0.1 or localhost),
    returns None so the cookie is scoped to the request host — otherwise the
    browser would reject the Set-Cookie (domain mismatch).
    """
    zone = get_config().zone_domain
    if not zone:
        return None
    zone_no_port = zone.split(":")[0]
    if request_host:
        host_no_port = request_host.split(":")[0]
        if host_no_port != zone_no_port and not host_no_port.endswith("." + zone_no_port):
            return None
    return zone_no_port


def set_auth_cookies(
    response: Response,
    access_token: str,
    refresh_token: str | None = None,
    request: Request | None = None,
) -> Response:
    """Set zone_auth (and optionally zone_refresh) cookies."""
    request_host = request.host if request else None
    domain = _cookie_domain(request_host)
    response.set_cookie(
        COOKIE_ACCESS,
        access_token,
        path="/",
        domain=domain,
        httponly=True,
        secure=get_config().tls_enabled,
        samesite="Lax",
        max_age=ACCESS_TOKEN_EXPIRY + int(REFRESH_GRACE_PERIOD.total_seconds()),
    )
    if refresh_token:
        response.set_cookie(
            COOKIE_REFRESH,
            refresh_token,
            path="/",
            domain=domain,
            httponly=True,
            secure=get_config().tls_enabled,
            samesite="Lax",
            max_age=REFRESH_TOKEN_EXPIRY,
        )
    return response


def clear_auth_cookies(response: Response, request: Request | None = None) -> Response:
    """Delete auth cookies.

    Clears cookies both with the computed domain and without, to handle stale
    cookies that were set with a different Domain attribute (e.g. after switching
    TLS mode or changing zone_domain).
    """
    request_host = request.host if request else None
    domain = _cookie_domain(request_host)
    response.delete_cookie(COOKIE_ACCESS, path="/", domain=domain)
    response.delete_cookie(COOKIE_REFRESH, path="/", domain=domain)
    # Also clear without explicit domain, in case cookies were set differently
    if domain is not None:
        response.delete_cookie(COOKIE_ACCESS, path="/")
        response.delete_cookie(COOKIE_REFRESH, path="/")
    return response


def _validate_api_token(token: str) -> dict[str, str] | None:
    """Validate a bearer token against the api_tokens table.

    Returns a claims dict (owner-level access) or None.
    """
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    db = get_db()
    row = db.execute(
        "SELECT name, expires_at FROM api_tokens WHERE token_hash = ?",
        (token_hash,),
    ).fetchone()
    if not row:
        return None
    if row["expires_at"] and datetime.fromisoformat(row["expires_at"]) < datetime.now(UTC):
        return None
    owner = db.execute("SELECT username FROM owner WHERE id = 1").fetchone()
    if not owner:
        return None
    return {"sub": owner["username"], "username": owner["username"]}


def resolve_app_from_token(token: str) -> str | None:
    """Look up a Bearer token in the app_tokens table, return the app name or None."""
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    db = get_db()
    row = db.execute("SELECT app_name FROM app_tokens WHERE token_hash = ?", (token_hash,)).fetchone()
    return row["app_name"] if row else None


def get_current_user_from_request(request: Request) -> dict[str, Any] | None:
    """Extract and verify identity from request cookies or Authorization header.

    Checks JWT cookie first, then falls back to Authorization: Bearer token.
    Returns claims dict or None.
    """
    # Warn on duplicate auth cookies — this happens when cookies were set with
    # different Domain attributes (e.g. after a config change). The browser
    # sends both, but only the first is read, which may be stale/invalid.
    cookie_header = request.headers.get("Cookie", "")
    dupes = cookie_header.count(f"{COOKIE_ACCESS}=")
    if dupes > 1:
        logger.warning(
            "Duplicate %s cookies detected (%d instances) for %s %s. "
            "This usually means cookies were set with different Domain attributes. "
            "The user should clear cookies to fix this.",
            COOKIE_ACCESS,
            dupes,
            request.method,
            request.path,
        )

    token = request.cookies.get(COOKIE_ACCESS)
    if token:
        claims = decode_access_token(token)
        if claims:
            return claims

    # Fall back to Authorization: Bearer (API tokens)
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        return _validate_api_token(auth_header.removeprefix("Bearer "))

    return None
