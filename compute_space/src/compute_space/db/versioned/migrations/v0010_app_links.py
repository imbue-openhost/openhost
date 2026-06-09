"""v10: add ``apps.links`` column.  Body in v0010_app_links.sql."""

from __future__ import annotations

from compute_space.db.versioned.base import SqlFileMigration


class Migration0010AppLinks(SqlFileMigration):
    version = 10
    sql_file = "v0010_app_links.sql"
