"""Tests for the ``/api/drop-docker-cache`` endpoint and its backing helper.

The endpoint runs ``podman image prune --all --force`` via
``drop_docker_build_cache`` in compute_space.core.containers.
"""

import subprocess

import pytest
from quart import Quart

from compute_space.core.containers import drop_docker_build_cache
from compute_space.web.routes.api import system as system_routes


def test_drop_docker_build_cache_runs_podman_image_prune(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: dict[str, object] = {}

    def fake_run(cmd: list[str], capture_output: bool, text: bool, timeout: int) -> subprocess.CompletedProcess[str]:
        calls["cmd"] = cmd
        calls["capture_output"] = capture_output
        calls["text"] = text
        calls["timeout"] = timeout
        return subprocess.CompletedProcess(
            args=cmd,
            returncode=0,
            stdout="Total reclaimed space: 12.3MB\n",
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    output = drop_docker_build_cache()

    assert output == "Total reclaimed space: 12.3MB"
    assert calls["cmd"] == ["podman", "image", "prune", "--all", "--force"]
    assert calls["capture_output"] is True
    assert calls["text"] is True
    assert calls["timeout"] == 120


def test_drop_docker_build_cache_raises_on_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(cmd: list[str], capture_output: bool, text: bool, timeout: int) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=cmd,
            returncode=1,
            stdout="",
            stderr="podman image prune error",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="podman image prune error"):
        drop_docker_build_cache()


@pytest.mark.asyncio
async def test_drop_docker_cache_endpoint_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = Quart(__name__)
    monkeypatch.setattr(
        system_routes,
        "drop_docker_build_cache",
        lambda: "Total reclaimed space: 12.3MB",
    )

    async with app.app_context():
        response = system_routes.drop_docker_cache.__wrapped__()  # type: ignore[attr-defined]
        assert response.status_code == 200
        payload = await response.get_json()

    assert payload == {"ok": True, "output": "Total reclaimed space: 12.3MB"}


@pytest.mark.asyncio
async def test_drop_docker_cache_endpoint_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = Quart(__name__)

    def _raise_error() -> str:
        raise RuntimeError("podman engine error")

    monkeypatch.setattr(system_routes, "drop_docker_build_cache", _raise_error)

    async with app.app_context():
        response, status_code = system_routes.drop_docker_cache.__wrapped__()  # type: ignore[attr-defined]
        assert status_code == 500
        payload = await response.get_json()

    assert payload == {"ok": False, "error": "podman engine error"}
