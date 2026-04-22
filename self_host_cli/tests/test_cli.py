"""Tests for the openhost CLI.

These tests exercise argument parsing, doctor checks, config generation,
and the down command -- all without actually starting services.
"""

import os
import subprocess
from argparse import Namespace

import pytest

from self_host_cli import doctor as doctor_mod
from self_host_cli.doctor import _check_podman
from self_host_cli.doctor import _check_port
from self_host_cli.doctor import _check_python
from self_host_cli.doctor import _check_router_code
from self_host_cli.doctor import _check_uv
from self_host_cli.doctor import run_doctor
from self_host_cli.down import _cleanup_pidfile
from self_host_cli.down import _is_alive
from self_host_cli.down import _read_pid
from self_host_cli.main import _build_parser
from self_host_cli.up import _detect_public_ip
from self_host_cli.up import _resolve_zone_domain

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------


class TestArgParsing:
    def test_no_args_is_none_command(self):
        parser = _build_parser()
        args = parser.parse_args([])
        assert args.command is None

    def test_up_defaults(self):
        parser = _build_parser()
        args = parser.parse_args(["up"])
        assert args.command == "up"
        assert args.domain == ""
        assert args.zone_domain == ""
        assert args.email == ""
        assert args.port == 8080
        assert args.foreground is False

    def test_up_domain(self):
        parser = _build_parser()
        args = parser.parse_args(["up", "--domain", "example.com"])
        assert args.domain == "example.com"

    def test_up_zone_domain(self):
        parser = _build_parser()
        args = parser.parse_args(["up", "--zone-domain", "example.com"])
        assert args.zone_domain == "example.com"

    def test_up_domain_and_zone_domain(self):
        parser = _build_parser()
        args = parser.parse_args(["up", "--domain", "example.com", "--zone-domain", "example.com"])
        assert args.domain == "example.com"
        assert args.zone_domain == "example.com"

    def test_up_email(self):
        parser = _build_parser()
        args = parser.parse_args(["up", "--domain", "example.com", "--email", "admin@example.com"])
        assert args.domain == "example.com"
        assert args.email == "admin@example.com"

    def test_up_custom_port(self):
        parser = _build_parser()
        args = parser.parse_args(["up", "--port", "9090"])
        assert args.port == 9090

    def test_up_foreground(self):
        parser = _build_parser()
        args = parser.parse_args(["up", "--foreground"])
        assert args.foreground is True

    def test_down(self):
        parser = _build_parser()
        args = parser.parse_args(["down"])
        assert args.command == "down"

    def test_doctor(self):
        parser = _build_parser()
        args = parser.parse_args(["doctor"])
        assert args.command == "doctor"

    def test_update(self):
        parser = _build_parser()
        args = parser.parse_args(["update"])
        assert args.command == "update"


# ---------------------------------------------------------------------------
# Doctor checks
# ---------------------------------------------------------------------------


