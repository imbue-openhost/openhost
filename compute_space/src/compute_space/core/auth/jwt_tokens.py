"""JWT access tokens and opaque refresh tokens for zone auth."""

import secrets
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from typing import Any

import jwt

from compute_space.config import get_config
from compute_space.core.auth import keys
from compute_space.core.auth.auth import AuthenticatedUser

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


def validate_jwt_access_token(token: str) -> AuthenticatedUser | None:
    """Verify and decode a JWT. Returns AuthenticatedUser if valid; None if not."""
    try:
        zone = _zone_audience()
        claims = jwt.decode(token, keys.get_public_key_pem(), algorithms=["RS256"], audience=zone, issuer=zone)
        # TODO: we should have a stable identifier for users, not a renamable "username".
        # the convention is that claims["sub"] is a stable id, and claims["username"] is something mutable.
        return AuthenticatedUser(username=claims["sub"])
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


def validate_jwt_refresh_token(access_token: str, refresh_token: str) -> AuthenticatedUser | None:
    """Authenticate by validating the refresh-token cookie + the (allowed-expired) JWT cookie.

    Pure check: no side effects. ``AuthRefreshMiddleware`` separately decides whether to mint a
    fresh access cookie based on the same conditions.
    """
    if not (refresh_tok := connection.cookies.get(COOKIE_REFRESH)):
        return None
    if not (expired_jwt := connection.cookies.get(COOKIE_ACCESS)):
        return None
    if (expired_claims := decode_access_token_allow_expired(expired_jwt)) is None:
        return None

    refresh_tok_hash = hashlib.sha256(refresh_tok.encode()).hexdigest()
    rt = db.execute(
        "SELECT expires_at FROM refresh_tokens WHERE token_hash = ? AND revoked = 0",
        (refresh_tok_hash,),
    ).fetchone()
    if rt is None or datetime.fromisoformat(rt["expires_at"]) < datetime.now(UTC):
        return None

    return AuthenticatedUser(username=expired_claims["sub"])
