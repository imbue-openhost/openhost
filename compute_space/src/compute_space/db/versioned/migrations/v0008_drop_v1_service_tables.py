"""v8: drop v1 ``service_providers`` and ``permissions`` tables.  Body in
v0008_drop_v1_service_tables.sql."""

from __future__ import annotations

from compute_space.db.versioned.base import SqlFileMigration


class Migration0008DropV1ServiceTables(SqlFileMigration):
    version = 8
    sql_file = "v0008_drop_v1_service_tables.sql"
