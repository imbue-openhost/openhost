"""Unit tests for JWT access token iss/aud claims."""

from pathlib import Path
from unittest.mock import patch

import jwt as pyjwt
import pytest

from compute_space.config import DefaultConfig
from compute_space.core.auth.jwt_tokens import create_access_token
from compute_space.core.auth.jwt_tokens import decode_access_token
from compute_space.core.auth.jwt_tokens import decode_access_token_allow_expired
from compute_space.core.auth.keys import load_keys

# Tests use this as the zone_domain; update _setup_keys if changed.
ZONE = "myzone.example.com"
OTHER_ZONE = "other.example.com"


def _make_cfg(tmp_path: Path, zone_domain: str = ZONE) -> DefaultConfig:
    return DefaultConfig(
        host="127.0.0.1",
        data_root_dir=str(tmp_path),
        zone_domain=zone_domain,
        tls_enabled=False,
        start_caddy=False,
    )


@pytest.fixture(autouse=True)
def _setup_keys(tmp_path: Path) -> None:
    """Load keys and patch get_config for each test."""
    load_keys(str(tmp_path / "keys"))
    cfg = _make_cfg(tmp_path)
    cfg.make_all_dirs()
    with patch("compute_space.core.auth.jwt_tokens.get_config", return_value=cfg):
        yield


def test_access_token_contains_iss_and_aud() -> None:
    token = create_access_token("alice")
    claims = pyjwt.decode(token, options={"verify_signature": False}, algorithms=["RS256"])
    assert claims["iss"] == ZONE
    assert claims["aud"] == ZONE
    assert claims["sub"] == "alice"
    assert claims["username"] == "alice"


def test_decode_verifies_aud_and_iss(tmp_path: Path) -> None:
    token = create_access_token("alice")
    assert decode_access_token(token) is not None

    # Different zone rejects token
    with patch("compute_space.core.auth.jwt_tokens.get_config", return_value=_make_cfg(tmp_path, OTHER_ZONE)):
        assert decode_access_token(token) is None


def test_decode_allow_expired_verifies_aud_and_iss(tmp_path: Path) -> None:
    token = create_access_token("alice")
    assert decode_access_token_allow_expired(token) is not None

    with patch("compute_space.core.auth.jwt_tokens.get_config", return_value=_make_cfg(tmp_path, OTHER_ZONE)):
        assert decode_access_token_allow_expired(token) is None
