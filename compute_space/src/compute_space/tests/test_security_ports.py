"""Unit tests for the listening-port classifier in ``core.security``.

Exercises ``list_listening_ports``'s classification logic by stubbing out
the ``ss`` subprocess so we don't depend on the host actually having
processes listening on the ports under test.
"""

from __future__ import annotations

import subprocess
from unittest import mock

import pytest

from compute_space.core import security


_SS_FORMAT = "LISTEN  0  4096  {addr}  *:*"


@pytest.fixture
def fake_ss(monkeypatch: pytest.MonkeyPatch):
    """Patch subprocess.run so ``ss -tlnH`` returns a configurable line set."""

    def _make(addrs: list[str]):
        result = mock.Mock(stdout="\n".join(_SS_FORMAT.format(addr=a) for a in addrs))
        monkeypatch.setattr(subprocess, "run", lambda *a, **k: result)
        return result

    return _make


def _classify(ports, port: int) -> str | None:
    for entry in ports:
        if entry["port"] == port:
            return entry["classification"]
    return None


def test_juicefs_pprof_loopback_classified_secure(fake_ss) -> None:
    """JuiceFS binds 127.0.0.1:6060/:6061 for its pprof debug server.
    These must be classified as ``secure`` (not ``unexpected``) so the
    audit doesn't fail and the system tab doesn't surface them as a
    finding.
    """
    fake_ss(["127.0.0.1:6060", "127.0.0.1:6061"])

    ports = security.list_listening_ports(db=None)

    assert _classify(ports, 6060) == "secure"
    assert _classify(ports, 6061) == "secure"


def test_juicefs_pprof_range_loopback_classified_secure(fake_ss) -> None:
    """JuiceFS walks 6060..6099 if 6060 is taken; the entire range,
    when bound to loopback, should be allowed.
    """
    fake_ss([f"127.0.0.1:{p}" for p in (6060, 6075, 6099)])

    ports = security.list_listening_ports(db=None)

    for p in (6060, 6075, 6099):
        assert _classify(ports, p) == "secure", p


def test_juicefs_range_on_public_address_still_unexpected(fake_ss) -> None:
    """If something opens 6060 on a public interface (not loopback),
    do NOT silently allow it — the JuiceFS exception is loopback-only.
    """
    fake_ss(["0.0.0.0:6060"])

    ports = security.list_listening_ports(db=None)

    assert _classify(ports, 6060) == "unexpected"


def test_outside_juicefs_range_loopback_still_unexpected(fake_ss) -> None:
    """Loopback is not a blanket allow-list; ports outside 6060..6099
    on loopback are still classified as unexpected.
    """
    fake_ss(["127.0.0.1:5555"])

    ports = security.list_listening_ports(db=None)

    assert _classify(ports, 5555) == "unexpected"


def test_ipv6_loopback_juicefs_range(fake_ss) -> None:
    """``ss`` reports IPv6 loopback as ``[::1]:port``; the classifier
    should accept that form too.
    """
    fake_ss(["[::1]:6060"])

    ports = security.list_listening_ports(db=None)

    assert _classify(ports, 6060) == "secure"
