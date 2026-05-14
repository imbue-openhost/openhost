"""JWT access tokens and opaque refresh tokens for zone auth."""

import secrets
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from typing import Any

import jwt

from compute_space.config import get_config
from compute_space.core.auth import keys

ACCESS_TOKEN_EXPIRY = 3600  # 60 minutes
REFRESH_TOKEN_EXPIRY = 2592000  # 30 days

REFRESH_GRACE_PERIOD = timedelta(hours=2)


def _zone_audience() -> str:
    """Return the audience/issuer identifier for this zone."""
    return get_config().zone_domain


def create_access_token(username: str) -> str:
    """Create a signed RS256 JWT."""
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
    return jwt.encode(payload, keys.get_private_key_pem(), algorithm="RS256")


def create_refresh_token() -> str:
    """Generate an opaque refresh token string."""
    return secrets.token_urlsafe(48)


def decode_access_token(token: str) -> dict[str, Any] | None:
    """Verify and decode a JWT. Returns claims dict or None."""
    try:
        zone = _zone_audience()
        return jwt.decode(token, keys.get_public_key_pem(), algorithms=["RS256"], audience=zone, issuer=zone)
    except jwt.InvalidTokenError:
        return None


def decode_access_token_allow_expired(token: str) -> dict[str, Any] | None:
    """Decode a JWT allowing up to REFRESH_GRACE_PERIOD past expiry -- used during token refresh."""
    try:
        zone = _zone_audience()
        return jwt.decode(
            token,
            keys.get_public_key_pem(),
            algorithms=["RS256"],
            audience=zone,
            issuer=zone,
            leeway=REFRESH_GRACE_PERIOD,
        )
    except jwt.InvalidTokenError:
        return None