class TestDoctorChecks:
    def test_python_version(self):
        c = _check_python()
        assert c.ok is True

    def test_uv_check(self):
        c = _check_uv()
        assert c.ok is True

    def test_router_code_present(self):
        c = _check_router_code()
        assert c.ok is True

    def test_port_check_high_port(self):
        """A random high port should typically be available."""
        c = _check_port(59123)
        assert c.ok is True

    def test_podman_check_returns_check(self):
        c = _check_podman()
        assert hasattr(c, "ok")
        assert hasattr(c, "name")

    def test_podman_check_accepts_rootless_true(self, monkeypatch):
        """The happy path: podman info reports ``rootless=true`` ⇒ OK.
        We can't rely on the test host actually having rootless podman
        configured, so mock subprocess.run with a canned JSON payload."""

        class _R:
            returncode = 0
            stdout = '{"host":{"security":{"rootless":true}}}'

        monkeypatch.setattr(subprocess, "run", lambda *_a, **_kw: _R())
        c = doctor_mod._check_podman()
        assert c.ok is True
        assert c.detail == "rootless mode"

    def test_podman_check_rejects_rootful_installation(self, monkeypatch):
        """Security-sensitive: rootful podman must be rejected because
        the router's idmap/userns model depends on rootless mode."""

        class _R:
            returncode = 0
            stdout = '{"host":{"security":{"rootless":false}}}'

        monkeypatch.setattr(subprocess, "run", lambda *_a, **_kw: _R())
        c = doctor_mod._check_podman()
        assert c.ok is False
        assert "rootful" in c.detail

    def test_podman_check_rejects_missing_rootless_key(self, monkeypatch):
        """If podman info JSON doesn't expose host.security.rootless at
        all (unexpected format), report failure rather than silently
        defaulting to 'ok' — we can't verify the security prerequisite."""

        class _R:
            returncode = 0
            stdout = '{"host":{"security":{}}}'

        monkeypatch.setattr(subprocess, "run", lambda *_a, **_kw: _R())
        c = doctor_mod._check_podman()
        assert c.ok is False
        assert "rootless status" in c.detail

    def test_podman_check_rejects_invalid_json(self, monkeypatch):
        class _R:
            returncode = 0
            stdout = "this is not json"

        monkeypatch.setattr(subprocess, "run", lambda *_a, **_kw: _R())
        c = doctor_mod._check_podman()
        assert c.ok is False
        assert "non-JSON" in c.detail

    def test_podman_check_rejects_nonzero_exit(self, monkeypatch):
        """``podman info`` returning non-zero (engine error) must surface
        as a failed check, not silently pass."""

        class _R:
            returncode = 1
            stdout = ""

        monkeypatch.setattr(subprocess, "run", lambda *_a, **_kw: _R())
        c = doctor_mod._check_podman()
        assert c.ok is False
        assert "podman info failed" in c.detail

    def test_podman_check_reports_missing_binary(self, monkeypatch):
        def _raise(*_a, **_kw):
            raise FileNotFoundError(2, "podman")

        monkeypatch.setattr(subprocess, "run", _raise)
        c = doctor_mod._check_podman()
        assert c.ok is False
        assert "PATH" in c.detail

    def test_podman_check_reports_timeout(self, monkeypatch):
        def _raise(*_a, **_kw):
            raise subprocess.TimeoutExpired(cmd="podman", timeout=10)

        monkeypatch.setattr(subprocess, "run", _raise)
        c = doctor_mod._check_podman()
        assert c.ok is False
        assert "timed out" in c.detail

    def test_podman_check_reports_oserror(self, monkeypatch):
        """EPERM on the binary / fd exhaustion / similar must not crash
        ``openhost doctor`` with an unhandled traceback; consistent with
        how core.containers.podman_available handles the same case."""

        def _raise(*_a, **_kw):
            raise OSError(13, "Permission denied")

        monkeypatch.setattr(subprocess, "run", _raise)
        c = doctor_mod._check_podman()
        assert c.ok is False
        assert "OSError" in c.detail

    def test_run_doctor_returns_bool(self, capsys):
        result = run_doctor()
        assert isinstance(result, bool)
        out = capsys.readouterr().out
        assert "Checks" in out


# ---------------------------------------------------------------------------
# Down (pidfile helpers)
# ---------------------------------------------------------------------------


class TestDownHelpers:
    def test_read_pid_missing_file(self, tmp_path):
        assert _read_pid(str(tmp_path / "nope.pid")) is None

    def test_read_pid_valid(self, tmp_path):
        pid_file = tmp_path / "test.pid"
        pid_file.write_text("12345")
        assert _read_pid(str(pid_file)) == 12345

    def test_read_pid_invalid(self, tmp_path):
        pid_file = tmp_path / "test.pid"
        pid_file.write_text("not-a-number")
        assert _read_pid(str(pid_file)) is None

    def test_is_alive_current_process(self):
        assert _is_alive(os.getpid()) is True

    def test_is_alive_nonexistent(self):
        assert _is_alive(1 << 30) is False

    def test_cleanup_pidfile(self, tmp_path):
        pid_file = tmp_path / "test.pid"
        pid_file.write_text("123")
        _cleanup_pidfile(str(pid_file))
        assert not pid_file.exists()

    def test_cleanup_pidfile_missing(self, tmp_path):
        _cleanup_pidfile(str(tmp_path / "nope.pid"))


# ---------------------------------------------------------------------------
# Up (unit-testable helpers)
# ---------------------------------------------------------------------------


class TestUpHelpers:
    def test_detect_public_ip_returns_string(self):
        result = _detect_public_ip()
        assert isinstance(result, str)

    def test_detect_public_ip_respects_env(self, monkeypatch):
        monkeypatch.setenv("PUBLIC_IP", "1.2.3.4")
        assert _detect_public_ip() == "1.2.3.4"

    def test_detect_public_ip_returns_empty_on_error(self, monkeypatch):
        def fake_run(*args, **kwargs):
            raise OSError("hostname not found")

        def fake_socket(*args, **kwargs):
            raise OSError("no route")

        monkeypatch.setattr("self_host_cli.up.subprocess.run", fake_run)
        monkeypatch.setattr("self_host_cli.up.socket.socket", fake_socket)
        monkeypatch.delenv("PUBLIC_IP", raising=False)
        assert _detect_public_ip() == ""

    def test_resolve_zone_domain_prefers_domain(self):
        args = Namespace(domain="example.com", zone_domain="")
        assert _resolve_zone_domain(args) == "example.com"

    def test_resolve_zone_domain_uses_zone_domain(self):
        args = Namespace(domain="", zone_domain="example.com")
        assert _resolve_zone_domain(args) == "example.com"

    def test_resolve_zone_domain_rejects_mismatch(self):
        args = Namespace(domain="example.com", zone_domain="other.com")
        with pytest.raises(SystemExit):
            _resolve_zone_domain(args)
