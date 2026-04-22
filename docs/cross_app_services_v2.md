## Cross-App Services V2

Services V2 replaces string-name service identifiers with git repo URLs, adds semver versioning, and moves permission enforcement from the router to provider apps. It runs alongside the existing V1 service interface — both work simultaneously.

### Service identity

A service is identified by a git repo URL, e.g. `github.com/imbue-openhost/openhost/services/secrets`. Subdirectory paths are supported. The service spec repo contains an `openhost_service.toml` manifest (currently minimal — fields will be added as needed).

### Provider manifest

Provider apps declare V2 services in `openhost.toml` using `[[services_v2.provides]]`:

```toml
[[services_v2.provides]]
service = "github.com/imbue-openhost/openhost/services/secrets"
version = "1.0.0"
endpoint = "/_service/"
```

- `service`: the git repo URL identifying the service
- `version`: exact semver version this app provides
- `endpoint`: path prefix on the provider app where the service is hosted (default: `/_service/`)

An app can provide multiple services and can coexist with V1 `[services] provides = [...]` in the same manifest.

### Consumer request flow

Consumer apps make HTTP requests to the router at:

```
/_services_v2/<url-encoded-service-url>/<endpoint>?version=<pip-style-specifier>
```

Example:
```
/_services_v2/github.com%2Fimbue-openhost%2Fopenhost%2Fservices%2Fsecrets/get?version=~=1.0
```

- The service URL is URL-encoded in the path (slashes become `%2F`). The first literal `/` after the service URL separates it from the endpoint.
- `version` query parameter is **required**. Uses pip-style specifiers: `~=1.0` means `>=1.0, <2.0`. See [PEP 440](https://peps.python.org/pep-0440/#version-specifiers).
- Authentication: same as V1 — either `Authorization: Bearer <OPENHOST_APP_TOKEN>` (server-to-server) or JWT cookie + Origin header (browser).

### Version resolution

The router matches the consumer's version specifier against all registered providers for that service URL. If the service has a configured default provider and it's version-compatible, it wins. Otherwise the provider with the highest compatible version is selected. If no compatible provider is running, the router returns 503.

### Permissions

Permissions are stored in the router's DB as `(consumer_app, service_url, grant_payload, scope)`:

- `grant_payload`: arbitrary JSON defining the details of the grant (e.g. `{"key": "DATABASE_URL"}`)
- `scope`: `"global"` (applies regardless of which app provides the service) or `"app"` (scoped to a specific provider app)
- `provider_app`: set when scope is `"app"`, null for global grants
- `expires_at`: optional expiration timestamp

The router attaches all matching grants to the proxied request as the `X-OpenHost-Permissions` header — a JSON array of grant objects:

```json
[
  {"grant": {"key": "DATABASE_URL"}, "scope": "global", "provider_app": null, "expires_at": null},
  {"grant": {"key": "STRIPE_KEY"}, "scope": "global", "provider_app": null, "expires_at": null}
]
```

**Provider-side enforcement**: the provider app reads `X-OpenHost-Permissions`, checks whether the grants are sufficient for the requested action, and either handles the request or returns 403.

### Permission grant flow

When a provider returns 403, it can include a JSON body describing what permissions are needed:

```json
{
  "required_grants": [
    {"key": "DATABASE_URL"},
    {"key": "email/read", "scope": "app", "grant_url": "https://email-app.host.example.com/grant?..."}
  ]
}
```

The router intercepts this 403 and reforms the response for the consumer:

```json
{
  "error": "permission_required",
  "grants_needed": [
    {"key": "DATABASE_URL", "scope": "global", "approve_url": "https://host.example.com/approve-permissions-v2?..."},
    {"key": "email/read", "scope": "app", "grant_url": "https://email-app.host.example.com/grant?..."}
  ],
  "service": "github.com/imbue-openhost/openhost/services/secrets"
}
```

- Global-scoped grants get an `approve_url` pointing to the router's approval page
- App-scoped grants pass through the provider's `grant_url` (the provider hosts its own fine-grained grant page)

If the 403 body doesn't contain `required_grants` (e.g. the provider returned 403 for a non-permission reason), the response is passed through as-is.

### Multiple providers and defaults

Multiple apps can provide the same service. The first app deployed for a given service URL becomes the default automatically. The owner can change the default via the API:

- `GET /api/services_v2/defaults` — list current defaults
- `POST /api/services_v2/defaults` — set default (`{"service_url": "...", "app_name": "..."}`)
- `DELETE /api/services_v2/defaults` — remove default (`{"service_url": "..."}`)

When no default is set, the router picks the provider with the highest compatible version.

### Discovery API

Consumer apps can discover which providers are available:

```
GET /api/services_v2/providers?service=<url>&version=<specifier>
```

Returns a list of provider apps with versions and status. The `version` parameter is optional — if omitted, all providers are returned.

### Management APIs

**Services:**
- `GET /api/services_v2` — list all registered V2 service providers

**Permissions:**
- `GET /api/permissions_v2` — list all V2 permissions (optional `?app=` filter)
- `POST /api/permissions_v2/grant` — grant a permission (`{"app": "...", "service_url": "...", "grant": {...}, "scope": "global"}`)
- `POST /api/permissions_v2/revoke` — revoke a permission (same body format)

### Differences from V1

| | V1 | V2 |
|---|---|---|
| Service identity | String name (`"secrets"`) | Git repo URL (`github.com/.../secrets`) |
| Versioning | None | Semver with pip-style specifiers |
| Permission model | Flat string keys (`secrets/key:DB_URL`) | JSON payloads with global/app scope |
| Permission enforcement | Router (hardcoded in `service_access_rules.py`) | Provider app (reads `X-OpenHost-Permissions` header) |
| Multiple providers | No (1:1 mapping) | Yes, with configurable defaults |
| Route prefix | `/_services/` | `/_services_v2/` |
| Consumer manifest | `[services.secrets] keys = [...]` | Not required — service URL and version are in the request |
