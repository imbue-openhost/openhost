"""Unit tests for per-app TLS cert provisioning/injection (no network).

The ACME DNS-01 issuance path is exercised by the requires_tls integration
suite; these tests cover the placeholder expansion, scope validation,
wildcard-coverage logic, expiry gating, and the wildcard-reuse provisioning
path (which never touches the network).
"""

import datetime

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from compute_space.core.manifest import TlsCertRequest
from compute_space.core.tls.app_certs import cert_covered_by_wildcard
from compute_space.core.tls.app_certs import cert_present_and_current
from compute_space.core.tls.app_certs import expand_template
from compute_space.core.tls.app_certs import provision_app_certs
from compute_space.core.tls.app_certs import render_cert_request

ZONE = "alice.example.com"
APP = "xmpp"


def _make_cert(domains, *, days_valid=90):
    key = ec.generate_private_key(ec.SECP256R1())
    now = datetime.datetime.now(datetime.UTC)
    builder = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(x509.oid.NameOID.COMMON_NAME, domains[0])]))
        .issuer_name(x509.Name([x509.NameAttribute(x509.oid.NameOID.COMMON_NAME, domains[0])]))
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=1))
        .not_valid_after(now + datetime.timedelta(days=days_valid))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(d) for d in domains]), critical=False)
    )
    cert = builder.sign(key, hashes.SHA256())

    cert_pem = cert.public_bytes(serialization.Encoding.PEM)
    key_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    )
    return cert_pem, key_pem


class TestExpandTemplate:
    def test_app_and_zone(self):
        assert expand_template("{app}.{zone}", APP, ZONE) == f"{APP}.{ZONE}"

    def test_no_placeholder(self):
        assert expand_template("static.example.com", APP, ZONE) == "static.example.com"


class TestRenderCertRequest:
    def test_in_scope_domains(self):
        req = TlsCertRequest(label="x", domains=["{app}.{zone}", "conference.{app}.{zone}"])
        r = render_cert_request(req, APP, ZONE)
        assert r.domains == [f"{APP}.{ZONE}", f"conference.{APP}.{ZONE}"]

    def test_dedupes_domains(self):
        req = TlsCertRequest(label="x", domains=["{app}.{zone}", "xmpp.alice.example.com"])
        r = render_cert_request(req, APP, ZONE)
        assert r.domains == [f"{APP}.{ZONE}"]

    def test_bare_zone_rejected(self):
        req = TlsCertRequest(label="x", domains=["{zone}"])
        with pytest.raises(ValueError, match="outside this app's subtree"):
            render_cert_request(req, APP, ZONE)

    def test_other_app_rejected(self):
        req = TlsCertRequest(label="x", domains=["other.{zone}"])
        with pytest.raises(ValueError, match="outside this app's subtree"):
            render_cert_request(req, APP, ZONE)

    def test_unrelated_domain_rejected(self):
        req = TlsCertRequest(label="x", domains=["evil.com"])
        with pytest.raises(ValueError, match="outside this app's subtree"):
            render_cert_request(req, APP, ZONE)

    def test_app_prefix_not_subdomain_rejected(self):
        # "xmpp.alice.example.com.evil.com" endswith trick must not pass.
        req = TlsCertRequest(label="x", domains=["{app}.{zone}.evil.com"])
        with pytest.raises(ValueError, match="outside this app's subtree"):
            render_cert_request(req, APP, ZONE)


class TestWildcardCoverage:
    def test_base_and_single_label_covered(self):
        assert cert_covered_by_wildcard([ZONE, f"app.{ZONE}"], ZONE)

    def test_two_level_not_covered(self):
        assert not cert_covered_by_wildcard([f"conference.{APP}.{ZONE}"], ZONE)

    def test_single_level_app_covered(self):
        assert cert_covered_by_wildcard([f"{APP}.{ZONE}"], ZONE)


class TestCertPresentAndCurrent:
    def test_missing_files(self, tmp_path):
        assert not cert_present_and_current(tmp_path / "c.crt", tmp_path / "c.key", [f"{APP}.{ZONE}"])

    def test_valid_cert_current(self, tmp_path):
        cert_pem, key_pem = _make_cert([f"{APP}.{ZONE}"])
        (tmp_path / "c.crt").write_bytes(cert_pem)
        (tmp_path / "c.key").write_bytes(key_pem)
        assert cert_present_and_current(tmp_path / "c.crt", tmp_path / "c.key", [f"{APP}.{ZONE}"])

    def test_missing_san_triggers_reprovision(self, tmp_path):
        cert_pem, key_pem = _make_cert([f"{APP}.{ZONE}"])
        (tmp_path / "c.crt").write_bytes(cert_pem)
        (tmp_path / "c.key").write_bytes(key_pem)
        assert not cert_present_and_current(tmp_path / "c.crt", tmp_path / "c.key", [f"conference.{APP}.{ZONE}"])

    def test_near_expiry_triggers_reprovision(self, tmp_path):
        cert_pem, key_pem = _make_cert([f"{APP}.{ZONE}"], days_valid=10)
        (tmp_path / "c.crt").write_bytes(cert_pem)
        (tmp_path / "c.key").write_bytes(key_pem)
        assert not cert_present_and_current(tmp_path / "c.crt", tmp_path / "c.key", [f"{APP}.{ZONE}"])

    def test_wildcard_san_covers_single_label(self, tmp_path):
        cert_pem, key_pem = _make_cert([ZONE, f"*.{ZONE}"])
        (tmp_path / "c.crt").write_bytes(cert_pem)
        (tmp_path / "c.key").write_bytes(key_pem)
        assert cert_present_and_current(tmp_path / "c.crt", tmp_path / "c.key", [f"{APP}.{ZONE}"])

    def test_unreadable_cert(self, tmp_path):
        (tmp_path / "c.crt").write_bytes(b"not a cert")
        (tmp_path / "c.key").write_bytes(b"nope")
        assert not cert_present_and_current(tmp_path / "c.crt", tmp_path / "c.key", [f"{APP}.{ZONE}"])


