from __future__ import annotations

from pathlib import Path

from openhost_system_agent.migrations.migration_log import MigrationLogEntry
from openhost_system_agent.migrations.migration_log import append_entry
from openhost_system_agent.migrations.migration_log import read_log


def test_append_inserts_newline_if_file_missing_one(tmp_path: Path) -> None:
    """An external writer (ansible, manual edit) may leave the file without a
    trailing newline; appending must not concatenate two JSON objects."""
    log = tmp_path / "migrations.jsonl"
    log.write_text('{"version":1,"timestamp":"t1","success":true,"error":null}')

    append_entry(str(log), MigrationLogEntry(version=2, timestamp="t2", success=True, error=None))

    entries = read_log(str(log))
    assert [e.version for e in entries] == [1, 2]
    assert log.read_text().count("\n") == 2


def test_append_does_not_double_newline(tmp_path: Path) -> None:
    log = tmp_path / "migrations.jsonl"
    log.write_text('{"version":1,"timestamp":"t1","success":true,"error":null}\n')

    append_entry(str(log), MigrationLogEntry(version=2, timestamp="t2", success=True, error=None))

    assert log.read_text().count("\n") == 2
