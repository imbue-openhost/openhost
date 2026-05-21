-- v10: persistent pending permission requests.
--
-- When a provider returns a permission_required 403, the router
-- records the request so the owner can review it from the
-- dashboard without needing to re-trigger the original flow.

CREATE TABLE IF NOT EXISTS pending_permission_requests_v2 (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    consumer_app_id TEXT NOT NULL,
    service_url TEXT NOT NULL,
    grant_payload TEXT NOT NULL,
    scope TEXT NOT NULL DEFAULT 'global' CHECK(scope IN ('global', 'app')),
    reason TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(consumer_app_id, service_url, grant_payload, scope),
    FOREIGN KEY (consumer_app_id) REFERENCES apps(app_id) ON DELETE CASCADE
);