class TestProvisionWildcardReuse:
    def test_reuses_wildcard_for_single_label(self, tmp_path):
        # Wildcard zone cert on disk; app requests only xmpp.{zone} -> reuse, no ACME.
        wc_cert, wc_key = _make_cert([ZONE, f"*.{ZONE}"])
        wildcard_cert = tmp_path / "openhost-tls-cert.pem"
        wildcard_key = tmp_path / "openhost-tls-key.pem"
        wildcard_cert.write_bytes(wc_cert)
        wildcard_key.write_bytes(wc_key)

        req = TlsCertRequest(label="main", domains=["{app}.{zone}"])
        rendered = provision_app_certs(
            app_name=APP,
            requests=[req],
            zone=ZONE,
            openhost_data_path=tmp_path,
            wildcard_cert_path=wildcard_cert,
            wildcard_key_path=wildcard_key,
            acme_account_key_path=None,
            coredns_zonefile_path=tmp_path / "zonefile",
            coredns_enabled=False,
        )
        assert len(rendered) == 1
        cert_dir = tmp_path / "app_certs" / APP
        written_cert = cert_dir / rendered[0].cert_rel_path
        written_key = cert_dir / rendered[0].key_rel_path
        assert written_cert.read_bytes() == wc_cert
        assert written_key.read_bytes() == wc_key
        # Key permissions tightened.
        assert oct((written_key.stat().st_mode) & 0o777) == "0o640"

    def test_two_level_without_coredns_raises(self, tmp_path):
        wc_cert, wc_key = _make_cert([ZONE, f"*.{ZONE}"])
        wildcard_cert = tmp_path / "cert.pem"
        wildcard_key = tmp_path / "key.pem"
        wildcard_cert.write_bytes(wc_cert)
        wildcard_key.write_bytes(wc_key)
        req = TlsCertRequest(label="main", domains=["conference.{app}.{zone}"])
        with pytest.raises(RuntimeError, match="CoreDNS is disabled"):
            provision_app_certs(
                app_name=APP,
                requests=[req],
                zone=ZONE,
                openhost_data_path=tmp_path,
                wildcard_cert_path=wildcard_cert,
                wildcard_key_path=wildcard_key,
                acme_account_key_path=None,
                coredns_zonefile_path=tmp_path / "zonefile",
                coredns_enabled=False,
            )

    def test_out_of_scope_request_raises(self, tmp_path):
        req = TlsCertRequest(label="main", domains=["{zone}"])
        with pytest.raises(ValueError, match="outside this app's subtree"):
            provision_app_certs(
                app_name=APP,
                requests=[req],
                zone=ZONE,
                openhost_data_path=tmp_path,
                wildcard_cert_path=tmp_path / "cert.pem",
                wildcard_key_path=tmp_path / "key.pem",
                acme_account_key_path=None,
                coredns_zonefile_path=tmp_path / "zonefile",
                coredns_enabled=False,
            )

    def test_current_cert_not_reprovisioned(self, tmp_path):
        wc_cert, wc_key = _make_cert([ZONE, f"*.{ZONE}"])
        wildcard_cert = tmp_path / "cert.pem"
        wildcard_key = tmp_path / "key.pem"
        wildcard_cert.write_bytes(wc_cert)
        wildcard_key.write_bytes(wc_key)
        req = TlsCertRequest(label="main", domains=["{app}.{zone}"])
        kwargs = dict(
            app_name=APP,
            requests=[req],
            zone=ZONE,
            openhost_data_path=tmp_path,
            wildcard_cert_path=wildcard_cert,
            wildcard_key_path=wildcard_key,
            acme_account_key_path=None,
            coredns_zonefile_path=tmp_path / "zonefile",
            coredns_enabled=False,
        )
        rendered = provision_app_certs(**kwargs)
        written = tmp_path / "app_certs" / APP / rendered[0].cert_rel_path
        first_mtime = written.stat().st_mtime_ns
        # Re-run: should be a no-op (cert still current).
        provision_app_certs(**kwargs)
        assert written.stat().st_mtime_ns == first_mtime
