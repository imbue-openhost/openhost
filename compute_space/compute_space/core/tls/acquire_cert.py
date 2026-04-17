import asyncio
import os
from pathlib import Path

from compute_space.core.logging import logger
from compute_space.core.tls.util import _acquire_cert_dns01
from compute_space.core.tls.util import load_account_key

GTS_PRODUCTION = "https://dv.acme-v02.api.pki.goog/directory"


def check_if_cert_exists(cert_path: Path, key_path: Path) -> bool:
    if os.path.exists(cert_path) and os.path.exists(key_path):
        logger.info(f"Using existing TLS cert from {cert_path}")
        return True
    return False


async def acquire_tls_cert(
    domain: str,
    cert_path: Path,
    key_path: Path,
    acme_account_key_path: Path,
    coredns_zonefile_path: Path,
    directory_url: str | None = None,
    verify_ssl: bool = True,
) -> None:

    logger.info(f"Acquiring TLS certificate for {domain}...")

    account_key = load_account_key(acme_account_key_path)
    logger.info(f"Loaded ACME account key from {acme_account_key_path}")

    if directory_url is None:
        directory_url = GTS_PRODUCTION
    domains = [domain, f"*.{domain}"]
    logger.info(f"Requesting wildcard TLS cert for {domains} from {directory_url} (DNS-01)")
    cert_pem, key_pem = await asyncio.to_thread(
        _acquire_cert_dns01,
        domains=domains,
        directory_url=directory_url,
        coredns_zonefile_path=coredns_zonefile_path,
        account_key=account_key,
        verify_ssl=verify_ssl,
    )

    logger.info(f"TLS cert acquired for {domain}, writing to {cert_path} and {key_path}")

    with open(cert_path, "wb") as f:
        f.write(cert_pem)
    with open(key_path, "wb") as f:
        f.write(key_pem)
    os.chmod(key_path, 0o600)
