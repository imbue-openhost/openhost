-- v12: add ``app_alt_domains`` table.
--
-- Each row maps a custom domain (e.g. "myapp.example.com") to an app. The
-- owner points a CNAME at <app_name>.<zone_domain>; the router matches
-- inbound Host headers against this table and Caddy issues on-demand TLS
-- certs gated by these rows. ``domain`` is globally UNIQUE so two apps
-- can't claim the same custom domain.

CREATE TABLE IF NOT EXISTS app_alt_domains (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    app_id TEXT NOT NULL,
    domain TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (app_id) REFERENCES apps(app_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_alt_domains_app_id ON app_alt_domains(app_id);
