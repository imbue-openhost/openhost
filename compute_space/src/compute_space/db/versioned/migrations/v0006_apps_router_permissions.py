"""v6: add per-app router-permission columns.  Body in v0006_apps_router_permissions.sql."""

from __future__ import annotations

from compute_space.db.versioned.base import SqlFileMigration


class Migration0006AppsRouterPermissions(SqlFileMigration):
    version = 6
    sql_file = "v0006_apps_router_permissions.sql"
