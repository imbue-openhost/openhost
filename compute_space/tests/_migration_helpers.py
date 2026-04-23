"""
Shared helpers for migration tests.

Used by both ``test_legacy_migrations.py`` (imperative legacy migrate())
and ``test_yoyo_migrations.py`` (yoyo dispatch + snapshot tests).
"""

import os
import sqlite3

from compute_space.db.connection import init_db

PACKAGE_DIR = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "compute_space"))
SCHEMA_SQL_PATH = os.path.join(PACKAGE_DIR, "db", "schema.sql")
MIGRATIONS_DIR = os.path.join(PACKAGE_DIR, "db", "migrations")


class FakeApp:
    """Minimal stand-in for a Quart app so init_db(app) can read app.config."""

    def __init__(self, db_path: str) -> None:
        self.config = {"DB_PATH": db_path}


def fresh_db(path: str) -> str:
    """Create a DB using only schema.sql (the gold-standard fresh path)."""
    db = sqlite3.connect(path)
    with open(SCHEMA_SQL_PATH) as f:
        db.executescript(f.read())
    db.close()
    return path


def run_init_db(db_path: str) -> None:
    """Run the real init_db against an existing database file."""
    init_db(FakeApp(db_path))


# ---------------------------------------------------------------------------
# Oldest-known router schema: before public_paths, manifest_name were added,
# and with base_path + subdomain columns still present, and owner table
# lacking password_needs_set.
# ---------------------------------------------------------------------------

OLDEST_ROUTER_SCHEMA = """\
CREATE TABLE apps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    base_path TEXT NOT NULL UNIQUE,
    subdomain TEXT NOT NULL UNIQUE,
    version TEXT NOT NULL,
    description TEXT,
    runtime_type TEXT NOT NULL CHECK(runtime_type IN ('serverless', 'serverfull')),
    repo_path TEXT NOT NULL,
    health_check TEXT,
    local_port INTEGER NOT NULL UNIQUE,
    container_port INTEGER,
    docker_container_id TEXT,
    spin_pid INTEGER,
    status TEXT NOT NULL DEFAULT 'stopped' CHECK(status IN ('building', 'starting', 'running', 'stopped', 'error')),
    error_message TEXT,
    memory_mb INTEGER NOT NULL DEFAULT 128,
    cpu_millicores INTEGER NOT NULL DEFAULT 1000,
    gpu INTEGER NOT NULL DEFAULT 0,
    manifest_raw TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE app_databases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    app_name TEXT NOT NULL,
    db_name TEXT NOT NULL,
    db_path TEXT NOT NULL,
    FOREIGN KEY (app_name) REFERENCES apps(name) ON DELETE CASCADE,
    UNIQUE(app_name, db_name)
);

CREATE TABLE app_object_stores (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    app_name TEXT NOT NULL,
    bucket_name TEXT NOT NULL,
    bucket_path TEXT NOT NULL,
    FOREIGN KEY (app_name) REFERENCES apps(name) ON DELETE CASCADE,
    UNIQUE(app_name, bucket_name)
);

CREATE INDEX idx_apps_status ON apps(status);

CREATE TABLE owner (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE refresh_tokens (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    token TEXT UNIQUE NOT NULL,
    expires_at TEXT NOT NULL,
    revoked INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX idx_refresh_tokens_token ON refresh_tokens(token);
"""
