"""Phase 3b: runtime domain persistence (runtime_domains.json) + effective-set merge, and the
per-domain `ensure_cert_for` acquisition wrapper.  No real ACME here — acquisition is stubbed;
these lock in the store/merge/cert-path logic that the /api/domains endpoint builds on."""

from __future__ import annotations

from pathlib import Path

from compute_space.config import Domain
from compute_space.config import get_config
from compute_space.core import domain_store
from compute_space.core.domain_store import CERT_STATUS_ACQUIRING
from compute_space.core.domain_store import CERT_STATUS_ACTIVE
from compute_space.core.domain_store import DomainRecord
from compute_space.core.domain_store import effective_domains
from compute_space.core.domain_store import load_records
from compute_space.core.domain_store import rebuild_active_domains
from compute_space.core.domain_store import remove_record
from compute_space.core.domain_store import set_base_domains
from compute_space.core.domain_store import set_record_status
from compute_space.core.domain_store import upsert_record
from compute_space.core.tls import domain_certs
from compute_space.tests.conftest import _make_test_config

PRIMARY = Domain("host.example.com", tls=True)


def _cfg(tmp_path: Path):  # type: ignore[no-untyped-def]
    return _make_test_config(tmp_path, zone_domain="host.example.com", tls_enabled=True, domains=(PRIMARY,))


# --- persistence round-trip -------------------------------------------------------


def test_records_round_trip_through_json_file(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    assert load_records(cfg) == ()
    upsert_record(cfg, DomainRecord("myhost.local", tls=False, mdns=True, cert_status=CERT_STATUS_ACTIVE))
    upsert_record(cfg, DomainRecord("host.example.org", tls=True, mdns=False, cert_status=CERT_STATUS_ACQUIRING))
    names = {r.name for r in load_records(cfg)}
    assert names == {"myhost.local", "host.example.org"}
    assert cfg.runtime_domains_path.exists()


def test_load_records_tolerates_corrupt_json(tmp_path: Path) -> None:
    # A corrupt/partial runtime_domains.json must not raise — rebuild_active_domains runs at boot
    # and on every /api/domains call, so a raise here would brick the router.  Fail safe to ().
    cfg = _cfg(tmp_path)
    cfg.runtime_domains_path.parent.mkdir(parents=True, exist_ok=True)
    cfg.runtime_domains_path.write_text('[{"name": "ok.example.com"}, {"nope": 1}]  <<garbage')
    assert load_records(cfg) == ()
    # The effective set still serves the base/primary domain rather than crashing.
    set_base_domains((PRIMARY,))
    assert effective_domains(cfg) == (PRIMARY,)


def test_upsert_replaces_same_name(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    upsert_record(cfg, DomainRecord("host.example.org", tls=True, mdns=False))
    set_record_status(cfg, "host.example.org", CERT_STATUS_ACTIVE)
    recs = load_records(cfg)
    assert len(recs) == 1 and recs[0].cert_status == CERT_STATUS_ACTIVE


def test_remove_record(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    upsert_record(cfg, DomainRecord("host.example.org", tls=True, mdns=False))
    assert remove_record(cfg, "host.example.org") is True
    assert remove_record(cfg, "host.example.org") is False
    assert load_records(cfg) == ()


# --- effective-set merge (base + runtime), dedup, primary first -------------------


def test_effective_domains_merges_base_and_runtime(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    set_base_domains(cfg.all_domains)  # (PRIMARY,)
    upsert_record(cfg, DomainRecord("myhost.local", tls=False, mdns=True))
    eff = effective_domains(cfg)
    assert [d.name for d in eff] == ["host.example.com", "myhost.local"]  # primary first


def test_rebuild_active_domains_swaps_active_config(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    set_base_domains(cfg.all_domains)
    upsert_record(cfg, DomainRecord("myhost.local", tls=False, mdns=True))
    rebuild_active_domains(cfg)
    # routing now resolves the runtime .local domain
    active = get_config()
    assert active.match_domain("app.myhost.local") is not None
    assert active.match_domain("app.myhost.local").mdns is True


def test_base_dedup_keeps_primary_once(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    set_base_domains(cfg.all_domains)
    # a runtime record duplicating the primary name must not double it
    upsert_record(cfg, DomainRecord("host.example.com", tls=True, mdns=False))
    eff = effective_domains(cfg)
    assert [d.name for d in eff] == ["host.example.com"]


# --- ensure_cert_for: no-op for mDNS, acquires for TLS ----------------------------


def test_ensure_cert_for_noop_on_mdns(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    called = []
    monkeypatch.setattr(domain_certs, "acquire_cert_for_domain", lambda *a, **k: called.append(a))
    domain_certs.ensure_cert_for(_cfg(tmp_path), Domain("myhost.local", tls=False, mdns=True))
    assert called == []  # mDNS never touches ACME


def test_ensure_cert_for_acquires_tls_to_per_domain_path(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    captured = {}

    def fake_acquire(config, domain, cert_path, key_path):  # type: ignore[no-untyped-def]
        captured["domain"] = domain
        captured["cert_path"] = cert_path

    monkeypatch.setattr(domain_certs, "acquire_cert_for_domain", fake_acquire)
    cfg = _cfg(tmp_path)
    domain_certs.ensure_cert_for(cfg, Domain("host.example.org", tls=True))
    assert captured["domain"] == "host.example.org"
    # per-domain path under certs/, NOT the primary's legacy cert file
    assert captured["cert_path"] == cfg.certs_dir / "host.example.org.pem"
    assert captured["cert_path"].parent.exists()  # ensure_cert_for created certs/


def _reset_base() -> None:
    domain_store.set_base_domains(())
