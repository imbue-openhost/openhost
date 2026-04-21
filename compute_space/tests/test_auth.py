"""Unit tests for JWT access token iss/aud claims."""

from pathlib import Path
from unittest.mock import patch

import jwt as pyjwt
import pytest

from compute_space.config import DefaultConfig
from compute_space.core import auth


@pytest.fixture(autouse=True)
def _setup_keys(tmp_path: Path) -> None:
    """Load keys and set zone_domain config for each test."""
    auth.load_keys(str(tmp_path / "keys"))
    cfg = DefaultConfig(
        host="127.0.0.1",
        data_root_dir=str(tmp_path),
        zone_domain="myzone.example.com",
        tls_enabled=False,
        start_caddy=False,
    )
    cfg.make_all_dirs()
    with patch("compute_space.core.auth.get_config", return_value=cfg):
        yield


def _decode_raw(token: str) -> dict:
    """Decode without verification to inspect raw claims."""
    return pyjwt.decode(token, options={"verify_signature": False}, algorithms=["RS256"])


def test_access_token_contains_iss_and_aud(tmp_path: Path) -> None:
    cfg = DefaultConfig(
        host="127.0.0.1",
        data_root_dir=str(tmp_path),
        zone_domain="myzone.example.com",
        tls_enabled=False,
        start_caddy=False,
    )
    with patch("compute_space.core.auth.get_config", return_value=cfg):
        token = auth.create_access_token("alice")
    claims = _decode_raw(token)
    assert claims["iss"] == "myzone.example.com"
    assert claims["aud"] == "myzone.example.com"
    assert claims["sub"] == "alice"
    assert claims["username"] == "alice"


def test_decode_verifies_aud_and_iss(tmp_path: Path) -> None:
    cfg = DefaultConfig(
        host="127.0.0.1",
        data_root_dir=str(tmp_path),
        zone_domain="myzone.example.com",
        tls_enabled=False,
        start_caddy=False,
    )
    with patch("compute_space.core.auth.get_config", return_value=cfg):
        token = auth.create_access_token("alice")
        # Same zone decodes fine
        assert auth.decode_access_token(token) is not None

    # Different zone rejects token
    cfg_other = DefaultConfig(
        host="127.0.0.1",
        data_root_dir=str(tmp_path),
        zone_domain="other.example.com",
        tls_enabled=False,
        start_caddy=False,
    )
    with patch("compute_space.core.auth.get_config", return_value=cfg_other):
        assert auth.decode_access_token(token) is None


def test_decode_allow_expired_verifies_aud_and_iss(tmp_path: Path) -> None:
    cfg = DefaultConfig(
        host="127.0.0.1",
        data_root_dir=str(tmp_path),
        zone_domain="myzone.example.com",
        tls_enabled=False,
        start_caddy=False,
    )
    with patch("compute_space.core.auth.get_config", return_value=cfg):
        token = auth.create_access_token("alice")
        assert auth.decode_access_token_allow_expired(token) is not None

    cfg_other = DefaultConfig(
        host="127.0.0.1",
        data_root_dir=str(tmp_path),
        zone_domain="other.example.com",
        tls_enabled=False,
        start_caddy=False,
    )
    with patch("compute_space.core.auth.get_config", return_value=cfg_other):
        assert auth.decode_access_token_allow_expired(token) is None
