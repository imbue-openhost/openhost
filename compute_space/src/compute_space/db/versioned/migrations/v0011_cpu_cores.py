"""v11: rename ``apps.cpu_millicores`` -> ``apps.cpu_cores``.  Body in v0011_cpu_cores.sql."""

from __future__ import annotations

from compute_space.db.versioned.base import SqlFileMigration


class Migration0011CpuCores(SqlFileMigration):
    version = 11
    sql_file = "v0011_cpu_cores.sql"
