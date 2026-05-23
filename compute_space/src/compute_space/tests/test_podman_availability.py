"""Tests for container runtime graceful-failure paths.

``is_container_running()`` must return False (not raise) when podman is
missing or unusable, so callers can handle the condition rather than
crashing.
"""

from __future__ import annotations

import subprocess

import pytest

from compute_space.core.containers import is_container_running


def test_is_container_running_returns_false_on_filenotfound(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(cmd, capture_output, text, timeout):  # type: ignore[no-untyped-def]
        raise FileNotFoundError(2, "No such file or directory: 'podman'")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert is_container_running("container-id") is False


def test_is_container_running_returns_false_on_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(cmd, capture_output, text, timeout):  # type: ignore[no-untyped-def]
        raise subprocess.TimeoutExpired(cmd, timeout)

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert is_container_running("container-id") is False


def test_is_container_running_returns_false_on_oserror(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(cmd, capture_output, text, timeout):  # type: ignore[no-untyped-def]
        raise OSError(13, "Permission denied")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert is_container_running("container-id") is False
