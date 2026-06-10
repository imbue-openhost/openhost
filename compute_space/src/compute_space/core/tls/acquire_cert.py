import asyncio
import os
from pathlib import Path

from compute_space.config import CERT_PROVIDER_BYO
from compute_space.core.logging import logger
from compute_space.core.tls.account import ensure_account_key
from compute_space.core.tls.util import _acquire_cert_dns01

GTS_PRODUCTION = "https://dv.acme-v02.api.pki.goog/directory"


def check_if_cert_exists(cert_path: Path, key_path: Path) -> bool:
    if os.path.exists(cert_path) and os.path.exists(key_path):
        logger.info(f"Using existing TLS cert from {cert_path}")
        return True
    return False


def _assert_domains_within_zone(domains: list[str], zone_domain: str) -> None:
    """Defense-in-depth: refuse to request certs for anything outside our own zone.

    The trust boundary is DNS delegation — this instance is authoritative ONLY
    for its own delegated zone (served by its CoreDNS), so it must only ever
    order certs for ``zone_domain`` and its subdomains.  This guards against a
    future change accidentally requesting a domain this instance can't validate.
    """
    for d in domains:
        base = d[2:] if d.startswith("*.") else d
        if base != zone_domain and not base.endswith(f".{zone_domain}"):
            raise RuntimeError(f"Refusing to request cert for {d!r}: outside this instance's zone {zone_domain!r}")


async def acquire_tls_cert(
    domain: str,
    cert_path: Path,
    key_path: Path,
    acme_account_key_path: Path,
    coredns_zonefile_path: Path,
    cert_provider: str = CERT_PROVIDER_BYO,
    cert_api_url: str | None = None,
    cert_api_token: str | None = None,
    directory_url: str | None = None,
    verify_ssl: bool = True,
    acme_email: str | None = None,
) -> None:

    logger.info(f"Acquiring TLS certificate for {domain} (cert_provider={cert_provider})...")

    if directory_url is None:
        directory_url = GTS_PRODUCTION

    # Resolve the ACME account key.  This is the only step that differs between
    # provider modes (eab_mint mints+registers a new account; byo/renewal loads
    # the persisted key); the DNS-01 issuance below is shared.  In eab_mint mode
    # the cert-api also dictates the directory, so issue against the one the
    # account was created with.
    resolved = await asyncio.to_thread(
        ensure_account_key,
        mode=cert_provider,
        account_key_path=acme_account_key_path,
        directory_url=directory_url,
        cert_api_url=cert_api_url,
        cert_api_token=cert_api_token,
        zone_domain=domain,
        acme_email=acme_email,
        verify_ssl=verify_ssl,
    )

    domains = [domain, f"*.{domain}"]
    _assert_domains_within_zone(domains, domain)
    logger.info(f"Requesting wildcard TLS cert for {domains} from {resolved.directory_url} (DNS-01)")
    cert_pem, key_pem = await asyncio.to_thread(
        _acquire_cert_dns01,
        domains=domains,
        directory_url=resolved.directory_url,
        coredns_zonefile_path=coredns_zonefile_path,
        account_key=resolved.account_key,
        verify_ssl=verify_ssl,
        acme_email=acme_email,
    )

    logger.info(f"TLS cert acquired for {domain}, writing to {cert_path} and {key_path}")

    with open(cert_path, "wb") as f:
        f.write(cert_pem)
    with open(key_path, "wb") as f:
        f.write(key_pem)
    os.chmod(key_path, 0o600)
