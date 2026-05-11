"""Here to avoid a circular import in migrations if we put this in apps.py"""

import secrets

# Bitcoin base58 alphabet: omits 0, O, I, l to avoid visual ambiguity.
_BASE58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_BASE58_SET = frozenset(_BASE58_ALPHABET)

APP_ID_LENGTH = 12


def _encode_base58_padded(n: int) -> str:
    chars = []
    for _ in range(APP_ID_LENGTH):
        n, rem = divmod(n, 58)
        chars.append(_BASE58_ALPHABET[rem])
    return "".join(reversed(chars))


def new_app_id() -> str:
    """Mint a fresh 12-char base58 app id (~70 bits of entropy)."""
    n = int.from_bytes(secrets.token_bytes(9), "big")
    return _encode_base58_padded(n)


def is_valid_app_id(s: str) -> bool:
    """True iff s is exactly APP_ID_LENGTH chars, all from the base58 alphabet."""
    return len(s) == APP_ID_LENGTH and all(c in _BASE58_SET for c in s)
