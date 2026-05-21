"""Tests for the host-prep fields on /api/settings/check_for_updates
and the HTTP 409 gate on /api/settings/update_repo_state.

These protect the Docker → podman transition and any future runtime
upgrade: /api/settings/update_repo_state must refuse to apply updates
when the host isn't ready, and /api/settings/check_for_updates must
surface the exact reason/message the dashboard banner renders.

The handlers are Litestar route handlers — to exercise them in isolation
we call the underlying coroutine via ``handler.fn(...)``, passing the
``config`` dependency directly instead of going through Litestar's DI.
On the error path the handler raises ``HTTPException``; we inspect its
``status_code`` / ``detail`` / ``extra`` directly.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from typing import cast

import pytest
from litestar.exceptions import HTTPException

import compute_space.web.routes.api.settings as settings_mod
from compute_space.config import Config
from compute_space.core.containers import CONTAINER_RUNTIME_MISSING_ERROR
from compute_space.core.runtime_sentinel import HostPrepStatus
from compute_space.core.updates import GitState


def _fake_config(repo_path: Path) -> Config:
    """Minimal config-like stub.  The handlers only read ``openhost_repo_path``."""

    class _Cfg:
        openhost_repo_path = repo_path

    return cast(Config, _Cfg())


@pytest.mark.asyncio
async def test_check_for_updates_reports_podman_ok_and_sentinel_ok(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Happy path: both signals OK means ``host_prep_ok=True`` and the
    response is the ``CheckUpdatesOk`` variant (no reason/message fields)."""

    async def fake_check_git_state(_repo_path: Path) -> GitState:
        return GitState.UP_TO_DATE

    monkeypatch.setattr(settings_mod, "check_git_state", fake_check_git_state)
    monkeypatch.setattr(settings_mod, "container_runtime_available", lambda: True)
    monkeypatch.setattr(settings_mod, "host_prep_status", lambda: HostPrepStatus(True, "", "ok"))

    result = await settings_mod.check_for_updates.fn(config=_fake_config(tmp_path))

    assert isinstance(result, settings_mod.CheckUpdatesOk)
    assert result.host_prep_ok is True
    assert result.container_runtime_available is True


