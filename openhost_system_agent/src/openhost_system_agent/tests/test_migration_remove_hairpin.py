"""Tests for the v3 migration that removes obsolete hairpin DNAT rules.

The migration mutates host firewall state and files, so we drive it through a
fake subprocess to assert it builds the right iptables commands, loops to clear
duplicate rules, and is safe/idempotent when nothing is present.
"""

from __future__ import annotations

from typing import Any

import pytest

import openhost_system_agent.migrations.versions.v0003_remove_obsolete_hairpin_nat as mod
from openhost_system_agent.migrations.versions.v0003_remove_obsolete_hairpin_nat import (
    Migration0003RemoveObsoleteHairpinNat,
)

_PREFIX = "openhost_system_agent.migrations.versions.v0003_remove_obsolete_hairpin_nat"


class _FakeCompleted:
    def __init__(self, returncode: int = 0, stdout: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout


def _patch_public_ip(monkeypatch: pytest.MonkeyPatch, ip: str | None) -> None:
    monkeypatch.setattr(f"{_PREFIX}._detect_public_ip", lambda: ip)


def _patch_unlink(monkeypatch: pytest.MonkeyPatch, sink: list[str] | None = None) -> None:
    def fake_unlink(self: Any, missing_ok: bool = False) -> None:
        if sink is not None:
            sink.append(str(self))

    monkeypatch.setattr(f"{_PREFIX}.Path.unlink", fake_unlink)


def test_removes_both_ports_when_rules_present(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_public_ip(monkeypatch, "203.0.113.10")
    calls: list[list[str]] = []
    check_state = {"80": 1, "443": 1}  # rule present once per port, then gone

    def fake_run(cmd: list[str], **kwargs: Any) -> _FakeCompleted:
        calls.append(cmd)
        if cmd[:2] == ["iptables", "-C"]:
            port = cmd[cmd.index("--dport") + 1]
            if check_state[port] > 0:
                check_state[port] -= 1
                return _FakeCompleted(0)
            return _FakeCompleted(1)
        return _FakeCompleted(0)

    monkeypatch.setattr(f"{_PREFIX}.subprocess.run", fake_run)
    _patch_unlink(monkeypatch)

    Migration0003RemoveObsoleteHairpinNat().up()

    deletes = [c for c in calls if c[:2] == ["iptables", "-D"]]
    assert len(deletes) == 2, deletes
    assert {c[c.index("--dport") + 1] for c in deletes} == {"80", "443"}
    for c in deletes:
        assert c[2] == "OUTPUT"
        assert "203.0.113.10" in c
        assert any(a.startswith("127.0.0.1:") for a in c)


def test_idempotent_when_no_rules(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_public_ip(monkeypatch, "203.0.113.10")
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **kwargs: Any) -> _FakeCompleted:
        calls.append(cmd)
        if cmd[:2] == ["iptables", "-C"]:
            return _FakeCompleted(1)  # rule absent
        return _FakeCompleted(0)

    monkeypatch.setattr(f"{_PREFIX}.subprocess.run", fake_run)
    _patch_unlink(monkeypatch)

    Migration0003RemoveObsoleteHairpinNat().up()

    assert [c for c in calls if c[:2] == ["iptables", "-D"]] == []


def test_clears_duplicate_rules(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_public_ip(monkeypatch, "203.0.113.10")
    remaining = {"80": 3, "443": 3}  # stacked duplicates the loop must clear

    def fake_run(cmd: list[str], **kwargs: Any) -> _FakeCompleted:
        if cmd[:2] == ["iptables", "-C"]:
            port = cmd[cmd.index("--dport") + 1]
            return _FakeCompleted(0 if remaining[port] > 0 else 1)
        if cmd[:2] == ["iptables", "-D"]:
            port = cmd[cmd.index("--dport") + 1]
            remaining[port] -= 1
            return _FakeCompleted(0)
        return _FakeCompleted(0)

    monkeypatch.setattr(f"{_PREFIX}.subprocess.run", fake_run)
    _patch_unlink(monkeypatch)

    Migration0003RemoveObsoleteHairpinNat().up()
    assert remaining == {"80": 0, "443": 0}


def test_skips_iptables_when_public_ip_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_public_ip(monkeypatch, None)
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **kwargs: Any) -> _FakeCompleted:
        calls.append(cmd)
        return _FakeCompleted(0)

    monkeypatch.setattr(f"{_PREFIX}.subprocess.run", fake_run)
    _patch_unlink(monkeypatch)

    Migration0003RemoveObsoleteHairpinNat().up()

    assert [c for c in calls if c and c[0] == "iptables"] == []


def test_removes_both_persistence_scripts(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_public_ip(monkeypatch, "203.0.113.10")
    monkeypatch.setattr(f"{_PREFIX}.subprocess.run", lambda cmd, **k: _FakeCompleted(1))
    unlinked: list[str] = []
    _patch_unlink(monkeypatch, unlinked)

    Migration0003RemoveObsoleteHairpinNat().up()

    assert set(unlinked) == set(mod._OBSOLETE_PERSISTENCE_SCRIPTS)
