"""v2 marker migration: no schema change; bumps schema_version via the
SqlFileMigration wrapper's version-bump statement.
"""

from __future__ import annotations

from compute_space.db.versioned.base import SqlFileMigration


class Migration0002Noop(SqlFileMigration):
    version = 2
    sql_file = "0002_noop.sql"
