"""RSA keypair management for zone auth (signs/verifies zone_auth JWTs).

Keys are ephemeral — regenerated on each fresh keys_dir. See identity.py for
the persistent identity keypair, which is a separate concern.
"""

from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from compute_space.core.util import write_restricted

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


def get_private_key_pem() -> str:
    """Return the PEM-encoded private key string."""
    if _private_key is None:
        raise RuntimeError("Keys not loaded. Call load_keys() first.")
    return _private_key


def get_public_key_pem() -> str:
    """Return the PEM-encoded public key string."""
    if _public_key is None:
        raise RuntimeError("Keys not loaded. Call load_keys() first.")
    return _public_key
