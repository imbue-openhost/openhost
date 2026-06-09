"""Unit tests for per-app TLS cert provisioning/injection.

The live ACME DNS-01 issuance is exercised by the requires_tls integration
suite; here the ACME call is mocked, so these cover placeholder expansion,
scope validation, expiry gating, and the always-dedicated provisioning flow.
"""

import datetime

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from compute_space.core.manifest import TlsCertRequest
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


class TestProvisionAppCerts:
    """Provisioning always issues a DEDICATED cert with its own key — the zone
    wildcard (and its zone-wide private key) is never copied into an app."""

    def test_issues_dedicated_cert_with_own_key(self, tmp_path, monkeypatch):
        acme_calls = []

        def fake_acquire(*, domains, **kwargs):
            acme_calls.append(list(domains))
            return _make_cert(domains)

        monkeypatch.setattr("compute_space.core.tls.app_certs._acquire_cert_dns01", fake_acquire)
        monkeypatch.setattr(
            "compute_space.core.tls.app_certs.load_account_key", lambda p: object()
        )
        acct = tmp_path / "acct.json"
        acct.write_text("{}")

        req = TlsCertRequest(label="main", domains=["{app}.{zone}"])
        rendered = provision_app_certs(
            app_name=APP,
            requests=[req],
            zone=ZONE,
            openhost_data_path=tmp_path,
            acme_account_key_path=acct,
            coredns_zonefile_path=tmp_path / "zonefile",
            coredns_enabled=True,
        )
        # A dedicated ACME order was placed even for a single-label subdomain.
        assert acme_calls == [[f"{APP}.{ZONE}"]]
        cert_dir = tmp_path / "app_certs" / APP
        written_key = cert_dir / rendered[0].key_rel_path
        assert oct(written_key.stat().st_mode & 0o777) == "0o640"

    def test_never_reads_wildcard_key(self, tmp_path, monkeypatch):
        """Regression guard: provisioning must not read the zone wildcard key."""

        def fake_acquire(*, domains, **kwargs):
            return _make_cert(domains)

        monkeypatch.setattr("compute_space.core.tls.app_certs._acquire_cert_dns01", fake_acquire)
        monkeypatch.setattr("compute_space.core.tls.app_certs.load_account_key", lambda p: object())
        acct = tmp_path / "acct.json"
        acct.write_text("{}")
        req = TlsCertRequest(label="main", domains=["{app}.{zone}"])
        rendered = provision_app_certs(
            app_name=APP,
            requests=[req],
            zone=ZONE,
            openhost_data_path=tmp_path,
            acme_account_key_path=acct,
            coredns_zonefile_path=tmp_path / "zonefile",
            coredns_enabled=True,
        )
        # The written key is the freshly generated one (matches the fake cert),
        # not any shared wildcard key.
        cert = x509.load_pem_x509_certificate((tmp_path / "app_certs" / APP / rendered[0].cert_rel_path).read_bytes())
        san = set(
            cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value.get_values_for_type(
                x509.DNSName
            )
        )
        assert san == {f"{APP}.{ZONE}"}
        assert "*." + ZONE not in san  # never a wildcard

    def test_without_coredns_raises(self, tmp_path):
        req = TlsCertRequest(label="main", domains=["conference.{app}.{zone}"])
        with pytest.raises(RuntimeError, match="CoreDNS is disabled"):
            provision_app_certs(
                app_name=APP,
                requests=[req],
                zone=ZONE,
                openhost_data_path=tmp_path,
                acme_account_key_path=None,
                coredns_zonefile_path=tmp_path / "zonefile",
                coredns_enabled=False,
            )

    def test_no_acme_key_raises(self, tmp_path):
        req = TlsCertRequest(label="main", domains=["{app}.{zone}"])
        with pytest.raises(RuntimeError, match="no ACME account key"):
            provision_app_certs(
                app_name=APP,
                requests=[req],
                zone=ZONE,
                openhost_data_path=tmp_path,
                acme_account_key_path=None,
                coredns_zonefile_path=tmp_path / "zonefile",
                coredns_enabled=True,
            )

    def test_out_of_scope_request_raises(self, tmp_path):
        req = TlsCertRequest(label="main", domains=["{zone}"])
        with pytest.raises(ValueError, match="outside this app's subtree"):
            provision_app_certs(
                app_name=APP,
                requests=[req],
                zone=ZONE,
                openhost_data_path=tmp_path,
                acme_account_key_path=None,
                coredns_zonefile_path=tmp_path / "zonefile",
                coredns_enabled=False,
            )

    def test_current_cert_not_reprovisioned(self, tmp_path, monkeypatch):
        calls = []

        def fake_acquire(*, domains, **kwargs):
            calls.append(list(domains))
            return _make_cert(domains)

        monkeypatch.setattr("compute_space.core.tls.app_certs._acquire_cert_dns01", fake_acquire)
        monkeypatch.setattr("compute_space.core.tls.app_certs.load_account_key", lambda p: object())
        acct = tmp_path / "acct.json"
        acct.write_text("{}")
        kwargs = dict(
            app_name=APP,
            requests=[TlsCertRequest(label="main", domains=["{app}.{zone}"])],
            zone=ZONE,
            openhost_data_path=tmp_path,
            acme_account_key_path=acct,
            coredns_zonefile_path=tmp_path / "zonefile",
            coredns_enabled=True,
        )
        provision_app_certs(**kwargs)
        provision_app_certs(**kwargs)
        # Second run reused the current cert -> only one ACME order total.
        assert len(calls) == 1
