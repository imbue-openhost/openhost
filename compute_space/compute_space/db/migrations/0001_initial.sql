-- Baseline schema. Represents the state of the database after all
-- historical legacy migrate() operations have completed. On fresh DBs
-- yoyo applies this as the first migration; on legacy DBs it is marked
-- applied without re-running (legacy_migrate.migrate() brought the DB
-- to this state already).

CREATE TABLE IF NOT EXISTS apps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
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
    docker_container_id TEXT,
    status TEXT NOT NULL DEFAULT 'stopped' CHECK(status IN ('building', 'starting', 'running', 'stopped', 'error')),
    error_message TEXT,
    memory_mb INTEGER NOT NULL DEFAULT 128,
    cpu_millicores INTEGER NOT NULL DEFAULT 100,
    gpu INTEGER NOT NULL DEFAULT 0,
    public_paths TEXT NOT NULL DEFAULT '[]',
    manifest_raw TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS app_databases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    app_name TEXT NOT NULL,
    db_name TEXT NOT NULL,
    db_path TEXT NOT NULL,
    FOREIGN KEY (app_name) REFERENCES apps(name) ON DELETE CASCADE,
    UNIQUE(app_name, db_name)
);

CREATE TABLE IF NOT EXISTS app_port_mappings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    app_name TEXT NOT NULL,
    label TEXT NOT NULL,
    container_port INTEGER NOT NULL,
    host_port INTEGER NOT NULL,
    FOREIGN KEY (app_name) REFERENCES apps(name) ON DELETE CASCADE,
    UNIQUE(app_name, label)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_port_mappings_host_port ON app_port_mappings(host_port);
CREATE INDEX IF NOT EXISTS idx_apps_status ON apps(status);

CREATE TABLE IF NOT EXISTS owner (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT,
    password_needs_set INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS refresh_tokens (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    token_hash TEXT UNIQUE NOT NULL,
    expires_at TEXT NOT NULL,
    revoked INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_refresh_tokens_token_hash ON refresh_tokens(token_hash);

CREATE TABLE IF NOT EXISTS api_tokens (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS app_tokens (
    app_name TEXT PRIMARY KEY,
    token_hash TEXT NOT NULL UNIQUE,
    FOREIGN KEY (app_name) REFERENCES apps(name) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS service_providers (
    service_name TEXT NOT NULL,
    app_name TEXT NOT NULL,
    PRIMARY KEY (service_name, app_name),
    FOREIGN KEY (app_name) REFERENCES apps(name) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS permissions (
    consumer_app TEXT NOT NULL,
    permission_key TEXT NOT NULL,
    PRIMARY KEY (consumer_app, permission_key),
    FOREIGN KEY (consumer_app) REFERENCES apps(name) ON DELETE CASCADE
);
