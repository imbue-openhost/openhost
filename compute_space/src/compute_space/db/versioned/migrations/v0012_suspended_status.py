"""v12: extend ``apps.status`` CHECK to include ``'suspended'``.

Rebuilds the apps table (using the same create-new/copy/drop/rename pattern
as v0004) to add ``'suspended'`` to the CHECK constraint and to canonicalise
the column order: ``cpu_cores`` and ``links`` were appended by v0011/v0010,
this rebuild moves them into their logical positions.
"""

from __future__ import annotations

import sqlite3

from compute_space.db.versioned.base import Migration

_CREATE_NEW_APPS = """
CREATE TABLE apps_new (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    app_id TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL UNIQUE,
    manifest_name TEXT NOT NULL DEFAULT '',
    version TEXT NOT NULL,
    description TEXT,
    runtime_type TEXT NOT NULL DEFAULT 'serverfull',
    repo_path TEXT NOT NULL,
    repo_url TEXT,
    health_check TEXT,
    local_port INTEGER NOT NULL UNIQUE,
    container_port INTEGER,
    container_id TEXT,
    status TEXT NOT NULL DEFAULT 'stopped' CHECK(status IN ('building', 'starting', 'running', 'stopped', 'error', 'removing', 'suspended')),
    error_message TEXT,
    memory_mb INTEGER NOT NULL DEFAULT 128,
    cpu_cores REAL NOT NULL DEFAULT 0.1,
    gpu INTEGER NOT NULL DEFAULT 0,
    public_paths TEXT NOT NULL DEFAULT '[]',
    links TEXT NOT NULL DEFAULT '[]',
    manifest_raw TEXT,
    installed_by TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
)
"""

_COPY_APPS = """
INSERT INTO apps_new (
    id, app_id, name, manifest_name, version, description,
    runtime_type, repo_path, repo_url, health_check, local_port, container_port,
    container_id, status, error_message, memory_mb, cpu_cores,
    gpu, public_paths, links, manifest_raw, installed_by, created_at, updated_at
)
SELECT
    id, app_id, name, manifest_name, version, description,
    runtime_type, repo_path, repo_url, health_check, local_port, container_port,
    container_id, status, error_message, memory_mb, cpu_cores,
    gpu, public_paths, links, manifest_raw, installed_by, created_at, updated_at
FROM apps
"""


class Migration0012SuspendedStatus(Migration):
    version = 12

    def up(self, db: sqlite3.Connection) -> None:  # pragma: no cover - apply() is overridden
        raise NotImplementedError("Migration0012SuspendedStatus drives execution through apply()")

    def apply(self, db: sqlite3.Connection) -> None:
        db.execute("PRAGMA foreign_keys = OFF")
        db.execute("BEGIN EXCLUSIVE")
        try:
            db.execute(_CREATE_NEW_APPS)
            db.execute(_COPY_APPS)
            db.execute("DROP TABLE apps")
            db.execute("ALTER TABLE apps_new RENAME TO apps")
            db.execute("CREATE INDEX IF NOT EXISTS idx_apps_status ON apps(status)")
            db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_apps_app_id ON apps(app_id)")
            db.execute(
                "INSERT OR REPLACE INTO schema_version (id, version) VALUES (1, ?)",
                (self.version,),
            )
            db.execute("COMMIT")
        except Exception:
            try:
                db.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            db.execute("PRAGMA foreign_keys = ON")
