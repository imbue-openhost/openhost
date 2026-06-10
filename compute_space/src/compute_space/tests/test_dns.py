from __future__ import annotations

import socket
from pathlib import Path

import pytest

import compute_space.core.dns as dns_mod
from compute_space.core.dns import TxtRecord
from compute_space.core.dns import clear_txt
from compute_space.core.dns import set_txt_records


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


def test_set_txt_records_writes_absolute_fqdn_names(tmp_path: Path) -> None:
    zonefile = tmp_path / "zonefile"
    _write_zonefile(zonefile, serial=100)

    # Two challenges sharing one record_name (base + wildcard) plus a distinct one.
    set_txt_records(
        zonefile,
        [
            TxtRecord(record_name="_acme-challenge.app.example.com", record_value="base-value"),
            TxtRecord(record_name="_acme-challenge.app.example.com", record_value="wildcard-value"),
        ],
    )

    content = zonefile.read_text()
    # Names are written as absolute FQDNs (trailing dot) so CoreDNS does not
    # re-append $ORIGIN.
    assert '_acme-challenge.app.example.com.   IN TXT  "base-value"' in content
    assert '_acme-challenge.app.example.com.   IN TXT  "wildcard-value"' in content
    # Not doubled up into _acme-challenge.app.example.com.app.example.com.
    assert "app.example.com.app.example.com" not in content
    # Serial bumped so CoreDNS reloads.
    assert "101   ; serial" in content


def test_set_txt_records_preserves_existing_trailing_dot(tmp_path: Path) -> None:
    zonefile = tmp_path / "zonefile"
    _write_zonefile(zonefile)

    set_txt_records(zonefile, [TxtRecord(record_name="_acme-challenge.app.example.com.", record_value="v")])

    content = zonefile.read_text()
    assert '_acme-challenge.app.example.com.   IN TXT  "v"' in content
    assert "app.example.com..   IN TXT" not in content


def test_clear_txt_removes_broker_records(tmp_path: Path) -> None:
    zonefile = tmp_path / "zonefile"
    _write_zonefile(zonefile)
    set_txt_records(zonefile, [TxtRecord(record_name="_acme-challenge.app.example.com", record_value="v")])

    clear_txt(zonefile)

    assert "IN TXT" not in zonefile.read_text()
