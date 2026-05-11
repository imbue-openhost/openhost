BEGIN TRANSACTION;
CREATE TABLE api_tokens (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE "app_databases" (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                app_id TEXT NOT NULL,
                db_name TEXT NOT NULL,
                db_path TEXT NOT NULL,
                FOREIGN KEY (app_id) REFERENCES apps(app_id) ON DELETE CASCADE,
                UNIQUE(app_id, db_name)
            );
CREATE TABLE "app_port_mappings" (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                app_id TEXT NOT NULL,
                label TEXT NOT NULL,
                container_port INTEGER NOT NULL,
                host_port INTEGER NOT NULL,
                FOREIGN KEY (app_id) REFERENCES apps(app_id) ON DELETE CASCADE,
                UNIQUE(app_id, label)
            );
CREATE TABLE "app_tokens" (
                app_id TEXT PRIMARY KEY,
                token_hash TEXT NOT NULL UNIQUE,
                FOREIGN KEY (app_id) REFERENCES apps(app_id) ON DELETE CASCADE
            );
CREATE TABLE "apps" (
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
                status TEXT NOT NULL DEFAULT 'stopped' CHECK(status IN ('building', 'starting', 'running', 'stopped', 'error', 'removing')),
                error_message TEXT,
                memory_mb INTEGER NOT NULL DEFAULT 128,
                cpu_millicores INTEGER NOT NULL DEFAULT 100,
                gpu INTEGER NOT NULL DEFAULT 0,
                public_paths TEXT NOT NULL DEFAULT '[]',
                manifest_raw TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
CREATE TABLE archive_backend (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    backend TEXT NOT NULL DEFAULT 'disabled' CHECK(backend IN ('disabled', 's3')),
    s3_bucket TEXT,
    s3_region TEXT,
    s3_endpoint TEXT,
    s3_prefix TEXT,
    s3_access_key_id TEXT,
    s3_secret_access_key TEXT,
    juicefs_volume_name TEXT NOT NULL DEFAULT 'openhost',
    configured_at TEXT,
    state_message TEXT
);
INSERT INTO "archive_backend" VALUES(1,'disabled',NULL,NULL,NULL,NULL,NULL,NULL,'openhost',NULL,NULL);
CREATE TABLE "owner" (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE "permissions" (
                consumer_app_id TEXT NOT NULL,
                permission_key TEXT NOT NULL,
                PRIMARY KEY (consumer_app_id, permission_key),
                FOREIGN KEY (consumer_app_id) REFERENCES apps(app_id) ON DELETE CASCADE
            );
CREATE TABLE "permissions_v2" (
                consumer_app_id TEXT NOT NULL,
                service_url TEXT NOT NULL,
                grant_payload TEXT NOT NULL,
                scope TEXT NOT NULL DEFAULT 'global' CHECK(scope IN ('global', 'app')),
                provider_app_id TEXT NOT NULL DEFAULT '',
                PRIMARY KEY (consumer_app_id, service_url, grant_payload, scope, provider_app_id),
                FOREIGN KEY (consumer_app_id) REFERENCES apps(app_id) ON DELETE CASCADE
            );
CREATE TABLE refresh_tokens (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    token_hash TEXT UNIQUE NOT NULL,
    expires_at TEXT NOT NULL,
    revoked INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE schema_version (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    version INTEGER NOT NULL
);
INSERT INTO "schema_version" VALUES(1,6);
CREATE TABLE "service_defaults" (
                service_url TEXT PRIMARY KEY,
                app_id TEXT NOT NULL,
                FOREIGN KEY (app_id) REFERENCES apps(app_id) ON DELETE CASCADE
            );
CREATE TABLE "service_providers" (
                service_name TEXT NOT NULL,
                app_id TEXT NOT NULL,
                PRIMARY KEY (service_name, app_id),
                FOREIGN KEY (app_id) REFERENCES apps(app_id) ON DELETE CASCADE
            );
CREATE TABLE "service_providers_v2" (
                service_url TEXT NOT NULL,
                app_id TEXT NOT NULL,
                service_version TEXT NOT NULL,
                endpoint TEXT NOT NULL,
                PRIMARY KEY (service_url, app_id, service_version),
                FOREIGN KEY (app_id) REFERENCES apps(app_id) ON DELETE CASCADE
            );
CREATE INDEX idx_refresh_tokens_token_hash ON refresh_tokens(token_hash);
CREATE INDEX idx_apps_status ON apps(status);
CREATE UNIQUE INDEX idx_apps_app_id ON apps(app_id);
CREATE UNIQUE INDEX idx_port_mappings_host_port ON app_port_mappings(host_port);
COMMIT;
