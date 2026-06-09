"""Per-app real TLS certificate provisioning and injection.

Apps declare ``[[tls_certs]]`` in their manifest to request real,
ACME-issued certificates (as opposed to the self-signed pairs an app
would otherwise have to generate itself).  The motivating case is an XMPP
server: server-to-server federation is rejected by most peers when the
presented cert is self-signed, and the zone wildcard ``*.{zone}`` does
NOT cover second-level component hosts like ``conference.xmpp.{zone}``.

The router already owns the ACME account and the CoreDNS zone, so it can
issue any cert under the zone via DNS-01.  This module:

  * expands the ``{app}``/``{zone}`` placeholders in a manifest request,
  * enforces that every requested SAN lives under ``{app}.{zone}`` (an app
    can never obtain a cert for another app's hostname or the bare zone),
  * reuses the already-acquired zone wildcard cert when it covers every
    requested SAN (cheap, no new ACME order), otherwise issues a dedicated
    cert via DNS-01,
  * writes the cert/key under a router-owned per-app directory that is
    bind-mounted read-only into the container, and
  * decides when an existing cert is close enough to expiry to renew.

Cert acquisition is serialized process-wide via ``_ACME_LOCK`` because the
DNS-01 flow mutates a single shared ``_acme-challenge`` record set in the
zone file; two concurrent orders would clobber each other's TXT records.
"""

from __future__ import annotations

import datetime
import os
import threading
from pathlib import Path

import attr
from cryptography import x509

from compute_space.config import Config
from compute_space.core.logging import logger
from compute_space.core.manifest import AppManifest
from compute_space.core.manifest import TlsCertRequest
from compute_space.core.tls.acquire_cert import GTS_PRODUCTION
from compute_space.core.tls.util import _acquire_cert_dns01
from compute_space.core.tls.util import load_account_key

# Renew a cert once it is within this many days of its notAfter.  GTS/LE
# certs are ~90 days; 30 days of headroom is the conventional ACME margin.
RENEWAL_WINDOW_DAYS = 30

# Serialize ACME DNS-01 orders: they share the zone file's _acme-challenge
# TXT record set, so concurrent orders would corrupt each other.
_ACME_LOCK = threading.Lock()


@attr.s(auto_attribs=True, frozen=True)
class RenderedCertRequest:
    """A [[tls_certs]] request with placeholders resolved to concrete values."""

    label: str
    domains: list[str]
    # Path of the cert/key relative to the per-app cert dir (and, identically,
    # relative to the read-only /data/tls mount inside the container).
    cert_rel_path: str
    key_rel_path: str


def expand_template(value: str, app_name: str, zone: str) -> str:
    """Expand ``{app}``/``{zone}`` placeholders in a manifest template."""
    return value.replace("{app}", app_name).replace("{zone}", zone)


def _is_within_zone_subtree(domain: str, app_name: str, zone: str) -> bool:
    """True iff ``domain`` is ``{app}.{zone}`` or a subdomain of it.

    The bare zone and other apps' subdomains are intentionally excluded: an
    app may only obtain certs for hostnames rooted at its own app subdomain.
    """
    base = f"{app_name}.{zone}".lower()
    domain = domain.lower()
    return domain == base or domain.endswith("." + base)


def render_cert_request(req: TlsCertRequest, app_name: str, zone: str) -> RenderedCertRequest:
    """Resolve placeholders and validate scoping for a single request.

    Raises ``ValueError`` if any requested SAN falls outside ``{app}.{zone}``
    or renders to something that isn't a valid DNS name.
    """
    from compute_space.core.manifest import _DNS_NAME_RE  # noqa: PLC0415 — avoid import cycle

    domains: list[str] = []
    seen: set[str] = set()
    for raw in req.domains:
        d = expand_template(raw, app_name, zone).lower()
        if not _DNS_NAME_RE.match(d):
            raise ValueError(f"[[tls_certs]] '{req.label}' domain {raw!r} rendered to invalid DNS name {d!r}")
        if not _is_within_zone_subtree(d, app_name, zone):
            raise ValueError(
                f"[[tls_certs]] '{req.label}' domain {d!r} is outside this app's subtree "
                f"({app_name}.{zone}); apps may only request certs for their own subdomains"
            )
        if d not in seen:
            seen.add(d)
            domains.append(d)

    cert_rel = os.path.normpath(expand_template(req.cert_path, app_name, zone))
    key_rel = os.path.normpath(expand_template(req.key_path, app_name, zone))
    for rel in (cert_rel, key_rel):
        if os.path.isabs(rel) or rel.startswith(".."):
            raise ValueError(f"[[tls_certs]] '{req.label}' resolved to an out-of-bounds path {rel!r}")
    return RenderedCertRequest(label=req.label, domains=domains, cert_rel_path=cert_rel, key_rel_path=key_rel)


