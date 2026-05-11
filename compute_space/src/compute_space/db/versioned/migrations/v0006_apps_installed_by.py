"""v6: add ``apps.installed_by`` column.  Body in v0006_apps_installed_by.sql."""

from __future__ import annotations

from compute_space.db.versioned.base import SqlFileMigration


class Migration0006AppsInstalledBy(SqlFileMigration):
    version = 6
    sql_file = "v0006_apps_installed_by.sql"
