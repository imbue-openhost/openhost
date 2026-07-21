"""Phase 3: the generated Caddyfile serves each configured domain on its own terms
— https (with the acquired cert or Caddy's internal CA) for TLS domains, plain http
with no redirect for mDNS `.local` domains — so http `.local` and https external run
at once.  Where the `caddy` binary is available we adapt the output to prove it's
syntactically valid, not just string-matched."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from compute_space.config import Domain
from compute_space.core.caddy import generate_caddyfile

PUBLIC = Domain("host.example.com", tls=True)
PUBLIC2 = Domain("host.example.org", tls=True)
LOCAL = Domain("myhost.local", tls=False, mdns=True)
CERT = Path("/data/cert.pem")
KEY = Path("/data/key.pem")


def _cert_for(cert_domain: str | None):  # type: ignore[no-untyped-def]
    """Resolver that hands out the file cert for `cert_domain` only (mimics the primary having an
    acquired cert while other domains don't yet)."""

    def resolve(name: str):  # type: ignore[no-untyped-def]
        return (CERT, KEY) if name == cert_domain else None

    return resolve


def _gen(domains: tuple[Domain, ...], cert_domain: str | None = "host.example.com") -> str:
    return generate_caddyfile(domains, 8080, _cert_for(cert_domain))


def test_primary_tls_domain_uses_file_cert() -> None:
    cf = _gen((PUBLIC,))
    assert "https://host.example.com, https://*.host.example.com {" in cf
    assert f"tls {CERT} {KEY}" in cf
    assert "reverse_proxy localhost:8080" in cf


def test_tls_domain_redirect_is_scoped_not_global() -> None:
    cf = _gen((PUBLIC,))
    # per-domain http site, not a bare `:80 {` catch-all
    assert ":80 {" not in cf
    assert "http://host.example.com, http://*.host.example.com {" in cf
    assert "redir https://{host}{uri} permanent" in cf


def test_local_domain_served_plain_http_without_redirect() -> None:
    cf = _gen((PUBLIC, LOCAL))
    assert "http://myhost.local, http://*.myhost.local {" in cf
    # the .local http block reverse-proxies and does NOT redirect to https
    local_block = cf.split("http://myhost.local")[1].split("}")[0]
    assert "reverse_proxy localhost:8080" in local_block
    assert "redir" not in local_block


def test_second_public_domain_uses_internal_ca() -> None:
    cf = _gen((PUBLIC, PUBLIC2))
    # only the primary (cert_domain) gets the file cert; the extra domain self-signs
    assert f"tls {CERT} {KEY}" in cf
    assert "tls internal" in cf
    assert "https://host.example.org, https://*.host.example.org {" in cf


def test_auto_https_disable_redirects_when_any_tls() -> None:
    # `disable_redirects` (not `off`) so `tls internal` can still issue
    assert "auto_https disable_redirects" in _gen((PUBLIC, LOCAL))


def test_auto_https_off_when_no_tls_domain() -> None:
    assert "auto_https off" in _gen((LOCAL,), cert_domain=None)


# --- validate with the real caddy binary where present ----------------------------

_caddy = shutil.which("caddy")


@pytest.mark.skipif(_caddy is None, reason="caddy binary not on PATH")
@pytest.mark.parametrize(
    "domains,cert_domain",
    [
        ((PUBLIC,), "host.example.com"),
        ((PUBLIC, LOCAL), "host.example.com"),
        ((PUBLIC, PUBLIC2), "host.example.com"),
        ((LOCAL,), None),
    ],
)
def test_generated_caddyfile_is_valid(tmp_path: Path, domains: tuple[Domain, ...], cert_domain: str | None) -> None:
    cf = generate_caddyfile(domains, 8080, _cert_for(cert_domain))
    path = tmp_path / "Caddyfile"
    path.write_text(cf)
    result = subprocess.run(
        [_caddy, "adapt", "--config", str(path), "--adapter", "caddyfile"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"caddy rejected the config:\n{result.stderr}\n---\n{cf}"
