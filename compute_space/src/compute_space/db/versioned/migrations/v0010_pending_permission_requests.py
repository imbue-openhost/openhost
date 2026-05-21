"""v10: add ``pending_permission_requests_v2`` table for persistent
permission request tracking.  Body in v0010_pending_permission_requests.sql."""

from __future__ import annotations

from compute_space.db.versioned.base import SqlFileMigration


class Migration0010PendingPermissionRequests(SqlFileMigration):
    version = 10
    sql_file = "v0010_pending_permission_requests.sql"
