"""v4: add the ``archive_backend`` table.

See ``v0004_archive_backend.sql`` for the body.
"""

from __future__ import annotations

from compute_space.db.versioned.base import SqlFileMigration


class Migration0004ArchiveBackend(SqlFileMigration):
    version = 4
    sql_file = "v0004_archive_backend.sql"
