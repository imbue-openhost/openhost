import os
import secrets

# Bitcoin base58 alphabet: omits 0, O, I, l to avoid visual ambiguity.
_BASE58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_BASE58_SET = frozenset(_BASE58_ALPHABET)

APP_ID_LENGTH = 12

# When set, app_ids are minted from a monotonic counter instead of a CSPRNG so
# migration / snapshot tests can produce reproducible dumps. Production code
# never sets this — the migration writes opaque random ids in real deployments.
_DETERMINISTIC_ENV = "OPENHOST_DETERMINISTIC_APP_ID"
_deterministic_counter = 0


def _encode_base58_padded(n: int) -> str:
    chars = []
    for _ in range(APP_ID_LENGTH):
        n, rem = divmod(n, 58)
        chars.append(_BASE58_ALPHABET[rem])
    return "".join(reversed(chars))


def new_app_id() -> str:
    """Mint a fresh 12-char base58 app id (~70 bits of entropy)."""
    if os.environ.get(_DETERMINISTIC_ENV):
        global _deterministic_counter
        _deterministic_counter += 1
        # Pad into the full base58 range so the output stays 12 chars.
        return _encode_base58_padded(_deterministic_counter)
    n = int.from_bytes(secrets.token_bytes(9), "big")
    return _encode_base58_padded(n)


def is_valid_app_id(s: str) -> bool:
    """True iff s is exactly APP_ID_LENGTH chars, all from the base58 alphabet."""
    return len(s) == APP_ID_LENGTH and all(c in _BASE58_SET for c in s)


def _reset_deterministic_counter() -> None:
    """Test-only hook to reset the deterministic counter between runs."""
    global _deterministic_counter
    _deterministic_counter = 0
