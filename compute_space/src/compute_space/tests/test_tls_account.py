from __future__ import annotations

from pathlib import Path

import pytest

from compute_space.config import CERT_PROVIDER_BYO
from compute_space.config import CERT_PROVIDER_EAB_MINT
from compute_space.core.tls.account import _generate_account_key
from compute_space.core.tls.account import ensure_account_key
from compute_space.core.tls.account import persist_account_key
from compute_space.core.tls.acquire_cert import _assert_domains_within_zone
from compute_space.core.tls.cert_api_client import EABCredential
from compute_space.core.tls.cert_api_client import mint_eab
from compute_space.core.tls.util import load_account_key

# ---------------------------------------------------------------------------
# ensure_account_key — provider-mode selection (no network)
# ---------------------------------------------------------------------------


def _ensure(mode: str, account_key_path: Path, **overrides: object) -> object:
    kwargs: dict[str, object] = {
        "mode": mode,
        "account_key_path": account_key_path,
        "directory_url": "https://acme.example.test/dir",
        "cert_api_url": "https://cert-api.example.test",
        "cert_api_token": None,
        "zone_domain": "host.example.com",
    }
    kwargs.update(overrides)
    return ensure_account_key(**kwargs)  # type: ignore[arg-type]


def test_persisted_key_is_reused_without_minting(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A persisted key is loaded directly — the cert-api is never contacted (renewal path)."""
    key_path = tmp_path / "acme-account-key.json"
    original = _generate_account_key()
    persist_account_key(original, key_path)

    def _boom(*args: object, **kwargs: object) -> object:
        raise AssertionError("mint_eab must not be called when an account key is already persisted")

    monkeypatch.setattr("compute_space.core.tls.account.mint_eab", _boom)

    loaded = _ensure(CERT_PROVIDER_EAB_MINT, key_path)
    assert loaded.to_json() == original.to_json()


def test_byo_missing_key_raises(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="byo"):
        _ensure(CERT_PROVIDER_BYO, tmp_path / "missing.json", cert_api_url=None)


def test_eab_mint_requires_cert_api_url(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="cert_api_url"):
        _ensure(CERT_PROVIDER_EAB_MINT, tmp_path / "missing.json", cert_api_url=None)


def test_unknown_mode_raises(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="Unknown cert_provider"):
        _ensure("bogus", tmp_path / "missing.json")


def test_persist_account_key_round_trip_and_perms(tmp_path: Path) -> None:
    key_path = tmp_path / "nested" / "acme-account-key.json"
    original = _generate_account_key()
    persist_account_key(original, key_path)

    assert key_path.exists()
    assert oct(key_path.stat().st_mode & 0o777) == "0o600"
    assert load_account_key(key_path).to_json() == original.to_json()


# ---------------------------------------------------------------------------
# mint_eab — cert-api seam (httpx stubbed)
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, payload: object) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> object:
        return self._payload


def test_mint_eab_parses_response_and_sends_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_post(url: str, *, json: object, headers: dict[str, str], timeout: float, verify: bool) -> _FakeResponse:
        captured["url"] = url
        captured["json"] = json
        captured["headers"] = headers
        return _FakeResponse({"kid": "kid-1", "hmac_key": "bWFjLWtleQ"})

    monkeypatch.setattr("compute_space.core.tls.cert_api_client.httpx.post", fake_post)

    cred = mint_eab("https://cert-api.example.test/", "host.example.com", token="secret-token")

    assert cred == EABCredential(kid="kid-1", hmac_key="bWFjLWtleQ")
    assert captured["url"] == "https://cert-api.example.test/eab"
    assert captured["json"] == {"zone_domain": "host.example.com"}
    assert captured["headers"] == {"Authorization": "Bearer secret-token"}


def test_mint_eab_omits_auth_header_without_token(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_post(url: str, *, json: object, headers: dict[str, str], timeout: float, verify: bool) -> _FakeResponse:
        captured["headers"] = headers
        return _FakeResponse({"kid": "kid-1", "hmac_key": "bWFjLWtleQ"})

    monkeypatch.setattr("compute_space.core.tls.cert_api_client.httpx.post", fake_post)

    mint_eab("https://cert-api.example.test", "host.example.com")
    assert captured["headers"] == {}


def test_mint_eab_missing_fields_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "compute_space.core.tls.cert_api_client.httpx.post",
        lambda url, **kwargs: _FakeResponse({"kid": "only-kid"}),
    )
    with pytest.raises(RuntimeError, match="missing expected fields"):
        mint_eab("https://cert-api.example.test", "host.example.com")


# ---------------------------------------------------------------------------
# _assert_domains_within_zone — defense-in-depth guard
# ---------------------------------------------------------------------------


def test_zone_guard_allows_own_zone_and_wildcard() -> None:
    _assert_domains_within_zone(["host.example.com", "*.host.example.com"], "host.example.com")
    _assert_domains_within_zone(["app.host.example.com"], "host.example.com")


@pytest.mark.parametrize("bad", ["evil.com", "*.evil.com", "host.example.com.evil.com", "nothost.example.com"])
def test_zone_guard_blocks_foreign_domains(bad: str) -> None:
    with pytest.raises(RuntimeError, match="outside this instance's zone"):
        _assert_domains_within_zone([bad], "host.example.com")
