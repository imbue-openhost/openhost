"""v9: drop the JWT-era ``owner``/``refresh_tokens`` tables and create the new
``users``/``sessions`` tables that back the opaque-session auth code.  SQL body
in v0009_session_auth.sql."""

from __future__ import annotations

from compute_space.db.versioned.base import SqlFileMigration


class Migration0009SessionAuth(SqlFileMigration):
    version = 9
    sql_file = "v0009_session_auth.sql"
