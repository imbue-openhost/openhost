from __future__ import annotations

import socket

import pytest

import compute_space.core.dns as dns_mod
from compute_space.core.dns import set_txt_records
from compute_space.core.tls.util import _challenge_record_name


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


def test_set_txt_records_multiple_owners(tmp_path):
    zonefile = tmp_path / "zonefile"
    zonefile.write_text(
        "$ORIGIN z.example.\n$TTL 60\n@ IN SOA ns.z.example. admin.z.example. (\n"
        "    100   ; serial\n    3600 ; refresh\n    600 ; retry\n    86400 ; expire\n    60 ; minimum\n)\n"
        "@ IN NS ns.z.example.\n@ IN A 127.0.0.1\n"
    )
    set_txt_records(zonefile, {"_acme-challenge": ["v1", "v2"], "_acme-challenge.xmpp": ["v3"]})
    content = zonefile.read_text()
    assert '_acme-challenge   IN TXT  "v1"' in content
    assert '_acme-challenge   IN TXT  "v2"' in content
    assert '_acme-challenge.xmpp   IN TXT  "v3"' in content
    # Serial bumped to 101.
    assert "101   ; serial" in content


def test_challenge_record_name():
    zone = "alice.example.com"
    assert _challenge_record_name(zone, zone) == "_acme-challenge"
    assert _challenge_record_name(f"*.{zone}", zone) == "_acme-challenge"
    assert _challenge_record_name(f"xmpp.{zone}", zone) == "_acme-challenge.xmpp"
    assert _challenge_record_name(f"conference.xmpp.{zone}", zone) == "_acme-challenge.conference.xmpp"