def cert_covered_by_wildcard(domains: list[str], zone: str) -> bool:
    """True iff the zone cert (``zone`` + ``*.zone``) covers every domain.

    ``*.zone`` matches exactly one label below the zone, so ``a.zone`` is
    covered but ``a.b.zone`` is not.
    """
    zone = zone.lower()
    for d in domains:
        d = d.lower()
        if d == zone:
            continue
        if d.endswith("." + zone):
            label = d[: -(len(zone) + 1)]
            if "." not in label and label:
                continue  # single-label subdomain -> covered by *.zone
        return False
    return True


def cert_present_and_current(
    cert_path: Path,
    key_path: Path,
    expected_domains: list[str],
    now: datetime.datetime | None = None,
) -> bool:
    """True iff a cert/key pair exists, covers all expected SANs, and is not near expiry.

    Returns False (so the caller re-provisions) if the cert is missing,
    unreadable, missing a required SAN, or within ``RENEWAL_WINDOW_DAYS`` of
    expiry.  Never raises on a malformed cert.
    """
    if not cert_path.exists() or not key_path.exists():
        return False
    try:
        cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
    except (ValueError, OSError) as exc:
        logger.warning("Existing app cert at %s is unreadable (%s); will re-provision", cert_path, exc)
        return False

    try:
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        present = {n.lower() for n in san.get_values_for_type(x509.DNSName)}
    except x509.ExtensionNotFound:
        present = set()
    for d in expected_domains:
        dl = d.lower()
        if dl in present:
            continue
        # A wildcard SAN (*.zone) covers single-label subdomains.
        covered = any(w.startswith("*.") and dl.endswith(w[1:]) and "." not in dl[: -(len(w) - 1)] for w in present)
        if not covered:
            logger.info("App cert at %s missing SAN %r; will re-provision", cert_path, d)
            return False

    now = now or datetime.datetime.now(datetime.UTC)
    not_after = cert.not_valid_after_utc
    if not_after - now <= datetime.timedelta(days=RENEWAL_WINDOW_DAYS):
        logger.info("App cert at %s expires %s (within renewal window); will renew", cert_path, not_after)
        return False
    return True


def _write_pair(cert_path: Path, key_path: Path, cert_pem: bytes, key_pem: bytes) -> None:
    cert_path.parent.mkdir(parents=True, exist_ok=True)
    key_path.parent.mkdir(parents=True, exist_ok=True)
    # Atomic-ish write: temp then replace, so a crash mid-write can't leave a
    # half-written cert that the presence/expiry check mistakes for valid.
    tmp_cert = cert_path.with_suffix(cert_path.suffix + ".partial")
    tmp_key = key_path.with_suffix(key_path.suffix + ".partial")
    tmp_cert.write_bytes(cert_pem)
    tmp_key.write_bytes(key_pem)
    os.chmod(tmp_key, 0o640)
    os.replace(tmp_cert, cert_path)
    os.replace(tmp_key, key_path)


def app_cert_dir(openhost_data_path: Path, app_name: str) -> Path:
    """Router-owned directory holding an app's provisioned certs.

    Bind-mounted read-only into the container at ``/data/tls``.
    """
    return Path(openhost_data_path) / "app_certs" / app_name


