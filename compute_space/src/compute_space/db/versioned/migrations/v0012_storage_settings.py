"""v12: add the ``storage_settings`` table.  Body in v0012_storage_settings.sql."""

from __future__ import annotations

from compute_space.db.versioned.base import SqlFileMigration


class Migration0012StorageSettings(SqlFileMigration):
    version = 12
    sql_file = "v0012_storage_settings.sql"
