"""v7: introduce ``backend = 'disabled'`` as the new default.

Pre-v7 a brand-new zone came up with ``backend = 'local'`` silently
and apps with ``app_archive = true`` would write straight to local
disk under a "fallback" pretext.  Post-v7 a fresh zone is
``disabled`` and the install path refuses archive-using apps until
an operator picks a backend on the System tab.

Implemented as ``SqlFileMigration`` because the body is a pure-SQL
rename-create-copy-drop dance (SQLite can't ALTER a CHECK
constraint in place); see ``v0007_archive_backend_disabled_state.sql``.

Existing zones already at ``backend = 'local'`` are preserved
verbatim — the migration's INSERT-SELECT carries the row over, and
the dashboard renders the same "Local disk" UX it does today.
"""

from __future__ import annotations

from compute_space.db.versioned.base import SqlFileMigration


class Migration0007ArchiveBackendDisabledState(SqlFileMigration):
    version = 7
    sql_file = "v0007_archive_backend_disabled_state.sql"