@pytest.mark.asyncio
async def test_check_for_updates_reports_podman_missing_as_authoritative(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """When podman is missing, the response must say so with the
    shared CONTAINER_RUNTIME_MISSING_ERROR text — even if the sentinel happened
    to claim the host is prepared.  The live probe is the authoritative
    signal for the Docker → podman transition because pre-upgrade hosts
    never had sentinels."""

    async def fake_check_git_state(_repo_path: Path) -> GitState:
        return GitState.BEHIND_REMOTE

    monkeypatch.setattr(settings_mod, "check_git_state", fake_check_git_state)
    monkeypatch.setattr(settings_mod, "container_runtime_available", lambda: False)
    # Sentinel says "all good" but podman is actually missing — the
    # response must STILL surface the podman-missing reason.
    monkeypatch.setattr(settings_mod, "host_prep_status", lambda: HostPrepStatus(True, "", "ok"))

    result = await settings_mod.check_for_updates.fn(config=_fake_config(tmp_path))

    assert isinstance(result, settings_mod.CheckUpdatesBlocked)
    assert result.host_prep_ok is False
    assert result.container_runtime_available is False
    assert result.host_prep_reason == "container_runtime_missing"
    assert result.host_prep_message == CONTAINER_RUNTIME_MISSING_ERROR


@pytest.mark.asyncio
async def test_check_for_updates_surfaces_sentinel_mismatch_when_podman_ok(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """When podman IS installed but the sentinel reports the wrong
    runtime_version (future-upgrade scenario), the response must carry
    the sentinel's specific reason/message so the banner explains what
    to do."""

    async def fake_check_git_state(_repo_path: Path) -> GitState:
        return GitState.BEHIND_REMOTE

    monkeypatch.setattr(settings_mod, "check_git_state", fake_check_git_state)
    monkeypatch.setattr(settings_mod, "container_runtime_available", lambda: True)
    monkeypatch.setattr(
        settings_mod,
        "host_prep_status",
        lambda: HostPrepStatus(False, "wrong_version", "re-run ansible to bump runtime_version"),
    )

    result = await settings_mod.check_for_updates.fn(config=_fake_config(tmp_path))

    assert isinstance(result, settings_mod.CheckUpdatesBlocked)
    assert result.host_prep_ok is False
    assert result.container_runtime_available is True
    assert result.host_prep_reason == "wrong_version"
    assert "ansible" in result.host_prep_message


@pytest.mark.asyncio
async def test_update_repo_state_refuses_with_409_when_podman_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The server-side safety gate: even if the dashboard banner is
    bypassed (direct curl, older cached page, CLI), update_repo_state
    must refuse with 409 and NOT touch the git working tree when the
    host isn't prepared."""

    async def boom(*_a: Any, **_kw: Any) -> Any:
        raise AssertionError("hard_checkout_and_validate must not run when the gate fires")

    monkeypatch.setattr(settings_mod, "hard_checkout_and_validate", boom)
    monkeypatch.setattr(settings_mod, "container_runtime_available", lambda: False)
    monkeypatch.setattr(settings_mod, "host_prep_status", lambda: HostPrepStatus(True, "", "ok"))

    with pytest.raises(HTTPException) as excinfo:
        await settings_mod.update_repo_state.fn(config=_fake_config(tmp_path))
    assert excinfo.value.status_code == 409
    assert excinfo.value.extra is not None
    extra = cast(dict[str, Any], excinfo.value.extra)
    assert extra["host_prep_reason"] == "container_runtime_missing"
    assert extra["host_prep_ok"] is False


@pytest.mark.asyncio
async def test_update_repo_state_refuses_with_409_when_sentinel_mismatched(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The sentinel half of the gate: podman is present but the host
    hasn't been re-prepped for this runtime_version."""

    async def boom(*_a: Any, **_kw: Any) -> Any:
        raise AssertionError("hard_checkout_and_validate must not run when the gate fires")

    monkeypatch.setattr(settings_mod, "hard_checkout_and_validate", boom)
    monkeypatch.setattr(settings_mod, "container_runtime_available", lambda: True)
    monkeypatch.setattr(
        settings_mod,
        "host_prep_status",
        lambda: HostPrepStatus(False, "wrong_version", "bump required"),
    )

    with pytest.raises(HTTPException) as excinfo:
        await settings_mod.update_repo_state.fn(config=_fake_config(tmp_path))
    assert excinfo.value.status_code == 409
    assert excinfo.value.extra is not None
    extra = cast(dict[str, Any], excinfo.value.extra)
    assert extra["host_prep_reason"] == "wrong_version"


@pytest.mark.asyncio
async def test_update_repo_state_proceeds_when_host_prepared(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """When both signals say the host is ready, update_repo_state must
    actually invoke the git checkout rather than spuriously blocking."""

    called = {"n": 0}

    async def fake_get_current_ref(_repo_path: Path) -> str:
        return "main"

    async def fake_checkout(_repo_path: Path, _ref: str) -> None:
        called["n"] += 1

    monkeypatch.setattr(settings_mod, "get_current_ref", fake_get_current_ref)
    monkeypatch.setattr(settings_mod, "hard_checkout_and_validate", fake_checkout)
    monkeypatch.setattr(settings_mod, "container_runtime_available", lambda: True)
    monkeypatch.setattr(settings_mod, "host_prep_status", lambda: HostPrepStatus(True, "", "ok"))

    # No exception on the happy path, and the checkout actually ran.
    await settings_mod.update_repo_state.fn(config=_fake_config(tmp_path))
    assert called["n"] == 1
