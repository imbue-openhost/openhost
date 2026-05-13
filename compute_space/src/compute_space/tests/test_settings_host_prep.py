"""Tests for the host-prep fields on /api/settings/check_for_updates and the HTTP 409 gate on update_repo_state."""

from __future__ import annotations

from pathlib import Path

import pytest
from litestar import Litestar
from litestar.di import Provide
from litestar.testing import AsyncTestClient

import compute_space.web.routes.api.settings as settings_mod
from compute_space.config import set_active_config
from compute_space.core.containers import CONTAINER_RUNTIME_MISSING_ERROR
from compute_space.core.runtime_sentinel import HostPrepStatus
from compute_space.core.updates import GitState


async def _user_stub() -> dict[str, str]:
    return {"sub": "owner", "username": "owner"}


def _make_app_with_repo(repo_path: Path) -> Litestar:
    class _Cfg:
        openhost_repo_path = repo_path

    set_active_config(_Cfg())  # type: ignore[arg-type]
    return Litestar(
        route_handlers=[settings_mod.check_for_updates, settings_mod.update_repo_state],
        dependencies={"user": Provide(_user_stub)},
        openapi_config=None,
    )


@pytest.mark.asyncio
async def test_check_for_updates_reports_podman_ok_and_sentinel_ok(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    async def fake_check_git_state(_repo_path):
        return GitState.UP_TO_DATE

    monkeypatch.setattr(settings_mod, "check_git_state", fake_check_git_state)
    monkeypatch.setattr(settings_mod, "container_runtime_available", lambda: True)
    monkeypatch.setattr(settings_mod, "host_prep_status", lambda: HostPrepStatus(True, "", "ok"))

    app = _make_app_with_repo(tmp_path)
    async with AsyncTestClient(app=app) as client:
        resp = await client.post("/api/settings/check_for_updates")
    payload = resp.json()
    assert payload["ok"] is True
    assert payload["host_prep_ok"] is True
    assert payload["container_runtime_available"] is True
    assert "host_prep_reason" not in payload
    assert "host_prep_message" not in payload


@pytest.mark.asyncio
async def test_check_for_updates_reports_podman_missing_as_authoritative(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    async def fake_check_git_state(_repo_path):
        return GitState.BEHIND_REMOTE

    monkeypatch.setattr(settings_mod, "check_git_state", fake_check_git_state)
    monkeypatch.setattr(settings_mod, "container_runtime_available", lambda: False)
    monkeypatch.setattr(settings_mod, "host_prep_status", lambda: HostPrepStatus(True, "", "ok"))

    app = _make_app_with_repo(tmp_path)
    async with AsyncTestClient(app=app) as client:
        resp = await client.post("/api/settings/check_for_updates")
    payload = resp.json()
    assert payload["ok"] is True
    assert payload["host_prep_ok"] is False
    assert payload["container_runtime_available"] is False
    assert payload["host_prep_reason"] == "container_runtime_missing"
    assert payload["host_prep_message"] == CONTAINER_RUNTIME_MISSING_ERROR


@pytest.mark.asyncio
async def test_check_for_updates_surfaces_sentinel_mismatch_when_podman_ok(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    async def fake_check_git_state(_repo_path):
        return GitState.BEHIND_REMOTE

    monkeypatch.setattr(settings_mod, "check_git_state", fake_check_git_state)
    monkeypatch.setattr(settings_mod, "container_runtime_available", lambda: True)
    monkeypatch.setattr(
        settings_mod,
        "host_prep_status",
        lambda: HostPrepStatus(False, "wrong_version", "re-run ansible to bump runtime_version"),
    )

    app = _make_app_with_repo(tmp_path)
    async with AsyncTestClient(app=app) as client:
        resp = await client.post("/api/settings/check_for_updates")
    payload = resp.json()
    assert payload["host_prep_ok"] is False
    assert payload["container_runtime_available"] is True
    assert payload["host_prep_reason"] == "wrong_version"
    assert "ansible" in payload["host_prep_message"]


@pytest.mark.asyncio
async def test_update_repo_state_refuses_with_409_when_podman_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    async def boom(*_a, **_kw):
        raise AssertionError("hard_checkout_and_validate must not run when the gate fires")

    monkeypatch.setattr(settings_mod, "hard_checkout_and_validate", boom)
    monkeypatch.setattr(settings_mod, "container_runtime_available", lambda: False)
    monkeypatch.setattr(settings_mod, "host_prep_status", lambda: HostPrepStatus(True, "", "ok"))

    app = _make_app_with_repo(tmp_path)
    async with AsyncTestClient(app=app) as client:
        resp = await client.post("/api/settings/update_repo_state")
    assert resp.status_code == 409
    payload = resp.json()
    assert payload["ok"] is False
    assert payload["host_prep_reason"] == "container_runtime_missing"
    assert payload["host_prep_ok"] is False


@pytest.mark.asyncio
async def test_update_repo_state_refuses_with_409_when_sentinel_mismatched(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    async def boom(*_a, **_kw):
        raise AssertionError("hard_checkout_and_validate must not run when the gate fires")

    monkeypatch.setattr(settings_mod, "hard_checkout_and_validate", boom)
    monkeypatch.setattr(settings_mod, "container_runtime_available", lambda: True)
    monkeypatch.setattr(
        settings_mod,
        "host_prep_status",
        lambda: HostPrepStatus(False, "wrong_version", "bump required"),
    )

    app = _make_app_with_repo(tmp_path)
    async with AsyncTestClient(app=app) as client:
        resp = await client.post("/api/settings/update_repo_state")
    assert resp.status_code == 409
    payload = resp.json()
    assert payload["ok"] is False
    assert payload["host_prep_reason"] == "wrong_version"


@pytest.mark.asyncio
async def test_update_repo_state_proceeds_when_host_prepared(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    called = {"n": 0}

    async def fake_get_current_ref(_repo_path):
        return "main"

    async def fake_checkout(_repo_path, _ref):
        called["n"] += 1

    monkeypatch.setattr(settings_mod, "get_current_ref", fake_get_current_ref)
    monkeypatch.setattr(settings_mod, "hard_checkout_and_validate", fake_checkout)
    monkeypatch.setattr(settings_mod, "container_runtime_available", lambda: True)
    monkeypatch.setattr(settings_mod, "host_prep_status", lambda: HostPrepStatus(True, "", "ok"))

    app = _make_app_with_repo(tmp_path)
    async with AsyncTestClient(app=app) as client:
        resp = await client.post("/api/settings/update_repo_state")
    payload = resp.json()
    assert called["n"] == 1
    assert payload == {"ok": True}
