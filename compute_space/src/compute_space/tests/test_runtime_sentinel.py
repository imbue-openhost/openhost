from __future__ import annotations

import json
from pathlib import Path

from compute_space.core.runtime_sentinel import host_prep_status


def _write_jsonl(path: Path, entries: list[dict[str, object]]) -> None:
    path.write_text("".join(json.dumps(e) + "\n" for e in entries))


def test_matching_version_is_ok(tmp_path: Path) -> None:
    migrations = tmp_path / "migrations.jsonl"
    _write_jsonl(migrations, [{"version": 1, "success": True, "error": None}])
    status = host_prep_status(str(migrations))
    assert status.ok is True
    assert status.reason == ""


def test_missing_file_is_not_ok(tmp_path: Path) -> None:
    status = host_prep_status(str(tmp_path / "does-not-exist"))
    assert status.ok is False
    assert status.reason == "missing"


def test_empty_file_is_not_ok(tmp_path: Path) -> None:
    migrations = tmp_path / "migrations.jsonl"
    migrations.write_text("")
    status = host_prep_status(str(migrations))
    assert status.ok is False
    assert status.reason == "missing"


def test_behind_version_is_not_ok(tmp_path: Path, monkeypatch: "pytest.MonkeyPatch") -> None:
    from compute_space.core import runtime_sentinel

    from openhost_system_agent.migrations.base import SystemMigration

    class FakeMigration(SystemMigration):
        version = 2

        def up(self) -> None:
            pass

    monkeypatch.setattr(runtime_sentinel, "REGISTRY", [FakeMigration()])

    migrations = tmp_path / "migrations.jsonl"
    _write_jsonl(migrations, [{"version": 1, "success": True, "error": None}])
    status = host_prep_status(str(migrations))
    assert status.ok is False
    assert status.reason == "behind"


def test_ahead_version_is_not_ok(tmp_path: Path) -> None:
    migrations = tmp_path / "migrations.jsonl"
    _write_jsonl(
        migrations,
        [
            {"version": 1, "success": True, "error": None},
            {"version": 99, "success": True, "error": None},
        ],
    )
    status = host_prep_status(str(migrations))
    assert status.ok is False
    assert status.reason == "ahead"


def test_failed_entries_are_ignored_for_version(tmp_path: Path) -> None:
    migrations = tmp_path / "migrations.jsonl"
    _write_jsonl(
        migrations,
        [
            {"version": 1, "success": True, "error": None},
            {"version": 2, "success": False, "error": "boom"},
        ],
    )
    status = host_prep_status(str(migrations))
    assert status.ok is True


def test_malformed_lines_are_skipped(tmp_path: Path) -> None:
    migrations = tmp_path / "migrations.jsonl"
    migrations.write_text('not json\n{"version": 1, "success": true, "error": null}\n\n')
    status = host_prep_status(str(migrations))
    assert status.ok is True
