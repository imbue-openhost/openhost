"""v5: add the ``archive_backend`` table.

See ``v0005_archive_backend.sql`` for the body.

(Originally numbered v4 on the parent ``andrew/app-archive-dashboard``
branch; renumbered to v5 when this branch merged ``main``, which had
shipped its own ``v0004_apps_removing_status``.)
"""

from __future__ import annotations

from compute_space.db.versioned.base import SqlFileMigration


class Migration0005ArchiveBackend(SqlFileMigration):
    version = 5
    sql_file = "v0005_archive_backend.sql"