def provision_app_certs(
    app_name: str,
    requests: list[TlsCertRequest],
    zone: str,
    openhost_data_path: Path,
    wildcard_cert_path: Path,
    wildcard_key_path: Path,
    acme_account_key_path: Path | None,
    coredns_zonefile_path: Path,
    *,
    acme_email: str | None = None,
    acme_directory_url: str | None = None,
    coredns_enabled: bool = True,
    force: bool = False,
    verify_ssl: bool = True,
) -> list[RenderedCertRequest]:
    """Ensure every requested cert exists, is in scope, and is current.

    Returns the list of rendered requests (used by the caller to build the
    container's env vars and bind mount).  Reuses the zone wildcard cert when
    it covers all SANs; otherwise issues a dedicated cert via DNS-01.  Already
    valid, non-expiring certs are left untouched unless ``force`` is set.

    Raises ``ValueError`` for out-of-scope requests, ``RuntimeError`` if a
    cert must be issued but the prerequisites (CoreDNS, ACME key) are missing.
    """
    rendered: list[RenderedCertRequest] = []
    cert_dir = app_cert_dir(openhost_data_path, app_name)
    for req in requests:
        r = render_cert_request(req, app_name, zone)
        rendered.append(r)
        cert_path = cert_dir / r.cert_rel_path
        key_path = cert_dir / r.key_rel_path

        if not force and cert_present_and_current(cert_path, key_path, r.domains):
            logger.info("App %s cert '%s' is current; reusing", app_name, r.label)
            continue

        if cert_covered_by_wildcard(r.domains, zone):
            if wildcard_cert_path.exists() and wildcard_key_path.exists():
                logger.info("App %s cert '%s' covered by zone wildcard; copying wildcard cert", app_name, r.label)
                _write_pair(
                    cert_path,
                    key_path,
                    wildcard_cert_path.read_bytes(),
                    wildcard_key_path.read_bytes(),
                )
                continue
            logger.warning(
                "App %s cert '%s' is wildcard-covered but no zone wildcard cert exists; issuing dedicated cert",
                app_name,
                r.label,
            )

        if not coredns_enabled:
            raise RuntimeError(
                f"Cannot issue dedicated cert for app '{app_name}' (label '{r.label}'): CoreDNS is "
                f"disabled, so DNS-01 is unavailable.  Requested domains: {r.domains}"
            )
        if not acme_account_key_path:
            raise RuntimeError(
                f"Cannot issue dedicated cert for app '{app_name}' (label '{r.label}'): no ACME account key configured"
            )

        logger.info("Issuing dedicated TLS cert for app %s '%s': %s", app_name, r.label, r.domains)
        account_key = load_account_key(acme_account_key_path)
        directory_url = acme_directory_url or GTS_PRODUCTION
        with _ACME_LOCK:
            cert_pem, key_pem = _acquire_cert_dns01(
                domains=list(r.domains),
                directory_url=directory_url,
                coredns_zonefile_path=coredns_zonefile_path,
                account_key=account_key,
                acme_email=acme_email,
                verify_ssl=verify_ssl,
                zone_domain=zone,
            )
        _write_pair(cert_path, key_path, cert_pem, key_pem)
        logger.info("Wrote dedicated cert for app %s '%s' to %s", app_name, r.label, cert_path)

    return rendered


def provision_app_certs_for_deploy(
    app_name: str,
    manifest: AppManifest,
    config: Config,
    *,
    force: bool = False,
) -> tuple[str | None, list[RenderedCertRequest]]:
    """Provision an app's ``[[tls_certs]]`` from the live config.

    Convenience wrapper around :func:`provision_app_certs` that pulls the
    cert/zone/ACME settings out of ``config``.  Returns
    ``(host_cert_dir, rendered_requests)`` for :func:`run_container`; both are
    empty/``None`` when the app declares no certs.
    """
    if not manifest.tls_certs:
        return None, []
    zone = config.zone_domain_no_port
    rendered = provision_app_certs(
        app_name=app_name,
        requests=manifest.tls_certs,
        zone=zone,
        openhost_data_path=config.openhost_data_path,
        wildcard_cert_path=config.tls_cert_path,
        wildcard_key_path=config.tls_key_path,
        acme_account_key_path=Path(config.acme_account_key_path) if config.acme_account_key_path else None,
        coredns_zonefile_path=config.coredns_zonefile_path,
        acme_email=config.acme_email,
        acme_directory_url=config.acme_directory_url,
        coredns_enabled=config.coredns_enabled,
        force=force,
    )
    return str(app_cert_dir(config.openhost_data_path, app_name)), rendered
