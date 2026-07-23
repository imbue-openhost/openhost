"""Persistence + active-config wiring for domains added at runtime via /api/domains.

Runtime-added domains live in a small router-owned JSON file (``runtime_domains.json``) under
the data dir — mirroring the existing ``default_apps.json`` sentinel pattern — NOT in config.toml
(which provisioning owns) and NOT in the DB (this is a tiny, rarely-changed, single-owner list,
not relational data).  The effective domain set the router serves is the config-file/synthesized
set (the "base") plus these runtime records, merged into the active config so routing, Caddy, and
URL-building all see runtime additions with no per-request I/O.
"""

from __future__ import annotations

import json
import os

import attr

from compute_space.config import Config
from compute_space.config import Domain
from compute_space.config import set_active_config
from compute_space.core.logging import logger

# Per-domain cert/acquisition status surfaced by /api/domains.
CERT_STATUS_NONE = "none"  # TLS domain with no cert yet acquired
CERT_STATUS_ACQUIRING = "acquiring"  # acquisition in flight (served via `tls internal` meanwhile)
CERT_STATUS_ACTIVE = "active"  # cert in place (or non-TLS domain — nothing to acquire, serving http)
CERT_STATUS_ERROR = "error"  # acquisition failed (see error_message)


@attr.s(auto_attribs=True, frozen=True)
class DomainRecord:
    """A runtime-added domain persisted in ``runtime_domains.json``: the Domain fields plus the
    cert-acquisition status shown in the dashboard/API."""

    name: str
    tls: bool
    mdns: bool
    cert_status: str = CERT_STATUS_NONE
    error_message: str | None = None

    def to_domain(self) -> Domain:
        return Domain(name=self.name, tls=self.tls, mdns=self.mdns)


def load_records(config: Config) -> tuple[DomainRecord, ...]:
    path = config.runtime_domains_path
    if not path.exists():
        return ()
    try:
        raw = json.loads(path.read_text() or "[]")
        return tuple(
            DomainRecord(
                name=str(e["name"]).lower(),
                tls=bool(e.get("tls", False)),
                mdns=bool(e.get("mdns", False)),
                cert_status=str(e.get("cert_status", CERT_STATUS_NONE)),
                error_message=e.get("error_message"),
            )
            for e in raw
        )
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        # A corrupt/partial file must not brick startup (rebuild_active_domains runs at boot) or
        # every /api/domains call.  Ignore the runtime additions and keep serving the base/primary
        # domains from config.toml; log loudly so the operator can repair or delete the file.
        logger.error("Ignoring unreadable {} ({}); serving config-file domains only", path, exc)
        return ()


def save_records(config: Config, records: tuple[DomainRecord, ...]) -> None:
    path = config.runtime_domains_path
    path.parent.mkdir(parents=True, exist_ok=True)
    data = [attr.asdict(r) for r in records]
    # Atomic write (temp + rename) so a crash mid-write can't leave a corrupt file.
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    os.replace(tmp, path)


def get_record(config: Config, name: str) -> DomainRecord | None:
    name = name.lower()
    for r in load_records(config):
        if r.name == name:
            return r
    return None


def upsert_record(config: Config, record: DomainRecord) -> None:
    others = tuple(r for r in load_records(config) if r.name != record.name)
    save_records(config, others + (record,))


def remove_record(config: Config, name: str) -> bool:
    name = name.lower()
    records = load_records(config)
    kept = tuple(r for r in records if r.name != name)
    if len(kept) == len(records):
        return False
    save_records(config, kept)
    return True


def set_record_status(config: Config, name: str, cert_status: str, error_message: str | None = None) -> None:
    record = get_record(config, name)
    if record is None:
        return
    upsert_record(config, attr.evolve(record, cert_status=cert_status, error_message=error_message))


# --- effective domain set = base (config-file/synthesized) + runtime records ------------------

_base_domains: tuple[Domain, ...] = ()


def set_base_domains(domains: tuple[Domain, ...]) -> None:
    """Capture the config-file / synthesized domain set (primary first) once at startup, so
    runtime additions layer on top without re-reading the config file and the primary is never
    dropped."""
    global _base_domains
    _base_domains = domains


def _dedup_by_name(domains: tuple[Domain, ...]) -> tuple[Domain, ...]:
    seen: set[str] = set()
    out: list[Domain] = []
    for d in domains:
        if d.name_no_port in seen:
            continue
        seen.add(d.name_no_port)
        out.append(d)
    return tuple(out)


def effective_domains(config: Config) -> tuple[Domain, ...]:
    """Base domains followed by runtime records, de-duplicated by name (base wins, primary first)."""
    return _dedup_by_name(_base_domains + tuple(r.to_domain() for r in load_records(config)))


def rebuild_active_domains(config: Config) -> Config:
    """Recompute the effective domain set and swap it into the active config, so routing, Caddy
    generation, and URL-building immediately reflect runtime additions.  Returns the new config."""
    new_config = config.evolve(domains=effective_domains(config))
    set_active_config(new_config)
    return new_config
