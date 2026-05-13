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

from compute_space.config import get_config
from compute_space.core.auth.cookies import ACCESS_TOKEN_EXPIRY
from compute_space.core.auth.cookies import COOKIE_ACCESS
from compute_space.core.auth.cookies import COOKIE_REFRESH
from compute_space.core.auth.cookies import REFRESH_GRACE_PERIOD
from compute_space.core.auth.cookies import REFRESH_TOKEN_EXPIRY
from compute_space.core.auth.cookies import _cookie_domain
from compute_space.core.auth.cookies import build_auth_cookies
from compute_space.core.auth.cookies import clear_auth_cookies_spec
from compute_space.core.auth.inputs import AuthInputs
from compute_space.core.logging import logger
from compute_space.core.util import write_restricted
from compute_space.db import get_db

# Re-exported for backward compatibility — external callers do
# ``from compute_space.core.auth import COOKIE_ACCESS`` etc.
__all__ = [
    "ACCESS_TOKEN_EXPIRY",
    "AuthInputs",
    "COOKIE_ACCESS",
    "COOKIE_REFRESH",
    "REFRESH_GRACE_PERIOD",
    "REFRESH_TOKEN_EXPIRY",
    "_cookie_domain",
    "build_auth_cookies",
    "clear_auth_cookies_spec",
    "create_access_token",
    "create_refresh_token",
    "decode_access_token",
    "decode_access_token_allow_expired",
    "get_current_user",
    "get_public_key_pem",
    "load_keys",
    "resolve_app_from_token",
]

_private_key: str | None = None
_public_key: str | None = None


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
    return get_config().zone_domain


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
    # TODO: give this a proper type?
    return {"sub": owner["username"], "username": owner["username"]}


def resolve_app_from_token(token: str) -> str | None:
    """Look up a Bearer token in the app_tokens table, return the app_id or None."""
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    db = get_db()
    row = db.execute("SELECT app_id FROM app_tokens WHERE token_hash = ?", (token_hash,)).fetchone()
    return row["app_id"] if row else None


def get_current_user(inputs: AuthInputs) -> dict[str, Any] | None:
    """Extract and verify identity from cookies or Authorization header.

    Checks JWT cookie first, then falls back to Authorization: Bearer token.
    Returns claims dict or None.

    TODO: return something with proper typing!
    """
    # Warn on duplicate auth cookies — this happens when cookies were set with
    # different Domain attributes (e.g. after a config change). The browser
    # sends both, but only the first is read, which may be stale/invalid.
    dupes = inputs.cookie_header.count(f"{COOKIE_ACCESS}=")
    if dupes > 1:
        logger.warning(
            "Duplicate %s cookies detected (%d instances) for %s %s. "
            "This usually means cookies were set with different Domain attributes. "
            "The user should clear cookies to fix this.",
            COOKIE_ACCESS,
            dupes,
            inputs.method,
            inputs.path,
        )

    token = inputs.cookies.get(COOKIE_ACCESS)
    if token:
        claims = decode_access_token(token)
        if claims:
            return claims

    # Fall back to Authorization: Bearer (API tokens)
    if inputs.auth_header.startswith("Bearer "):
        return _validate_api_token(inputs.auth_header.removeprefix("Bearer "))

    return None
