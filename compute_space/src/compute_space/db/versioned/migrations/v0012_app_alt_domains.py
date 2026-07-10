"""v12: add ``app_alt_domains`` table.  Body in v0012_app_alt_domains.sql."""

from __future__ import annotations

from compute_space.db.versioned.base import SqlFileMigration


class Migration0012AppAltDomains(SqlFileMigration):
    version = 12
    sql_file = "v0012_app_alt_domains.sql"
