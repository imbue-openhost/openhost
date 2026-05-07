BEGIN TRANSACTION;
CREATE TABLE api_tokens (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
INSERT INTO "api_tokens" VALUES(1,'ci-deploy','api-hash-ci','2099-01-01T00:00:00','2024-01-01T00:00:00');
INSERT INTO "api_tokens" VALUES(2,'monitoring','api-hash-mon','2099-12-31T00:00:00','2024-03-01T00:00:00');
CREATE TABLE app_databases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    app_name TEXT NOT NULL,
    db_name TEXT NOT NULL,
    db_path TEXT NOT NULL,
    FOREIGN KEY (app_name) REFERENCES apps(name) ON DELETE CASCADE,
    UNIQUE(app_name, db_name)
);
INSERT INTO "app_databases" VALUES(1,'orders','orders_db','/data/orders/orders.db');
INSERT INTO "app_databases" VALUES(2,'billing','billing_db','/data/billing/billing.db');
CREATE TABLE app_port_mappings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    app_name TEXT NOT NULL,
    label TEXT NOT NULL,
    container_port INTEGER NOT NULL,
    host_port INTEGER NOT NULL,
    FOREIGN KEY (app_name) REFERENCES apps(name) ON DELETE CASCADE,
    UNIQUE(app_name, label)
);
INSERT INTO "app_port_mappings" VALUES(1,'orders','grpc',8000,19500);
INSERT INTO "app_port_mappings" VALUES(2,'orders','health',8080,19501);
INSERT INTO "app_port_mappings" VALUES(3,'billing','http',8000,19502);
CREATE TABLE app_tokens (
    app_name TEXT PRIMARY KEY,
    token_hash TEXT NOT NULL UNIQUE,
    FOREIGN KEY (app_name) REFERENCES apps(name) ON DELETE CASCADE
);
INSERT INTO "app_tokens" VALUES('orders','app-hash-orders');
INSERT INTO "app_tokens" VALUES('billing','app-hash-billing');
CREATE TABLE "apps" (
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
, deploy_apps_permission INTEGER NOT NULL DEFAULT 0);
INSERT INTO "apps" VALUES(1,'orders','orders','1.0.0','Order service','serverfull','/repo/orders',NULL,NULL,19100,NULL,NULL,'stopped',NULL,256,500,0,'[]',NULL,'2024-01-01T00:00:00','2024-01-01T00:00:00',0);
INSERT INTO "apps" VALUES(2,'billing','billing','2.1.0','Billing service','serverfull','/repo/billing','https://git.example/billing',NULL,19101,NULL,NULL,'running',NULL,512,1000,0,'["/invoices"]',NULL,'2024-02-15T10:00:00','2024-02-15T10:00:00',0);
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
INSERT INTO "owner" VALUES(1,'admin','argon2-stub-owner-hash','2024-01-01T00:00:00');
CREATE TABLE permissions (
    consumer_app TEXT NOT NULL,
    permission_key TEXT NOT NULL,
    PRIMARY KEY (consumer_app, permission_key),
    FOREIGN KEY (consumer_app) REFERENCES apps(name) ON DELETE CASCADE
);
INSERT INTO "permissions" VALUES('orders','net.egress');
INSERT INTO "permissions" VALUES('billing','net.egress');
CREATE TABLE permissions_v2 (
            consumer_app TEXT NOT NULL,
            service_url TEXT NOT NULL,
            grant_payload TEXT NOT NULL,
            scope TEXT NOT NULL DEFAULT 'global' CHECK(scope IN ('global', 'app')),
            provider_app TEXT NOT NULL DEFAULT '',
            PRIMARY KEY (consumer_app, service_url, grant_payload, scope, provider_app),
            FOREIGN KEY (consumer_app) REFERENCES apps(name) ON DELETE CASCADE
        );
CREATE TABLE refresh_tokens (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    token_hash TEXT UNIQUE NOT NULL,
    expires_at TEXT NOT NULL,
    revoked INTEGER NOT NULL DEFAULT 0
);
INSERT INTO "refresh_tokens" VALUES(1,'refresh-hash-alpha','2099-01-01T00:00:00',0);
INSERT INTO "refresh_tokens" VALUES(2,'refresh-hash-beta','2099-06-01T00:00:00',1);
CREATE TABLE schema_version (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    version INTEGER NOT NULL
);
INSERT INTO "schema_version" VALUES(1,6);
CREATE TABLE service_defaults (
            service_url TEXT PRIMARY KEY,
            app_name TEXT NOT NULL,
            FOREIGN KEY (app_name) REFERENCES apps(name) ON DELETE CASCADE
        );
CREATE TABLE service_providers (
    service_name TEXT NOT NULL,
    app_name TEXT NOT NULL,
    PRIMARY KEY (service_name, app_name),
    FOREIGN KEY (app_name) REFERENCES apps(name) ON DELETE CASCADE
);
INSERT INTO "service_providers" VALUES('payments','orders');
INSERT INTO "service_providers" VALUES('invoices','billing');
CREATE TABLE service_providers_v2 (
            service_url TEXT NOT NULL,
            app_name TEXT NOT NULL,
            service_version TEXT NOT NULL,
            endpoint TEXT NOT NULL,
            PRIMARY KEY (service_url, app_name, service_version),
            FOREIGN KEY (app_name) REFERENCES apps(name) ON DELETE CASCADE
        );
CREATE UNIQUE INDEX idx_port_mappings_host_port ON app_port_mappings(host_port);
CREATE INDEX idx_refresh_tokens_token_hash ON refresh_tokens(token_hash);
CREATE INDEX idx_apps_status ON apps(status);
COMMIT;
