"""Tests for the periodic dangling-image pruner.

The pruner runs a daemon thread that periodically prunes only dangling images
(``podman image prune`` without ``--all``).  These tests drive the pieces
directly: the prune command shape, thread start/idempotency/disable, and that
the loop swallows failures so a transient podman error can't kill it.
"""

from __future__ import annotations

import subprocess
from typing import Any

import pytest

import compute_space.core.image_pruner as image_pruner
from compute_space.core.containers import prune_dangling_images
from compute_space.tests.conftest import _make_test_config


def test_prune_dangling_images_omits_all_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: dict[str, Any] = {}

    def fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls["cmd"] = cmd
        calls["timeout"] = kwargs.get("timeout")
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="Total reclaimed space: 5MB\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    output = prune_dangling_images()

    assert output == "Total reclaimed space: 5MB"
    # Dangling-only: no --all (which would also remove tagged stopped-app images).
    assert calls["cmd"] == ["podman", "image", "prune", "--force"]
    assert "--all" not in calls["cmd"]
    assert calls["timeout"] == 120


def test_prune_dangling_images_raises_on_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args=cmd, returncode=1, stdout="", stderr="podman broke")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="podman broke"):
        prune_dangling_images()


def test_run_prune_once_swallows_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom() -> str:
        raise RuntimeError("podman broke")

    monkeypatch.setattr(image_pruner, "prune_dangling_images", boom)
    # Must not raise — the loop relies on this so one bad prune can't kill it.
    image_pruner._run_prune_once()


def test_start_image_pruner_noop_when_disabled(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    config = _make_test_config(tmp_path, image_prune_interval_seconds=0)
    image_pruner._pruner_db_paths.clear()

    started: list[bool] = []

    class FakeThread:
        def __init__(self, target: Any, args: Any, daemon: bool) -> None:
            started.append(True)

        def start(self) -> None:
            pass

    monkeypatch.setattr(image_pruner.threading, "Thread", FakeThread)

    image_pruner.start_image_pruner(config)
    assert started == []


def test_start_image_pruner_starts_and_is_idempotent(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    config = _make_test_config(tmp_path, image_prune_interval_seconds=3600)
    image_pruner._pruner_db_paths.clear()

    started: list[Any] = []

    class FakeThread:
        def __init__(self, target: Any, args: Any, daemon: bool) -> None:
            self.target = target
            self.args = args
            self.daemon = daemon

        def start(self) -> None:
            started.append(self)

    monkeypatch.setattr(image_pruner.threading, "Thread", FakeThread)

    image_pruner.start_image_pruner(config)
    image_pruner.start_image_pruner(config)  # idempotent per db_path

    assert len(started) == 1
    assert started[0].daemon is True
    assert started[0].args == (3600,)


def test_loop_sleeps_before_first_prune(monkeypatch: pytest.MonkeyPatch) -> None:
    # The loop must sleep the interval before its first prune (so a mid-build
    # deploy's intermediate layers aren't pruned during the startup rush), then
    # prune each cycle.  Stop it after two iterations via a sleep that raises.
    sleeps: list[int] = []
    prunes: list[bool] = []

    class _Stop(Exception):
        pass

    def fake_sleep(seconds: int) -> None:
        sleeps.append(seconds)
        if len(sleeps) >= 2:
            raise _Stop

    monkeypatch.setattr(image_pruner.time, "sleep", fake_sleep)
    monkeypatch.setattr(image_pruner, "_run_prune_once", lambda: prunes.append(True))

    with pytest.raises(_Stop):
        image_pruner._image_pruner_loop(1800)

    # Slept before the first prune, and pruned once between the two sleeps.
    assert sleeps == [1800, 1800]
    assert prunes == [True]
