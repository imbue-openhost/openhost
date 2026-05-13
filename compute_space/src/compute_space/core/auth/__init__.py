"""JWT authentication for the OpenHost router.

The router always generates/loads its own RSA 2048-bit keypair and signs/verifies
JWTs locally.

This package is organized as:
- keys.py                 — RSA keypair load/generate, public-key accessor
- tokens.py               — JWT access tokens, refresh tokens
- cookies.py              — zone_auth/zone_refresh cookie helpers
- identity.py             — federated identity provider + request-based user/app resolution
- permissions.py          — v1 owner-granted app permissions
- permissions_v2.py       — v2 permission grants (per-service scopes)
- service_access_rules.py — service access rule evaluation
- security.py             — host security checks (sshd, listening ports, audit)
"""

from compute_space.core.auth.cookies import COOKIE_ACCESS
from compute_space.core.auth.cookies import COOKIE_REFRESH
from compute_space.core.auth.cookies import clear_auth_cookies
from compute_space.core.auth.cookies import set_auth_cookies
from compute_space.core.auth.identity import get_current_user_from_request
from compute_space.core.auth.identity import resolve_app_from_token
from compute_space.core.auth.keys import get_public_key_pem
from compute_space.core.auth.keys import load_keys
from compute_space.core.auth.tokens import ACCESS_TOKEN_EXPIRY
from compute_space.core.auth.tokens import REFRESH_GRACE_PERIOD
from compute_space.core.auth.tokens import REFRESH_TOKEN_EXPIRY
from compute_space.core.auth.tokens import create_access_token
from compute_space.core.auth.tokens import create_refresh_token
from compute_space.core.auth.tokens import decode_access_token
from compute_space.core.auth.tokens import decode_access_token_allow_expired

__all__ = [
    "ACCESS_TOKEN_EXPIRY",
    "COOKIE_ACCESS",
    "COOKIE_REFRESH",
    "REFRESH_GRACE_PERIOD",
    "REFRESH_TOKEN_EXPIRY",
    "clear_auth_cookies",
    "create_access_token",
    "create_refresh_token",
    "decode_access_token",
    "decode_access_token_allow_expired",
    "get_current_user_from_request",
    "get_public_key_pem",
    "load_keys",
    "resolve_app_from_token",
    "set_auth_cookies",
]
