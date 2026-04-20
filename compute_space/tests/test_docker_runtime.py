"""Unit tests for DockerRuntime that don't need a running daemon.

These tests mock ``subprocess`` to verify argument construction and error
handling.  End-to-end Docker tests live under ``@requires_docker`` and need
``--run-docker``.
"""

from __future__ import annotations

import subprocess

import pytest

from compute_space.core.runtimes.docker import DockerRuntime


class _FakeCompleted:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_drop_build_cache_runs_builder_prune(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: dict[str, object] = {}

    def fake_run(cmd, capture_output, text, timeout):  # type: ignore[no-untyped-def]
        calls["cmd"] = cmd
        return _FakeCompleted(0, stdout="Total reclaimed space: 42MB\n")

    monkeypatch.setattr(subprocess, "run", fake_run)

    output = DockerRuntime().drop_build_cache()

    assert output == "Total reclaimed space: 42MB"
    assert calls["cmd"] == ["docker", "builder", "prune", "--all", "--force"]


def test_drop_build_cache_raises_on_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(cmd, capture_output, text, timeout):  # type: ignore[no-untyped-def]
        return _FakeCompleted(1, stderr="daemon exploded")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="daemon exploded"):
        DockerRuntime().drop_build_cache()


def test_build_image_raises_build_cache_corrupt_marker_on_cache_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the BuildKit cache-corrupt string appears in build output, the
    runtime must raise a RuntimeError starting with the shared
    BUILD_CACHE_CORRUPT_MARKER so the HTTP API surfaces the right error kind.
    """

    def fake_run(cmd, capture_output, text, timeout):  # type: ignore[no-untyped-def]
        return _FakeCompleted(
            1,
            stderr="oh no: content digest sha256:deadbeef: not found",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(RuntimeError) as exc_info:
        # temp_data_dir=None selects the non-streaming code path that uses
        # subprocess.run (easier to mock than the streaming Popen path).
        DockerRuntime().build_image("myapp", "/repo", "Dockerfile", temp_data_dir=None)

    assert str(exc_info.value).startswith("[BUILD_CACHE_CORRUPT]")


def test_get_container_status_returns_unknown_on_runtime_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(cmd, capture_output, text, timeout):  # type: ignore[no-untyped-def]
        return _FakeCompleted(1, stderr="no such container")

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert DockerRuntime().get_container_status("bogus") == "unknown"


def test_runtime_name_is_docker() -> None:
    """Used in log messages and status displays; keep it stable."""
    assert DockerRuntime().name == "docker"
