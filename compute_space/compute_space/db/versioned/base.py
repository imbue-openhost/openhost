"""Base classes for versioned migrations."""

from __future__ import annotations

import inspect
import sqlite3
from pathlib import Path
from typing import ClassVar

# DDL for the single-row schema-version metadata table. Kept here so both
# the runner and the legacy migrate() import the exact same string.
SCHEMA_VERSION_DDL = (
    "CREATE TABLE IF NOT EXISTS schema_version (id INTEGER PRIMARY KEY CHECK (id = 1), version INTEGER NOT NULL)"
)


class Migration:
    """Base class for a single numbered schema migration.

    Subclasses set ``version`` (the target version after the migration
    runs — strictly greater than the source version) and implement
    :meth:`up`. :meth:`down` is optional and only needed for manual
    dev/test iteration.
    """

    version: ClassVar[int] = 0

    def up(self, db: sqlite3.Connection) -> None:
        raise NotImplementedError

    def down(self, db: sqlite3.Connection) -> None:
        raise NotImplementedError(f"Migration v{self.version} does not define down()")


class SqlFileMigration(Migration):
    """Migration that runs a sibling ``.sql`` file as its ``up`` step.

    ``sql_file`` is resolved relative to the directory of the subclass's
    source file, so the .sql lives next to the Python migration.
    """

    sql_file: ClassVar[str] = ""

    def up(self, db: sqlite3.Connection) -> None:
        module_file = inspect.getfile(self.__class__)
        sql_path = Path(module_file).resolve().parent / self.sql_file
        sql = sql_path.read_text()
        execute_sql_script(db, sql)


def execute_sql_script(db: sqlite3.Connection, sql: str) -> None:
    """Execute multi-statement SQL without the implicit COMMIT of ``executescript``.

    ``sqlite3.Connection.executescript`` commits the current transaction
    before executing — which would release an outer ``BEGIN EXCLUSIVE`` lock
    and break transactional atomicity for migrations. This helper splits
    the script into statements and calls ``db.execute`` on each, so it
    runs inside whatever transaction the caller has open.
    """
    for stmt in _iter_sql_statements(sql):
        if _is_effectively_empty(stmt):
            continue
        db.execute(stmt)


def _iter_sql_statements(sql: str) -> list[str]:
    """Split a SQL script into complete statements.

    Uses :func:`sqlite3.complete_statement` as the boundary oracle, which
    correctly handles quoted strings and block comments inside DDL.
    """
    statements: list[str] = []
    buffer = ""
    for line in sql.splitlines(keepends=True):
        buffer += line
        if sqlite3.complete_statement(buffer):
            statements.append(buffer)
            buffer = ""
    trailing = buffer.strip()
    if trailing:
        statements.append(buffer)
    return statements


def _is_effectively_empty(stmt: str) -> bool:
    """True if a statement chunk is whitespace or pure ``--`` line comments."""
    for line in stmt.splitlines():
        s = line.strip()
        if not s or s.startswith("--"):
            continue
        return False
    return True
