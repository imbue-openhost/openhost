import datetime
import enum
import threading
import time
from collections.abc import Callable
from pathlib import Path

from cryptography import x509

from compute_space.config import Config
from compute_space.core.logging import logger
from compute_space.core.tls.provision import provision_cert

# Renew well before expiry so transient ACME/DNS failures have days of retries left, not hours.
RENEW_BEFORE = datetime.timedelta(days=7)
CHECK_INTERVAL = datetime.timedelta(hours=12)
RETRY_INTERVAL = datetime.timedelta(hours=1)


class CertStatus(enum.Enum):
    MISSING = "missing"
    EXPIRED = "expired"
    EXPIRING_SOON = "expiring_soon"
    OK = "ok"


def get_cert_status(cert_path: Path, key_path: Path, now: datetime.datetime | None = None) -> CertStatus:
    if not cert_path.exists() or not key_path.exists():
        return CertStatus.MISSING
    try:
        cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
    except ValueError:
        # An unreadable cert can't be served; re-acquiring is the remedy, same as expired.
        logger.warning(f"Could not parse TLS cert at {cert_path}; treating it as expired")
        return CertStatus.EXPIRED
    if now is None:
        now = datetime.datetime.now(datetime.UTC)
    expires_at = cert.not_valid_after_utc
    if expires_at <= now:
        return CertStatus.EXPIRED
    if expires_at <= now + RENEW_BEFORE:
        return CertStatus.EXPIRING_SOON
    return CertStatus.OK


def renew_cert_if_needed(
    config: Config,
    restart_caddy: Callable[[], None],
    provision: Callable[[Config], None] = provision_cert,
) -> bool:
    """Renew the cert if it is missing, expired, or inside the renewal window.

    Returns True if a new cert was installed (and Caddy restarted to pick it up).
    """
    status = get_cert_status(config.tls_cert_path, config.tls_key_path)
    if status == CertStatus.OK:
        return False
    logger.info(f"TLS cert for {config.zone_domain} is {status.value}; renewing")
    provision(config)
    restart_caddy()
    logger.info(f"TLS cert for {config.zone_domain} renewed")
    return True


def start_renewal_thread(config: Config, restart_caddy: Callable[[], None]) -> threading.Thread:
    """Run renew_cert_if_needed periodically in a daemon thread, retrying sooner after failures."""

    def _loop() -> None:
        while True:
            interval = CHECK_INTERVAL
            try:
                renew_cert_if_needed(config, restart_caddy)
            except Exception:
                logger.exception(f"TLS cert renewal failed; retrying in {RETRY_INTERVAL}")
                interval = RETRY_INTERVAL
            time.sleep(interval.total_seconds())

    thread = threading.Thread(target=_loop, name="tls-cert-renewal", daemon=True)
    thread.start()
    return thread
