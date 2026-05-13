"""Tests for the ``/api/drop-docker-cache`` endpoint and its backing helper."""

import subprocess

import pytest
from litestar import Litestar
from litestar.di import Provide
from litestar.testing import AsyncTestClient

from compute_space.core.containers import drop_docker_build_cache
from compute_space.web.auth import api_system as system_routes


async def _user_stub() -> dict[str, str]:
    return {"sub": "owner", "username": "owner"}


def test_drop_docker_build_cache_runs_podman_image_prune(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: dict[str, object] = {}

    def fake_run(cmd, capture_output, text, timeout):  # type: ignore[no-untyped-def]
        calls["cmd"] = cmd
        calls["capture_output"] = capture_output
        calls["text"] = text
        calls["timeout"] = timeout
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="Total reclaimed space: 12.3MB\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    output = drop_docker_build_cache()
    assert output == "Total reclaimed space: 12.3MB"
    assert calls["cmd"] == ["podman", "image", "prune", "--all", "--force"]
    assert calls["capture_output"] is True
    assert calls["text"] is True
    assert calls["timeout"] == 120


def test_drop_docker_build_cache_raises_on_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(cmd, capture_output, text, timeout):  # type: ignore[no-untyped-def]
        return subprocess.CompletedProcess(args=cmd, returncode=1, stdout="", stderr="podman image prune error")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(RuntimeError, match="podman image prune error"):
        drop_docker_build_cache()


@pytest.mark.asyncio
async def test_drop_docker_cache_endpoint_success(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(system_routes, "drop_docker_build_cache", lambda: "Total reclaimed space: 12.3MB")
    app = Litestar(
        route_handlers=[system_routes.drop_docker_cache],
        dependencies={"user": Provide(_user_stub)},
        openapi_config=None,
    )
    async with AsyncTestClient(app=app) as client:
        resp = await client.post("/api/drop-docker-cache")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "output": "Total reclaimed space: 12.3MB"}


@pytest.mark.asyncio
async def test_drop_docker_cache_endpoint_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise_error() -> str:
        raise RuntimeError("podman engine error")

    monkeypatch.setattr(system_routes, "drop_docker_build_cache", _raise_error)
    app = Litestar(
        route_handlers=[system_routes.drop_docker_cache],
        dependencies={"user": Provide(_user_stub)},
        openapi_config=None,
    )
    async with AsyncTestClient(app=app) as client:
        resp = await client.post("/api/drop-docker-cache")
    assert resp.status_code == 500
    assert resp.json() == {"ok": False, "error": "podman engine error"}
