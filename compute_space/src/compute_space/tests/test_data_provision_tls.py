"""Tests for provision_data TLS cert env-var injection.

When a manifest requests ``[tls] cert = true``, provision_data must inject
OPENHOST_TLS_CERT_PATH and OPENHOST_TLS_KEY_PATH unconditionally (pointing
at the fixed in-container paths).  File-existence validation happens in
run_container, not here.
"""

from __future__ import annotations

import os

from compute_space.core.data import provision_data
from compute_space.core.manifest import AppManifest


def _basic_manifest(**overrides) -> AppManifest:  # type: ignore[no-untyped-def]
    kwargs = dict(
        name="testapp",
        version="0.1.0",
        container_image="Dockerfile",
        container_port=8080,
    )
    kwargs.update(overrides)
    return AppManifest(**kwargs)  # type: ignore[arg-type]


def _provision(
    tmp_path,
    manifest: AppManifest,
    tls_cert_path: str | None = None,
    tls_key_path: str | None = None,
) -> dict[str, str]:
    data_dir = str(tmp_path / "persistent")
    temp_data_dir = str(tmp_path / "temp")
    archive_dir = str(tmp_path / "archive")
    os.makedirs(data_dir, exist_ok=True)
    os.makedirs(temp_data_dir, exist_ok=True)
    return provision_data(
        app_id="app-abc123",
        app_name=manifest.name,
        manifest=manifest,
        data_dir=data_dir,
        temp_data_dir=temp_data_dir,
        archive_dir=archive_dir,
        my_openhost_redirect_domain="my.selfhost.imbue.com",
        zone_domain="example.selfhost.imbue.com",
        port=8080,
        owner_username="owner",
        tls_cert_path=tls_cert_path,
        tls_key_path=tls_key_path,
    )


class TestProvisionDataTlsCert:
    """provision_data injects OPENHOST_TLS_CERT_PATH / OPENHOST_TLS_KEY_PATH
    whenever the manifest requests [tls] cert = true, regardless of whether
    the host cert files exist (run_container enforces that separately)."""

    def test_tls_env_vars_injected_when_tls_cert_true(self, tmp_path) -> None:
        manifest = _basic_manifest(tls_cert=True)
        env = _provision(tmp_path, manifest, tls_cert_path="/some/path.crt", tls_key_path="/some/path.key")

        assert env["OPENHOST_TLS_CERT_PATH"] == "/run/secrets/tls/tls.crt"
        assert env["OPENHOST_TLS_KEY_PATH"] == "/run/secrets/tls/tls.key"

    def test_tls_env_vars_injected_even_when_paths_not_provided(self, tmp_path) -> None:
        """provision_data injects the in-container paths regardless of whether
        the host cert path args are passed — those are only used by run_container."""
        manifest = _basic_manifest(tls_cert=True)
        env = _provision(tmp_path, manifest, tls_cert_path=None, tls_key_path=None)

        assert env["OPENHOST_TLS_CERT_PATH"] == "/run/secrets/tls/tls.crt"
        assert env["OPENHOST_TLS_KEY_PATH"] == "/run/secrets/tls/tls.key"

    def test_tls_env_vars_not_injected_when_manifest_flag_false(self, tmp_path) -> None:
        manifest = _basic_manifest(tls_cert=False)
        env = _provision(tmp_path, manifest, tls_cert_path="/some/path.crt", tls_key_path="/some/path.key")

        assert "OPENHOST_TLS_CERT_PATH" not in env
        assert "OPENHOST_TLS_KEY_PATH" not in env

    def test_standard_env_vars_still_present_alongside_tls(self, tmp_path) -> None:
        """Adding TLS env vars must not displace the standard OPENHOST_* vars."""
        manifest = _basic_manifest(tls_cert=True)
        env = _provision(tmp_path, manifest, tls_cert_path="/some/path.crt", tls_key_path="/some/path.key")

        assert "OPENHOST_APP_NAME" in env
        assert "OPENHOST_ZONE_DOMAIN" in env
        assert "OPENHOST_ROUTER_URL" in env
