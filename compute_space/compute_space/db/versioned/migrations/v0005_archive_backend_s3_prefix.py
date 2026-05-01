"""v5: add ``s3_prefix`` to the archive_backend row.

Implemented as a Python migration (not a ``.sql`` file) because we
need to be idempotent against the schema.sql baseline.  The legacy
v0 -> v1 bootstrap path replays ``schema.sql`` first (which already
includes the v5 column) and THEN runs every numbered migration in
sequence; a bare ``ALTER TABLE archive_backend ADD COLUMN s3_prefix
TEXT`` would then fail with ``duplicate column name``.  SQLite does
not support ``ADD COLUMN IF NOT EXISTS``, so we introspect the
table via ``PRAGMA table_info`` and skip the ALTER when the column
is already present.
"""

from __future__ import annotations

import sqlite3

from compute_space.db.versioned.base import Migration


class Migration0005ArchiveBackendS3Prefix(Migration):
    version = 5

    def up(self, db: sqlite3.Connection) -> None:
        # ``PRAGMA table_info`` returns one row per column; the
        # second element of each tuple is the column name.
        existing_columns = {
            row[1]
            for row in db.execute("PRAGMA table_info(archive_backend)").fetchall()
        }
        if "s3_prefix" in existing_columns:
            # Already present (legacy-bootstrap path that loaded
            # schema.sql which already declares the column).
            # Nothing to do; the version-stamp INSERT in
            # ``Migration.apply`` will record us as applied.
            return
        db.execute("ALTER TABLE archive_backend ADD COLUMN s3_prefix TEXT")
