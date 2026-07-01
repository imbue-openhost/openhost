"""Tests for /api/settings/check_for_updates and the HTTP 409 gate on
/api/settings/apply_update.

The handlers are Litestar route handlers — to exercise them in isolation
we call the underlying coroutine via ``handler.fn(...)``.
On the error path the handler raises ``HTTPException``; we inspect its
``status_code`` / ``detail`` directly.
"""

from __future__ import annotations

import pytest
from litestar.exceptions import HTTPException

import compute_space.web.routes.api.settings as settings_mod
from compute_space.core.system_agent import SystemAgentError
from openhost_system_agent.protocol import ApplyResult
from openhost_system_agent.protocol import FetchResult
from openhost_system_agent.protocol import MigrationStatus


@pytest.mark.asyncio
async def test_check_for_updates_up_to_date(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_fetch() -> FetchResult:
        return FetchResult(state="UP_TO_DATE")

    async def fake_status() -> MigrationStatus:
        return MigrationStatus(ok=True, reason="", message="ok", current_host_version=1, expected_version=1)

    monkeypatch.setattr(settings_mod, "system_agent_fetch", fake_fetch)
    monkeypatch.setattr(settings_mod, "system_agent_status", fake_status)

    result = await settings_mod.check_for_updates.fn()

    assert result.state == "UP_TO_DATE"
    assert result.error is None


@pytest.mark.asyncio
async def test_check_for_updates_update_available(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_fetch() -> FetchResult:
        return FetchResult(state="BEHIND_REMOTE")

    async def fake_status() -> MigrationStatus:
        return MigrationStatus(ok=True, reason="", message="ok", current_host_version=1, expected_version=1)

    monkeypatch.setattr(settings_mod, "system_agent_fetch", fake_fetch)
    monkeypatch.setattr(settings_mod, "system_agent_status", fake_status)

    result = await settings_mod.check_for_updates.fn()

    assert result.state == "UPDATE_AVAILABLE"
    assert result.error is None


@pytest.mark.asyncio
async def test_check_for_updates_migration_behind_is_update_available(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_fetch() -> FetchResult:
        return FetchResult(state="BEHIND_REMOTE")

    async def fake_status() -> MigrationStatus:
        return MigrationStatus(
            ok=False, reason="behind", message="run system migrations", current_host_version=1, expected_version=2
        )

    monkeypatch.setattr(settings_mod, "system_agent_fetch", fake_fetch)
    monkeypatch.setattr(settings_mod, "system_agent_status", fake_status)

    result = await settings_mod.check_for_updates.fn()

    assert result.state == "UPDATE_AVAILABLE"
    assert result.error == "run system migrations"


@pytest.mark.asyncio
async def test_check_for_updates_detached_head_is_update_available_with_notice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_fetch() -> FetchResult:
        return FetchResult(state="DETACHED_HEAD")

    async def fake_status() -> MigrationStatus:
        return MigrationStatus(ok=True, reason="", message="ok", current_host_version=1, expected_version=1)

    monkeypatch.setattr(settings_mod, "system_agent_fetch", fake_fetch)
    monkeypatch.setattr(settings_mod, "system_agent_status", fake_status)

    result = await settings_mod.check_for_updates.fn()

    assert result.state == "UPDATE_AVAILABLE"
    assert result.error is not None
    assert "detached HEAD" in result.error


@pytest.mark.asyncio
async def test_check_for_updates_migration_missing_is_error(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_fetch() -> FetchResult:
        return FetchResult(state="UP_TO_DATE")

    async def fake_status() -> MigrationStatus:
        return MigrationStatus(
            ok=False, reason="missing", message="missing migration log", current_host_version=0, expected_version=2
        )

    monkeypatch.setattr(settings_mod, "system_agent_fetch", fake_fetch)
    monkeypatch.setattr(settings_mod, "system_agent_status", fake_status)

    result = await settings_mod.check_for_updates.fn()

    assert result.state == "ERROR"
    assert result.error == "missing migration log"


@pytest.mark.asyncio
async def test_check_for_updates_agent_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_fetch() -> FetchResult:
        raise SystemAgentError("agent down")

    monkeypatch.setattr(settings_mod, "system_agent_fetch", fake_fetch)

    result = await settings_mod.check_for_updates.fn()

    assert result.state == "ERROR"
    assert "agent down" in (result.error or "")


@pytest.mark.asyncio
async def test_apply_update_refuses_with_409_when_not_prepared(monkeypatch: pytest.MonkeyPatch) -> None:
    async def boom() -> None:
        raise AssertionError("system_agent_apply must not run when the gate fires")

    async def fake_status() -> MigrationStatus:
        return MigrationStatus(
            ok=False, reason="missing", message="migration log missing", current_host_version=0, expected_version=2
        )

    monkeypatch.setattr(settings_mod, "system_agent_apply", boom)
    monkeypatch.setattr(settings_mod, "system_agent_status", fake_status)

    with pytest.raises(HTTPException) as excinfo:
        await settings_mod.apply_update.fn()
    assert excinfo.value.status_code == 409


@pytest.mark.asyncio
async def test_apply_update_proceeds_when_migration_behind(monkeypatch: pytest.MonkeyPatch) -> None:
    called = {"n": 0}

    async def fake_apply() -> ApplyResult:
        called["n"] += 1
        return ApplyResult(ref="abc1234", system_migrations_applied=[2], already_up_to_date=False)

    async def fake_status() -> MigrationStatus:
        return MigrationStatus(
            ok=False, reason="behind", message="migrations needed", current_host_version=1, expected_version=2
        )

    monkeypatch.setattr(settings_mod, "system_agent_apply", fake_apply)
    monkeypatch.setattr(settings_mod, "system_agent_status", fake_status)

    await settings_mod.apply_update.fn()
    assert called["n"] == 1


@pytest.mark.asyncio
async def test_apply_update_proceeds_when_prepared(monkeypatch: pytest.MonkeyPatch) -> None:
    called = {"n": 0}

    async def fake_apply() -> ApplyResult:
        called["n"] += 1
        return ApplyResult(ref="abc1234", system_migrations_applied=[], already_up_to_date=False)

    async def fake_status() -> MigrationStatus:
        return MigrationStatus(ok=True, reason="", message="ok", current_host_version=1, expected_version=1)

    monkeypatch.setattr(settings_mod, "system_agent_apply", fake_apply)
    monkeypatch.setattr(settings_mod, "system_agent_status", fake_status)

    await settings_mod.apply_update.fn()
    assert called["n"] == 1
