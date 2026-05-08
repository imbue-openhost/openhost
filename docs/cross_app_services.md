## Cross-App Services

Apps can expose services that other apps consume. The router (compute_space) mediates all cross-app communication — apps never talk directly to each other.

A **service** is identified by a URL (typically a git URL pointing at a spec) plus a SemVer version. Multiple apps can implement the same service; the router resolves which provider to use per call.

### Service identity

Services are identified by URL, e.g. `github.com/imbue-openhost/openhost/services/secrets`. The URL is conventionally a git path containing the service spec (`openhost_service.toml` + an OpenAPI document), but the router treats it as an opaque identifier — only string equality matters at lookup time.

Versions follow SemVer. Providers declare a concrete version; consumers declare a SemVer specifier (e.g. `>=0.1.0`).

### Provider apps

Provider apps declare what services they offer in their manifest:

```toml
[[services.v2.provides]]
service = "github.com/imbue-openhost/openhost/services/secrets"
version = "0.1.0"
endpoint = "/_service_v2/"
```

`endpoint` is the path prefix on the provider where service requests land. The router proxies `<endpoint>/<rest>` to the provider container.

When the same service URL is provided by multiple apps, the first one to register becomes the **default provider** for that service. Consumers can pin to a specific provider per call (see "Provider selection" below).

### Consumer apps

Consumer apps declare what they consume, with a `shortname` they'll use to call it and the grants they're requesting:

```toml
[[services.v2.consumes]]
service = "github.com/imbue-openhost/openhost/services/oauth"
shortname = "oauth"
version = ">=0.1.0"
grants = [
    {provider = "google", scopes = ["https://www.googleapis.com/auth/gmail.readonly"]},
    {provider = "github", scopes = ["repo"]},
]
```

Each entry in `grants` is either an opaque string (e.g. `"read"`) or a TOML/JSON object (e.g. `{key = "DB_URL"}`). Strings work well for simple flag-style permissions; objects are for grants with structured fields. The shape is defined by the service, not the router — providers receive the raw grants verbatim and decide what they mean.

`shortname` must match `^[a-z][a-z0-9_-]{0,31}$` and be unique within the manifest.

### Authentication

The router identifies the calling app two ways:

- **Server-side calls:** `Authorization: Bearer {OPENHOST_APP_TOKEN}`. Each app gets a unique token injected as an env var at deploy time.
- **Browser calls:** the request's `Origin` is matched against the app's subdomain, with the JWT cookie verifying the user.

### Calling a service

```
GET|POST|... /api/services/v2/call/<shortname>/<rest>
```

The router loads the consumer's manifest, finds the `[[services.v2.consumes]]` entry matching `<shortname>`, resolves the provider, and proxies to `<provider_endpoint>/<rest>`. WebSockets are supported on the same path.

### Provider selection

Calls go to the service's default provider (the first app to register that service URL). If the resolved provider's version doesn't satisfy the consumer's version specifier, the router returns 503 `service_not_available`.

### Permissions

Permissions in v2 are **opaque grant payloads** (strings or JSON objects), scoped per `(consumer_app, service_url)`. The router stores grants and forwards the granted set to the provider on every call — but **the provider is what enforces access**, not the router. This lets services define whatever permission shape they need.

**Grant scope** is one of:
- `global`: applies to all consumer apps that ask for the same payload (e.g. "any app may read this secret").
- `app`: applies only to a specific consumer.

**On every proxied call, the router injects:**
- `X-OpenHost-Consumer: <consumer_app>`
- `X-OpenHost-Permissions: <json array of granted payloads>`

Each entry in the permissions array is `{"grant": <payload>, "scope": "global"|"app", "provider_app": <name or null>}`.

**Requesting a missing permission.** When a consumer calls without sufficient grants, the provider returns:

```json
HTTP 403
{
  "error": "permission_required",
  "required_grant": {
    "grant_payload": { ... },
    "scope": "global"
  }
}
```

For `scope: "global"`, the router rewrites the response to add a `grant_url` pointing at the owner-facing approval page. For `scope: "app"`, the provider must include its own `grant_url` (pointing back through the router to its own approval flow).

The consumer redirects the owner to `grant_url`; after approval, the call can be retried.

**Granting at deploy time.** The compute_space CLI's `--grant-permissions-v2` flag pre-grants every entry in a manifest's `[[services.v2.consumes]]` list, useful for trusted built-in apps.

### OAuth callback

`/api/services/v2/oauth_callback` is a fixed redirect target for third-party OAuth providers (Google, GitHub, etc). The OAuth app encodes its app name in the `state` parameter (`{"app": "<name>", "nonce": "..."}`); the router parses that and proxies the callback to that app's `/callback`.

### Service specs

Service definitions live under `services/<name>/`:

```
services/secrets/
  openhost_service.toml   # [service] name + description
  openapi.yaml            # request/response schemas, grant payload shape
```

This is documentation — the router doesn't read these files. They exist so consumers and providers agree on the service's wire format and grant payload structure.

### Built-in services

- **secrets** (`github.com/imbue-openhost/openhost/services/secrets`) — key-value secret storage. Grant payload: `{"key": "<NAME>"}` or `{"key": "*"}` for full access. Provider returns only the values for keys in the granted set.
- **oauth** (`github.com/imbue-openhost/openhost/services/oauth`) — OAuth token acquisition/refresh for third-party APIs. Grant payload: `{"provider": "<name>", "scopes": [...]}`.

### Design notes

- Router mediates everything centrally because it already enforces auth and the owner needs a single place to manage permissions.
- Apps authenticate via opaque tokens (no public/private keypairs) — the router is local and trusted.
- Permission payloads are opaque JSON: services own their permission shape, the router just stores and forwards.
- Provider-side enforcement (vs router-side) keeps the router stupid about service semantics; whatever a service decides "permitted" means is up to it.
- Versioning lets multiple incompatible versions of a service coexist; consumers pin via SemVer specifier.
