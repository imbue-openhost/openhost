from __future__ import annotations

import socket
from pathlib import Path

import pytest

import compute_space.core.dns as dns_mod
from compute_space.core.dns import TxtRecord
from compute_space.core.dns import append_txt_records
from compute_space.core.dns import clear_txt


def _write_zonefile(path: Path, serial: int = 100) -> None:
    path.write_text(
        "$ORIGIN app.example.com.\n"
        "$TTL 60\n"
        "@   IN SOA  ns.app.example.com. admin.app.example.com. (\n"
        f"    {serial}   ; serial\n"
        "    3600  ; refresh\n"
        "    600   ; retry\n"
        "    86400 ; expire\n"
        "    60    ; minimum\n"
        ")\n"
        "@   IN NS   ns.app.example.com.\n"
        "@   IN A    127.0.0.1\n"
    )


class _FakeSocket:
    def __enter__(self) -> _FakeSocket:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None

    def connect(self, addr: tuple[str, int]) -> None:
        self.addr = addr

    def getsockname(self) -> tuple[str, int]:
        return ("10.0.0.5", 12345)


def test_coredns_bind_ip_uses_default_route_source(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_socket = _FakeSocket()
    monkeypatch.setattr(dns_mod.socket, "socket", lambda *args: fake_socket)

    assert dns_mod._coredns_bind_ip("203.0.113.10") == "10.0.0.5"
    assert fake_socket.addr == ("8.8.8.8", 80)


def test_coredns_bind_ip_falls_back_to_public_ip(monkeypatch: pytest.MonkeyPatch) -> None:
    def raise_os_error(*args: object) -> object:
        raise OSError("no route")

    monkeypatch.setattr(socket, "socket", raise_os_error)

    assert dns_mod._coredns_bind_ip("203.0.113.10") == "203.0.113.10"


def test_append_txt_records_writes_relative_names_verbatim(tmp_path: Path) -> None:
    zonefile = tmp_path / "zonefile"
    _write_zonefile(zonefile, serial=100)

    # Local DNS-01 path: several values share one relative name, left for CoreDNS
    # to resolve against $ORIGIN.
    append_txt_records(
        zonefile,
        [
            TxtRecord(record_name="_acme-challenge", record_value="base-value"),
            TxtRecord(record_name="_acme-challenge", record_value="wildcard-value"),
        ],
    )

    content = zonefile.read_text()
    assert '_acme-challenge   IN TXT  "base-value"' in content
    assert '_acme-challenge   IN TXT  "wildcard-value"' in content
    # Relative name is not turned into an absolute FQDN.
    assert "_acme-challenge.   IN TXT" not in content
    # Serial bumped so CoreDNS reloads.
    assert "101   ; serial" in content


def test_append_txt_records_writes_absolute_fqdn_names_verbatim(tmp_path: Path) -> None:
    zonefile = tmp_path / "zonefile"
    _write_zonefile(zonefile)

    # Broker path: names arrive as absolute FQDNs (trailing dot) so CoreDNS does
    # not re-append $ORIGIN.
    append_txt_records(zonefile, [TxtRecord(record_name="_acme-challenge.app.example.com.", record_value="v")])

    content = zonefile.read_text()
    assert '_acme-challenge.app.example.com.   IN TXT  "v"' in content
    # Not doubled up into _acme-challenge.app.example.com.app.example.com.
    assert "app.example.com.app.example.com" not in content


def test_clear_txt_removes_records(tmp_path: Path) -> None:
    zonefile = tmp_path / "zonefile"
    _write_zonefile(zonefile)
    append_txt_records(zonefile, [TxtRecord(record_name="_acme-challenge.app.example.com.", record_value="v")])

    clear_txt(zonefile)

    assert "IN TXT" not in zonefile.read_text()


class _FakeProc:
    pid = 4242
    stdout = None

    def wait(self) -> int:
        return 0


def _stub_popen(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dns_mod.subprocess, "Popen", lambda *a, **k: _FakeProc())
    # Don't spawn the log-streaming thread (its target reads proc.stdout).
    monkeypatch.setattr(dns_mod.threading, "Thread", lambda *a, **k: type("T", (), {"start": lambda self: None})())


def test_container_dns_view_rendered_when_gateway_bindable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dns_mod, "_coredns_bind_ip", lambda ip: "10.0.0.5")
    monkeypatch.setattr(dns_mod, "_gateway_ip_is_bindable", lambda ip: True)
    monkeypatch.setattr(dns_mod, "_host_upstream_resolvers", lambda: ["9.9.9.9"])
    _stub_popen(monkeypatch)

    corefile = tmp_path / "Corefile"
    zonefile = tmp_path / "zonefile"
    dns_mod.start_coredns("app.example.com", "203.0.113.10", corefile, zonefile, container_gateway_ip="10.200.0.1")

    cf = corefile.read_text()
    # Public view binds the discovered local IP; container view binds the gateway.
    assert "bind 10.0.0.5" in cf
    assert "bind 10.200.0.1" in cf
    assert "forward . 9.9.9.9" in cf

    # Public zonefile points at the public IP; container zonefile at the gateway.
    assert "203.0.113.10" in zonefile.read_text()
    container_zone = tmp_path / "zonefile.container"
    assert container_zone.exists()
    cz = container_zone.read_text()
    assert "*   IN A    10.200.0.1" in cz
    assert "203.0.113.10" not in cz


