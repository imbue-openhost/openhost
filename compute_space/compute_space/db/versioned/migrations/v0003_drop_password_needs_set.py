"""v3: drop owner.password_needs_set and tighten password_hash to NOT NULL.

See ``v0003_drop_password_needs_set.sql`` for the body and motivation.
"""

from __future__ import annotations

from compute_space.db.versioned.base import SqlFileMigration


class Migration0003DropPasswordNeedsSet(SqlFileMigration):
    version = 3
    sql_file = "v0003_drop_password_needs_set.sql"
