"""Tests that the ``containers`` module's free functions correctly delegate
to the active ``ContainerRuntime``.

The existing ``test_docker_cache`` and integration tests already cover the
Docker-runtime code path end-to-end; these tests specifically exercise the
indirection layer added by the runtime abstraction so regressions in it are
caught without spinning up Docker.
"""

from __future__ import annotations

from typing import Any

import compute_space.core.runtimes.factory as factory_mod
from compute_space.core import containers
from compute_space.core.runtimes.base import ContainerRuntime


class _RecordingRuntime:
    """ContainerRuntime that records every call so tests can assert on it.

    Not a subclass — it just satisfies the Protocol structurally, which is
    enough given we never call ``isinstance`` in production code.
    """

    name = "recording"

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def __getattr__(self, item: str):  # noqa: ANN204 - test helper
        def _fn(*args: Any, **kwargs: Any) -> str:
            self.calls.append((item, args, kwargs))
            # Each method in ContainerRuntime that returns something returns
            # either a string (image tag / container id / status / logs) or
            # None, so a string default is always a safe stand-in.
            return f"recorded:{item}"

        return _fn


def _patch_runtime(monkeypatch, runtime: ContainerRuntime) -> None:
    """Force get_runtime() to return ``runtime`` regardless of config."""
    monkeypatch.setattr(factory_mod, "get_runtime", lambda name=None: runtime)
    # containers.py imported get_runtime by name, so patch that binding too.
    monkeypatch.setattr(containers, "get_runtime", lambda name=None: runtime)


def test_build_image_delegates_to_runtime(monkeypatch) -> None:
    runtime = _RecordingRuntime()
    _patch_runtime(monkeypatch, runtime)

    result = containers.build_image("myapp", "/repo", "Dockerfile", temp_data_dir="/tmp/t")

    assert result == "recorded:build_image"
    assert runtime.calls == [
        ("build_image", ("myapp", "/repo", "Dockerfile"), {"temp_data_dir": "/tmp/t"}),
    ]


def test_stop_container_delegates_to_runtime(monkeypatch) -> None:
    runtime = _RecordingRuntime()
    _patch_runtime(monkeypatch, runtime)

    containers.stop_container("abc123")

    assert len(runtime.calls) == 1
    assert runtime.calls[0][0] == "stop_container"
    assert runtime.calls[0][1] == ("abc123",)


def test_remove_image_delegates_to_runtime(monkeypatch) -> None:
    runtime = _RecordingRuntime()
    _patch_runtime(monkeypatch, runtime)

    containers.remove_image("myapp")

    assert runtime.calls[0] == ("remove_image", ("myapp",), {})


def test_drop_docker_build_cache_delegates_to_runtime(monkeypatch) -> None:
    """The public function is still named drop_docker_build_cache for
    backwards compatibility, but it now dispatches through the runtime."""
    runtime = _RecordingRuntime()
    _patch_runtime(monkeypatch, runtime)

    result = containers.drop_docker_build_cache()

    assert result == "recorded:drop_build_cache"
    assert runtime.calls[0][0] == "drop_build_cache"


def test_get_container_status_delegates_to_runtime(monkeypatch) -> None:
    runtime = _RecordingRuntime()
    _patch_runtime(monkeypatch, runtime)

    status = containers.get_container_status("abc123")

    assert status == "recorded:get_container_status"
    assert runtime.calls[0] == ("get_container_status", ("abc123",), {})


def test_build_cache_corrupt_marker_is_exported() -> None:
    """Other modules (notably the HTTP API) rely on this marker to detect
    build-cache-corruption errors; make sure it stays importable."""
    assert containers.BUILD_CACHE_CORRUPT_MARKER == "[BUILD_CACHE_CORRUPT]"
