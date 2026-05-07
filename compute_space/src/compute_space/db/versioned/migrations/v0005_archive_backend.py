"""v5: add the ``archive_backend`` table.  Body in v0005_archive_backend.sql."""

from __future__ import annotations

from compute_space.db.versioned.base import SqlFileMigration


class Migration0005ArchiveBackend(SqlFileMigration):
    version = 5
    sql_file = "v0005_archive_backend.sql"
