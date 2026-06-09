# OpenHost Manifest Spec (v0.1)

Apps declare how they should be deployed on OpenHost by placing an `openhost.toml` file at the root of their repository. For a walkthrough of building an app from scratch, see [Creating an App](creating_an_app.md).

## Field Reference

### `[app]` — required

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | string | yes | Unique app identifier (lowercase, hyphens ok) |
| `version` | string | yes | Semver version string |
| `description` | string | no | Short description |
| `authors` | string[] | no | List of author names |

### `[runtime.container]` — required

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `image` | string | yes | — | Path to Dockerfile relative to repo root |
| `port` | integer | yes | — | Port the container listens on |
| `command` | string | no | — | Override container CMD |
| `extra_ports` | string[] | no | `[]` | **Deprecated.** Present entries emit a WARNING log at parse time but do not produce any port mappings.  Use `[[ports]]` instead. |
| `capabilities` | string[] | no | `[]` | **Additional** Linux capabilities to grant inside the container, on top of the Docker-default baseline (CHOWN, DAC_OVERRIDE, FOWNER, FSETID, KILL, NET_BIND_SERVICE, SETFCAP, SETGID, SETPCAP, SETUID, SYS_CHROOT, NET_RAW, MKNOD, AUDIT_WRITE) that every container receives automatically. Restricted to a rootless-safe allowlist (see `compute_space.core.manifest.SAFE_CAPABILITIES`); entries like `"SYS_ADMIN"` are rejected at parse time. Accepts names with or without the `CAP_` prefix. |
| `devices` | string[] | no | `[]` | Host devices to pass through (e.g., `"/dev/net/tun"`). Restricted to a rootless-safe allowlist (see `compute_space.core.manifest.SAFE_DEVICE_PATHS`); paths like `/dev/mem`, `/dev/kvm`, or raw block devices are rejected at parse time. |

### `[[ports]]` — optional, repeatable

Declares additional port mappings for the container. Each entry binds a container port to a host port (TCP+UDP on 0.0.0.0). Set `host_port = 0` for auto-assignment from the 9000-9999 range.

Rootless podman can bind ports >= 25 only; `host_port` values below 25 are rejected at parse time. Ports `80` and `443` are claimed by the built-in Caddy front-door and will fail to bind if an app requests them. For public HTTP/HTTPS, route through the router proxy (apps live under `https://{app_name}.{zone_domain}/`); for other protocols (e.g. SMTP on `25`), pick `host_port = 25` or any port in the 9000-9999 auto-assign range.

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `label` | string | yes | — | Unique label for this port mapping (e.g., `"metrics"`) |
| `container_port` | integer | yes | — | Port inside the container |
| `host_port` | integer | no | `0` | Port on the host (0 = auto-assign) |

### `[[tls_certs]]` — optional, repeatable

Requests a real, ACME-issued TLS certificate (not self-signed) covering one or more hostnames under the app's own subdomain. The router acquires the certificate via the same ACME DNS-01 machinery used for the zone cert, writes it to a router-owned directory, and bind-mounts that directory **read-only** into the container at `/data/tls`. The cert/key paths are also exposed as environment variables.

This exists for apps that need certificates trusted by third parties — for example an XMPP server doing server-to-server federation, where peers reject self-signed certs, and where the zone wildcard `*.{zone}` does not cover second-level component hosts like `conference.xmpp.{zone}`.

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `label` | string | yes | — | Unique identifier for this cert (lowercase, used to disambiguate env vars) |
| `domains` | string[] | yes | — | SAN hostnames to cover. May use `{app}` and `{zone}` placeholders. Every rendered domain **must** be `{app}.{zone}` or a subdomain of it — apps cannot request certs for the bare zone or other apps' hostnames. |
| `cert_path` | string | no | `certs/{app}.{zone}.crt` | Where the cert PEM lands, relative to `/data/tls`. May use placeholders. Must be a relative path. |
| `key_path` | string | no | `certs/{app}.{zone}.key` | Where the key PEM lands, relative to `/data/tls`. May use placeholders. Must be a relative path. |

