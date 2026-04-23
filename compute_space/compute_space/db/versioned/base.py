"""Base classes for versioned migrations.

Transaction contract
--------------------

Each :class:`Migration` owns its own transaction. :meth:`Migration.apply`
is the public entry point the runner calls; it wraps :meth:`up` in a
single ``BEGIN EXCLUSIVE`` + version-bump + ``COMMIT`` so "migration
applied" and "schema_version bumped" are atomic.

Migration authors MUST NOT call ``db.commit()`` or issue ``BEGIN`` /
``COMMIT`` / ``ROLLBACK`` inside :meth:`up` — doing so breaks the
atomicity guarantee by ending the transaction that :meth:`apply` opened.
This rule is documented and trusted rather than enforced; we don't try
to police it.

:class:`SqlFileMigration` runs a sibling ``.sql`` file. It overrides
:meth:`apply` to wrap the file contents in ``BEGIN EXCLUSIVE`` + the
file's SQL + version bump + ``COMMIT`` and then hands that script to
:meth:`sqlite3.Connection.executescript`. The same "no BEGIN/COMMIT
inside the file" rule applies to the .sql — the wrapper owns the tx.
"""

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
    :meth:`up`. :meth:`down` is optional and only useful for manual
    dev/test iteration.

    Do NOT commit inside :meth:`up`. See the module docstring for the
    transaction contract.
    """

    version: ClassVar[int] = 0

    def up(self, db: sqlite3.Connection) -> None:
        raise NotImplementedError

    def down(self, db: sqlite3.Connection) -> None:
        raise NotImplementedError(f"Migration v{self.version} does not define down()")

    def apply(self, db: sqlite3.Connection) -> None:
        """Atomically apply ``up(db)`` and bump schema_version in one tx.

        The caller must already hold the process-level migration lock.
        ``db`` must be in autocommit mode (``isolation_level=None``) so
        that the explicit ``BEGIN EXCLUSIVE`` below is not short-circuited
        by Python's implicit-transaction handling.
        """
        db.execute("BEGIN EXCLUSIVE")
        try:
            self.up(db)
            db.execute(
                "INSERT OR REPLACE INTO schema_version (id, version) VALUES (1, ?)",
                (self.version,),
            )
            db.execute("COMMIT")
        except Exception:
            try:
                db.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise


class SqlFileMigration(Migration):
    """Migration whose body is a sibling ``.sql`` file.

    ``sql_file`` is resolved relative to the directory of the subclass's
    source file, so the ``.sql`` lives next to its Python wrapper.

    :meth:`up` is unused on this subclass — :meth:`apply` reads the file
    and drives everything via a single :meth:`sqlite3.Connection.executescript`
    call. The .sql MUST NOT contain ``BEGIN``, ``COMMIT`` or ``ROLLBACK``;
    :meth:`apply` wraps the file in its own ``BEGIN EXCLUSIVE`` ... ``COMMIT``.
    """

    sql_file: ClassVar[str] = ""

    def up(self, db: sqlite3.Connection) -> None:
        # Not used — apply() handles everything via executescript.
        raise NotImplementedError("SqlFileMigration drives execution through apply(), not up()")

    def apply(self, db: sqlite3.Connection) -> None:
        sql = self._load_sql()
        wrapped = (
            "BEGIN EXCLUSIVE;\n"
            f"{sql}\n"
            f"INSERT OR REPLACE INTO schema_version (id, version) VALUES (1, {int(self.version)});\n"
            "COMMIT;\n"
        )
        try:
            db.executescript(wrapped)
        except Exception:
            try:
                db.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise

    def _load_sql(self) -> str:
        module_file = inspect.getfile(self.__class__)
        sql_path = Path(module_file).resolve().parent / self.sql_file
        return sql_path.read_text()
