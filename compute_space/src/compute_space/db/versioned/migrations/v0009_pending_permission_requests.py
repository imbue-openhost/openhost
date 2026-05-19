"""v9: add ``pending_permission_requests_v2`` table for persistent
permission request tracking.  Body in v0009_pending_permission_requests.sql."""

from __future__ import annotations

from compute_space.db.versioned.base import SqlFileMigration


class Migration0009PendingPermissionRequests(SqlFileMigration):
    version = 9
    sql_file = "v0009_pending_permission_requests.sql"