def test_container_dns_view_skipped_when_gateway_not_bindable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dns_mod, "_coredns_bind_ip", lambda ip: "10.0.0.5")
    monkeypatch.setattr(dns_mod, "_gateway_ip_is_bindable", lambda ip: False)
    _stub_popen(monkeypatch)

    corefile = tmp_path / "Corefile"
    zonefile = tmp_path / "zonefile"
    dns_mod.start_coredns("app.example.com", "203.0.113.10", corefile, zonefile, container_gateway_ip="10.200.0.1")

    cf = corefile.read_text()
    assert "bind 10.200.0.1" not in cf
    assert "forward" not in cf
    # No container zonefile written.
    assert not (tmp_path / "zonefile.container").exists()


def test_host_upstream_resolvers_filters_loopback_and_gateway(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    resolv = tmp_path / "resolv.conf"
    resolv.write_text(
        "nameserver 127.0.0.53\n"
        f"nameserver {dns_mod.CONTAINER_GATEWAY_IP}\n"
        "nameserver 185.12.64.1\n"
        "nameserver 1.1.1.1\n"
        "search example.com\n"
    )
    real_open = open
    monkeypatch.setattr(
        "builtins.open",
        lambda p, *a, **k: real_open(resolv, *a, **k) if str(p) == "/etc/resolv.conf" else real_open(p, *a, **k),
    )
    assert dns_mod._host_upstream_resolvers() == ["185.12.64.1", "1.1.1.1"]


def test_host_upstream_resolvers_falls_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def raise_oserror(*a: object, **k: object) -> object:
        raise OSError("nope")

    monkeypatch.setattr("builtins.open", raise_oserror)
    assert dns_mod._host_upstream_resolvers() == list(dns_mod._FALLBACK_UPSTREAM_DNS)


def test_host_upstream_resolvers_falls_back_when_only_loopback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A host using only the systemd-resolved stub (127.0.0.53) would leave the
    # container view with no forwardable upstream; we must fall back, never emit
    # an empty/loopback forward (which would be unreachable from the container).
    resolv = tmp_path / "resolv.conf"
    resolv.write_text("nameserver 127.0.0.53\n")
    real_open = open
    monkeypatch.setattr(
        "builtins.open",
        lambda p, *a, **k: real_open(resolv, *a, **k) if str(p) == "/etc/resolv.conf" else real_open(p, *a, **k),
    )
    assert dns_mod._host_upstream_resolvers() == list(dns_mod._FALLBACK_UPSTREAM_DNS)


def test_container_view_forward_uses_discovered_resolvers_and_distinct_bind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The public view and the container view must bind different addresses (the
    # default-route source vs the gateway), and the container catch-all must
    # forward to the discovered upstreams.
    monkeypatch.setattr(dns_mod, "_coredns_bind_ip", lambda ip: "10.0.0.5")
    monkeypatch.setattr(dns_mod, "_gateway_ip_is_bindable", lambda ip: True)
    monkeypatch.setattr(dns_mod, "_host_upstream_resolvers", lambda: ["185.12.64.1", "1.1.1.1"])
    _stub_popen(monkeypatch)

    corefile = tmp_path / "Corefile"
    dns_mod.start_coredns("app.example.com", "203.0.113.10", corefile, tmp_path / "zonefile")
    cf = corefile.read_text()

    assert "bind 10.0.0.5" in cf  # public/authoritative view
    assert "bind 10.200.0.1" in cf  # container view + catch-all
    assert "forward . 185.12.64.1 1.1.1.1" in cf
    # Catch-all is scoped to the container gateway only (never the public bind),
    # so the public IP is not turned into an open recursive resolver.
    catch_all = cf.split(".:53 {", 1)[1]
    assert "bind 10.200.0.1" in catch_all
    assert "bind 10.0.0.5" not in catch_all
