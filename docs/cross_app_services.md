## Cross-App Services

Apps can expose services that other apps consume. The router mediates all cross-app communication — apps never talk directly to each other.

### How it works

**Provider apps** declare what services they offer in their manifest:
```toml
[services]
provides = ["secrets"]
```

**Consumer apps** declare what they need, with fine-grained per-key permissions:
```toml
[services.secrets]
keys = [
    { key = "DATABASE_URL", reason = "Connect to the production database", required = true },
    { key = "STRIPE_SECRET_KEY", reason = "Process payments", required = true },
    { key = "SENTRY_DSN", reason = "Error reporting", required = false },
]
```

### Authentication

Each app gets a unique `OPENHOST_APP_TOKEN` (random opaque string) injected as an env var at deploy time. Apps use this token to authenticate service requests to the router.

### Request flow

1. Consumer app sends HTTP request to `{OPENHOST_ROUTER_URL}/_services/{service_name}/{action}` with `Authorization: Bearer {OPENHOST_APP_TOKEN}`
2. Router verifies the token, identifies the calling app
3. Router looks up which app provides the requested service
4. Router checks which permission keys are granted for this consumer+service pair
5. Router proxies the request to the provider app at `/_service/{action}`.
6. Provider app handles the request, filtering data based on granted keys

### Permissions

**Static (deploy-time):** When a consumer app is deployed, the router creates permission rows for each key declared in the manifest (initially not granted).

**Dynamic (runtime):** Apps can also request new permissions at runtime:

1. App calls `POST {OPENHOST_ROUTER_URL}/_services/request-permission` with its app token
   - Body: `{"service": "secrets", "key": "NEW_KEY", "reason": "Why I need it"}`
   - Response: `{"status": "pending", "approve_url": "https://..."}` (or `{"status": "granted"}` if already approved)
2. If the owner is interacting with the app, the app redirects them to the `approve_url`
3. The owner sees the permission request with the reason and can grant or deny it
4. After approval, the owner is redirected back (via `?next=` param if the app provided one)
5. The app retries the service call — now it has access

If the owner isn't around, the app should handle the "pending" status gracefully (e.g. show a message saying "waiting for owner approval").

**Management APIs:**
- `GET /api/service-permissions` — list all permission requests
- `POST /api/service-permissions/{id}/grant` — grant a permission
- `POST /api/service-permissions/{id}/revoke` — revoke a permission
- `GET /approve-permissions` — owner-facing approval page (supports `?app=` filter)

### Built-in services

**secrets** — stores environment variables (key-value pairs). Owner manages values via the secrets app dashboard. Consumer apps request specific keys by name. The secrets app returns only the values for keys the consumer has been granted access to.

Service API:
- `POST /_services/secrets/get` — body: `{"keys": ["DATABASE_URL", "STRIPE_SECRET_KEY"]}` — returns granted values
- `GET /_services/secrets/list` — returns available key names (no values)

### Design notes (historical context)

- router mediates everything centrally (vs apps talking directly) because router already enforces auth and the owner needs a single place to manage permissions
- apps authenticate via opaque tokens (no public/private keypairs needed — router is local and trusted)
- permissions are per-key so the owner can grant fine-grained access (eg grant DATABASE_URL but not STRIPE_SECRET_KEY)
- all service calls are normal HTTP requests, no special injection — apps fetch what they need at runtime
