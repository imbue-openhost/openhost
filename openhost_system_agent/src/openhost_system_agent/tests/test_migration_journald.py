"""Tests for the v6 migration that caps journald's on-disk size.

The migration writes a journald drop-in and asks journald to apply it, so we
drive it through fakes to assert it writes the right file with the right mode,
restarts journald, and vacuums the journal down to the cap.  A separate test
enforces that the drop-in it writes stays byte-identical with the ansible task
that provisions fresh hosts.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from openhost_system_agent.migrations.versions.v0006_journald_size_cap import JOURNALD_DROPIN_CONTENT
from openhost_system_agent.migrations.versions.v0006_journald_size_cap import JOURNALD_DROPIN_PATH
from openhost_system_agent.migrations.versions.v0006_journald_size_cap import JOURNALD_MAX_USE
from openhost_system_agent.migrations.versions.v0006_journald_size_cap import Migration0006JournaldSizeCap

_PREFIX = "openhost_system_agent.migrations.versions.v0006_journald_size_cap"


def test_writes_dropin_and_applies(monkeypatch: pytest.MonkeyPatch) -> None:
    written: dict[str, Any] = {}

    def fake_write_file(path: str, content: str, *, mode: int = 0o600) -> None:
        written["path"] = path
        written["content"] = content
        written["mode"] = mode

    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **kwargs: Any) -> None:
        calls.append(cmd)

    monkeypatch.setattr(f"{_PREFIX}.write_file", fake_write_file)
    monkeypatch.setattr(f"{_PREFIX}.subprocess.run", fake_run)

    Migration0006JournaldSizeCap().up()

    # World-readable drop-in at the expected path with the cap set.
    assert written["path"] == JOURNALD_DROPIN_PATH
    assert written["mode"] == 0o644
    assert f"SystemMaxUse={JOURNALD_MAX_USE}" in written["content"]

    # journald is restarted so the cap takes effect without a reboot.
    assert ["systemctl", "restart", "systemd-journald"] in calls
    # The journal is vacuumed down to the cap immediately.
    vacuum = [c for c in calls if c and c[0] == "journalctl"]
    assert vacuum == [["journalctl", f"--vacuum-size={JOURNALD_MAX_USE}"]]


def test_dropin_targets_a_dropin_not_the_main_conf() -> None:
    # A drop-in under journald.conf.d, not /etc/systemd/journald.conf, so we
    # never clobber operator or distro settings.
    assert JOURNALD_DROPIN_PATH.startswith("/etc/systemd/journald.conf.d/")
    assert "[Journal]" in JOURNALD_DROPIN_CONTENT


def test_matches_ansible_task_byte_for_byte() -> None:
    # The migration-written drop-in and the ansible-copied drop-in must be
    # identical so a host looks the same however it was set up.  The ansible
    # task embeds the content inline in a `copy: content:` block, so extract
    # and compare it here.
    repo_root = Path(__file__).resolve().parents[4]
    task = (repo_root / "ansible" / "tasks" / "journald.yml").read_text()

    marker = "content: |\n"
    start = task.index(marker) + len(marker)
    # The literal block is indented 6 spaces under `content: |`.  Keep only the
    # lines carrying that indent (the following `dest:`/`mode:` keys are indented
    # 4 spaces and mark the end of the block), then strip the indent.
    indent = " " * 6
    block_lines: list[str] = []
    for line in task[start:].splitlines():
        if line.startswith(indent):
            block_lines.append(line[len(indent) :])
        elif line.strip() == "":
            block_lines.append("")
        else:
            break
    ansible_content = "\n".join(block_lines).rstrip("\n") + "\n"

    assert ansible_content == JOURNALD_DROPIN_CONTENT
