from __future__ import annotations

import json
from pathlib import Path

import pytest

import openhost_system_agent.status as status_mod
from openhost_system_agent.migrations.base import SystemMigration
from openhost_system_agent.status import get_migration_status


def _write_jsonl(path: Path, entries: list[dict[str, object]]) -> None:
    path.write_text("".join(json.dumps(e) + "\n" for e in entries))


def test_matching_version_is_ok(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(status_mod, "REGISTRY", [])
    migrations = tmp_path / "migrations.jsonl"
    _write_jsonl(migrations, [{"version": 1, "success": True, "error": None}])
    result = get_migration_status(str(migrations))
    assert result.ok is True
    assert result.reason == ""


def test_missing_file(tmp_path: Path) -> None:
    result = get_migration_status(str(tmp_path / "does-not-exist"))
    assert result.ok is False
    assert result.reason == "missing"


def test_empty_file(tmp_path: Path) -> None:
    migrations = tmp_path / "migrations.jsonl"
    migrations.write_text("")
    result = get_migration_status(str(migrations))
    assert result.ok is False
    assert result.reason == "missing"


def test_behind_version(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeMigration(SystemMigration):
        version = 2

        def up(self) -> None:
            pass

    monkeypatch.setattr(status_mod, "REGISTRY", [FakeMigration()])

    migrations = tmp_path / "migrations.jsonl"
    _write_jsonl(migrations, [{"version": 1, "success": True, "error": None}])
    result = get_migration_status(str(migrations))
    assert result.ok is False
    assert result.reason == "behind"
    assert result.current_host_version == 1
    assert result.expected_version == 2


def test_ahead_version(tmp_path: Path) -> None:
    migrations = tmp_path / "migrations.jsonl"
    _write_jsonl(
        migrations,
        [
            {"version": 1, "success": True, "error": None},
            {"version": 99, "success": True, "error": None},
        ],
    )
    result = get_migration_status(str(migrations))
    assert result.ok is False
    assert result.reason == "ahead"


def test_failed_entries_are_ignored(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(status_mod, "REGISTRY", [])
    migrations = tmp_path / "migrations.jsonl"
    _write_jsonl(
        migrations,
        [
            {"version": 1, "success": True, "error": None},
            {"version": 2, "success": False, "error": "boom"},
        ],
    )
    result = get_migration_status(str(migrations))
    assert result.ok is True


def test_malformed_lines_are_skipped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(status_mod, "REGISTRY", [])
    migrations = tmp_path / "migrations.jsonl"
    migrations.write_text('not json\n{"version": 1, "success": true, "error": null}\n\n')
    result = get_migration_status(str(migrations))
    assert result.ok is True