Placeholders: `{app}` expands to the deployed app name, `{zone}` to the zone domain (no port). The router always issues a **dedicated** certificate with its own freshly-generated private key, covering exactly the requested SANs. (The zone wildcard cert is never reused: its private key is also valid for the bare zone and every other app's subdomain, so handing it to a container would let the app impersonate the whole zone.) Certificates are renewed automatically by the router (re-issued within 30 days of expiry; the container is restarted to load the new pair).

If the certificate cannot be provisioned yet (e.g. CoreDNS/ACME not configured, or DNS not yet propagated), deployment fails with an error — apps that want to start regardless should also ship a self-signed fallback and only swap to the injected cert when present.

```toml
[[tls_certs]]
label = "xmpp"
domains = ["{app}.{zone}", "conference.{app}.{zone}", "share.{app}.{zone}"]
```

### `[routing]` — optional

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `health_check` | string | no | — | Health check path |
| `public_paths` | string[] | no | `[]` | Route prefixes accessible without authentication |

### `[resources]` — optional

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `memory_mb` | integer | no | 128 | Max memory in MB |
| `cpu_millicores` | integer | no | 100 | CPU allocation (1000 = 1 core) |
| `gpu` | boolean | no | false | Whether GPU access is needed |

### `[data]` — optional

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `app_data` | boolean | no | **true** | Request access to permanent filesystem directory (backed up). Set `false` to explicitly opt out. |
| `app_temp_data` | boolean | no | false | Request access to temporary filesystem directory (not backed up) |
| `app_archive` | boolean | no | false | Request access to the elastic archive directory for bulk content. Backed by S3 (via JuiceFS) once the operator configures it from the dashboard; apps with this flag won't install until S3 is configured. |
| `sqlite` | string[] | no | [] | SQLite database names to provision (implicitly enables `app_data`) |
| `access_vm_data` | boolean | no | false | Whether the app can access the VM's shared data directory (read-only) |
| `access_all_app_data` | boolean | no | false | Mount all apps' permanent data and temp data parent directories (rw). Also grants rw access to vm_data. For admin tools like file browsers. |
| `access_all_archive` | boolean | no | false | Mount all apps' archive parent directory. Permissive: silently skipped when JuiceFS is not configured. For backup tools. |
| `access_all_data` | boolean | no | false | Convenience shorthand for `access_all_app_data = true` + `access_all_archive = true`. |


## Data Directory Structure

Apps have three storage areas, each with different durability + size + latency tradeoffs. **By default, apps receive a permanent data directory (`app_data`).** Other tiers must be explicitly requested:

- **Permanent data** (`/data/app_data/{app_name}/`) — local disk. Small, fast, backed up. Enabled by `app_data = true` or by requesting `sqlite` entries. **SQLite databases must live here**, not in `app_archive`: the archive tier may be backed by a network FS with close-to-open consistency that corrupts SQLite WAL.
- **Temporary data** (`/data/app_temp_data/{app_name}/`) — local disk scratch. Not backed up, recreatable. Enabled by `app_temp_data = true`.
- **Archive data** (`/data/app_archive/{app_name}/`) — elastic, S3-backed via JuiceFS. Disabled by default on fresh zones; the operator configures the backend one-shot from the dashboard. Large, higher-latency on uncached reads, durability tied to the operator's S3 provider SLA. Intended for bulk content (videos, photos, attachments) — anything that needs near-unlimited capacity but tolerates network-FS latency. Enabled by `app_archive = true`. Apps with this flag are blocked from install/reload until the operator configures the S3 backend.
- **VM data** (`/data/vm_data/`) — router database and VM-level shared data. Only accessible if `access_vm_data = true`.

The archive tier is disabled by default. The operator configures it one-shot from the dashboard, supplying S3 credentials and a per-zone prefix; archive bytes then route through a JuiceFS mount of the operator-supplied bucket. Apps see `/data/app_archive/<app>/` as a normal POSIX directory and don't need to know JuiceFS is involved.

The host operator can optionally set `storage_min_free_mb` in the OpenHost config to require a minimum amount of free disk space. When free space drops below this threshold, the storage guard stops running apps until space is freed.

All data dirs live under `/data/` in the container. All apps see the same path structure regardless of permissions — only the dirs they have access to are mounted. With `access_all_app_data`, the parent dirs `/data/app_data/` and `/data/app_temp_data/` are mounted so the app can see all apps' data. With `access_all_archive`, the `/data/app_archive/` parent is mounted.

## Environment Variable Injection

The host provisions requested data services and injects connection info as environment variables:

- `OPENHOST_SQLITE_<NAME>` — filesystem path to the named sqlite database (only if `sqlite` entries requested)
- `OPENHOST_APP_DATA_DIR` — `/data/app_data/{app_name}` (only if app_data access granted)
- `OPENHOST_APP_TEMP_DIR` — `/data/app_temp_data/{app_name}` (only if app_temp_data access granted)
- `OPENHOST_APP_ARCHIVE_DIR` — `/data/app_archive/{app_name}` (only if app_archive access granted)
- `OPENHOST_AUTH_PUBLIC_KEY` — PEM-encoded JWT public key for token verification (only if signing keys are available)
- `OPENHOST_ROUTER_URL` — URL of the router's HTTP server, reachable from inside the container.
- `OPENHOST_OWNER_USERNAME` — the compute space owner's chosen display name; use to seed SSO account names. Defaults to `owner` if not explicitly configured.
- `OPENHOST_TLS_CERT` / `OPENHOST_TLS_KEY` — in-container (read-only) paths to the first `[[tls_certs]]` cert/key pair (only if `[[tls_certs]]` declared). `OPENHOST_TLS_DOMAINS` is the comma-separated SAN list.
- `OPENHOST_TLS_CERT_<LABEL>` / `OPENHOST_TLS_KEY_<LABEL>` / `OPENHOST_TLS_DOMAINS_<LABEL>` — per-cert variants for apps declaring multiple `[[tls_certs]]` entries (`<LABEL>` is the uppercased label with hyphens replaced by underscores).

## Examples

### Basic app

```toml
[app]
name = "my-app"
version = "0.1.0"
description = "A simple web app"

[runtime.container]
image = "Dockerfile"
port = 8080

[routing]
health_check = "/health"

[resources]
memory_mb = 128
cpu_millicores = 100

[data]
sqlite = ["main"]
```

### App with extra container permissions

```toml
[app]
name = "ha-tunnel"
version = "0.2.0"
description = "WebSocket tunnel to Home Assistant"

[runtime.container]
image = "Dockerfile"
port = 8080

[routing]
public_paths = ["/tunnel"]

[resources]
memory_mb = 128
cpu_millicores = 100
```

### App with extra port mappings

```toml
[app]
name = "monitoring"
version = "0.1.0"

[runtime.container]
image = "Dockerfile"
port = 8080

[[ports]]
label = "metrics"
container_port = 9090
host_port = 9090

[[ports]]
label = "debug"
container_port = 5005
host_port = 0  # auto-assigned
```

### Minimal app (wrapping existing software)

```toml
[app]
name = "file-browser"
version = "0.1.0"
description = "Web-based file browser"

[runtime.container]
image = "Dockerfile"
port = 5000
command = "/data -A"

[data]
access_all_app_data = true
access_all_archive = true
```
