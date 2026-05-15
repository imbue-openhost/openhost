"""Tests for the host-prep fields on /api/settings/check_for_updates
and the HTTP 409 gate on /api/settings/update_repo_state.

The handlers are async Quart views; to exercise them in isolation we
call the underlying coroutine via `.__wrapped__` (bypassing the
login_required decorator) inside a test_request_context so the Quart
globals they depend on are populated.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from quart import Quart

import compute_space.web.routes.api.settings as settings_mod
from compute_space.core.containers import CONTAINER_RUNTIME_MISSING_ERROR
from compute_space.core.runtime_sentinel import HostPrepStatus


@pytest.mark.asyncio
async def test_check_for_updates_reports_podman_ok_and_sentinel_ok(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    async def fake_agent_fetch() -> dict[str, object]:
        return {"ok": True, "state": "UP_TO_DATE"}

    monkeypatch.setattr(settings_mod, "agent_fetch", fake_agent_fetch)
    monkeypatch.setattr(settings_mod, "container_runtime_available", lambda: True)
    monkeypatch.setattr(settings_mod, "host_prep_status", lambda: HostPrepStatus(True, "", "ok"))

    app = Quart(__name__)
    async with app.test_request_context("/api/settings/check_for_updates", method="POST"):
        response = await settings_mod.check_for_updates.__wrapped__()
        payload = await response.get_json()

    assert payload["ok"] is True
    assert payload["host_prep_ok"] is True
    assert payload["container_runtime_available"] is True
    assert "host_prep_reason" not in payload
    assert "host_prep_message" not in payload


@pytest.mark.asyncio
async def test_check_for_updates_reports_podman_missing_as_authoritative(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    async def fake_agent_fetch() -> dict[str, object]:
        return {"ok": True, "state": "BEHIND_REMOTE"}

    monkeypatch.setattr(settings_mod, "agent_fetch", fake_agent_fetch)
    monkeypatch.setattr(settings_mod, "container_runtime_available", lambda: False)
    monkeypatch.setattr(settings_mod, "host_prep_status", lambda: HostPrepStatus(True, "", "ok"))

    app = Quart(__name__)
    async with app.test_request_context("/api/settings/check_for_updates", method="POST"):
        response = await settings_mod.check_for_updates.__wrapped__()
        payload = await response.get_json()

    assert payload["ok"] is True
    assert payload["host_prep_ok"] is False
    assert payload["container_runtime_available"] is False
    assert payload["host_prep_reason"] == "container_runtime_missing"
    assert payload["host_prep_message"] == CONTAINER_RUNTIME_MISSING_ERROR


@pytest.mark.asyncio
async def test_check_for_updates_surfaces_sentinel_mismatch_when_podman_ok(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    async def fake_agent_fetch() -> dict[str, object]:
        return {"ok": True, "state": "BEHIND_REMOTE"}

    monkeypatch.setattr(settings_mod, "agent_fetch", fake_agent_fetch)
    monkeypatch.setattr(settings_mod, "container_runtime_available", lambda: True)
    monkeypatch.setattr(
        settings_mod,
        "host_prep_status",
        lambda: HostPrepStatus(False, "behind", "run system migrations to upgrade"),
    )

    app = Quart(__name__)
    async with app.test_request_context("/api/settings/check_for_updates", method="POST"):
        response = await settings_mod.check_for_updates.__wrapped__()
        payload = await response.get_json()

    assert payload["host_prep_ok"] is False
    assert payload["container_runtime_available"] is True
    assert payload["host_prep_reason"] == "behind"


@pytest.mark.asyncio
async def test_update_repo_state_refuses_with_409_when_podman_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    async def boom() -> dict[str, object]:
        raise AssertionError("agent_apply must not run when the gate fires")

    monkeypatch.setattr(settings_mod, "agent_apply", boom)
    monkeypatch.setattr(settings_mod, "container_runtime_available", lambda: False)
    monkeypatch.setattr(settings_mod, "host_prep_status", lambda: HostPrepStatus(True, "", "ok"))

    app = Quart(__name__)
    async with app.test_request_context("/api/settings/update_repo_state", method="POST"):
        result = await settings_mod.update_repo_state.__wrapped__()
    assert isinstance(result, tuple)
    response, status_code = result
    assert status_code == 409
    payload = await response.get_json()
    assert payload["ok"] is False
    assert payload["host_prep_reason"] == "container_runtime_missing"
    assert payload["host_prep_ok"] is False


@pytest.mark.asyncio
async def test_update_repo_state_refuses_with_409_when_sentinel_mismatched(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    async def boom() -> dict[str, object]:
        raise AssertionError("agent_apply must not run when the gate fires")

    monkeypatch.setattr(settings_mod, "agent_apply", boom)
    monkeypatch.setattr(settings_mod, "container_runtime_available", lambda: True)
    monkeypatch.setattr(
        settings_mod,
        "host_prep_status",
        lambda: HostPrepStatus(False, "behind", "migrations needed"),
    )

    app = Quart(__name__)
    async with app.test_request_context("/api/settings/update_repo_state", method="POST"):
        result = await settings_mod.update_repo_state.__wrapped__()
    assert isinstance(result, tuple)
    response, status_code = result
    assert status_code == 409
    payload = await response.get_json()
    assert payload["ok"] is False
    assert payload["host_prep_reason"] == "behind"


@pytest.mark.asyncio
async def test_update_repo_state_proceeds_when_host_prepared(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    called = {"n": 0}

    async def fake_apply() -> dict[str, object]:
        called["n"] += 1
        return {"ok": True, "ref": "abc1234", "system_migrations_applied": []}

    monkeypatch.setattr(settings_mod, "agent_apply", fake_apply)
    monkeypatch.setattr(settings_mod, "container_runtime_available", lambda: True)
    monkeypatch.setattr(settings_mod, "host_prep_status", lambda: HostPrepStatus(True, "", "ok"))

    app = Quart(__name__)
    async with app.test_request_context("/api/settings/update_repo_state", method="POST"):
        response = await settings_mod.update_repo_state.__wrapped__()
        payload = await response.get_json()

    assert called["n"] == 1
    assert payload == {"ok": True}
