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
| `extra_ports` | string[] | no | `[]` | **Deprecated.** Use `[[ports]]` instead. Raw Docker `-p` format strings. |
| `capabilities` | string[] | no | `[]` | Linux capabilities to add (e.g., `"NET_ADMIN"`) |
| `devices` | string[] | no | `[]` | Host devices to pass through (e.g., `"/dev/tun"`) |

### `[[ports]]` — optional, repeatable

Declares additional port mappings for the container. Each entry binds a container port to a host port (TCP+UDP on 0.0.0.0). Set `host_port = 0` for auto-assignment from the 9000-9999 range.

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `label` | string | yes | — | Unique label for this port mapping (e.g., `"metrics"`) |
| `container_port` | integer | yes | — | Port inside the container |
| `host_port` | integer | no | `0` | Port on the host (0 = auto-assign) |

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

Apps must explicitly request filesystem access. Each category (permanent
data, temporary data, VM data, router state) can be requested for this
app alone (scoped) or for every app on the host (broad). Most
combinations are allowed; the exceptions are noted in the field
descriptions (e.g. `access_vm_data` read-only cannot be combined with
any flag that grants `vm_data` read/write access).

#### Scoped access — just this app

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `app_data` | boolean | no | false | Mount this app's permanent data directory at `/data/app_data/<app>` (backed up) |
| `app_temp_data` | boolean | no | false | Mount this app's temporary directory at `/data/app_temp_data/<app>` (not backed up) |
| `sqlite` | string[] | no | `[]` | SQLite database names to provision (implicitly enables `app_data`) |

#### Broad access — every app's data / shared state

Use these to write apps that manage or inspect state across the whole
host (backup/restore, file browsers, debugging tools). All can be
requested independently.

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `access_all_apps_data` | boolean | no | false | Read/write mount of `/data/app_data` (every app's permanent data) |
| `access_all_apps_temp_data` | boolean | no | false | Read/write mount of `/data/app_temp_data` (every app's temporary data) |
| `access_vm_data` | boolean | no | false | Read-only mount of `/data/vm_data` (VM-level shared data, e.g. signing keys used by multiple apps). Mutually exclusive with `access_vm_data_rw` and `access_all_data`, both of which grant RW access to the same directory. |
| `access_vm_data_rw` | boolean | no | false | Read/write mount of `/data/vm_data`. Mutually exclusive with `access_vm_data` (RO). Combining with `access_all_data` is redundant but accepted, since the legacy shorthand already grants RW. |
| `access_openhost_state_ro` | boolean | no | false | Read-only mount of the OpenHost router's own state directory (`router.db`, TLS cert + key, Corefile, Caddyfile, signing keys, claim token). Intended for full-instance inspection / backup tools. **Not** implied by `access_all_data` — apps must opt in explicitly. Grants visibility into all apps' API tokens and the owner password hash. |

#### Legacy shorthand

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `access_all_data` | boolean | no | false | Shorthand equivalent to `access_all_apps_data = true`, `access_all_apps_temp_data = true`, and `access_vm_data_rw = true`. Does **not** include `access_openhost_state_ro` — for backward compatibility, existing manifests using this flag do not silently gain access to router state. |

## Data Directory Structure

Apps have two storage areas on separate disks. **By default, apps have
no filesystem access.** Each must be explicitly requested:

- **Permanent data** (`/data/app_data/{app_name}/`) — backed up,
  user-visible. Enabled by `app_data = true`, by requesting `sqlite`
  entries, or by any of the broader flags that imply app_data access
  (`access_all_apps_data`, `access_all_data`).
- **Temporary data** (`/data/app_temp_data/{app_name}/`) — not backed
  up, recreatable. Enabled by `app_temp_data = true` or by any flag
  that implies temp-data access (`access_all_apps_temp_data`,
  `access_all_data`).
- **VM data** (`/data/vm_data/`) — VM-level shared data (e.g. signing
  keys used by multiple apps). Enabled by `access_vm_data = true` (RO)
  or `access_vm_data_rw = true` (RW). This does **not** contain the
  router's own SQLite DB — that lives separately, see below.
- **OpenHost router state** (`/data/openhost/`) — the router's own
  `router.db`, TLS material, and related control-plane files. Enabled
  only by `access_openhost_state_ro = true` (RO). Not implied by
  `access_all_data`.

The broad-access flags mount the **parent** directory instead of the
scoped subdir, so your container sees every app's data at
`/data/app_data/<other-app>/`, `/data/app_temp_data/<other-app>/`, etc.

The host operator can optionally set `storage_min_free_mb` in the
OpenHost config to require a minimum amount of free persistent storage.
When free space drops below this threshold, the storage guard stops
running apps until space is freed.

All data dirs live under `/data/` in the container. All apps see the
same path structure regardless of permissions — only the dirs they have
access to are mounted.

## Environment Variable Injection

The host provisions requested data services and injects connection info as environment variables:

- `OPENHOST_SQLITE_<name>` — filesystem path to the named sqlite database (only if `sqlite` entries requested)
- `OPENHOST_APP_DATA_DIR` — `/data/app_data/{app_name}` (set when any permission that implies scoped permanent-data access is granted: `app_data`, `sqlite`, `access_all_apps_data`, or `access_all_data`)
- `OPENHOST_APP_TEMP_DIR` — `/data/app_temp_data/{app_name}` (set when `app_temp_data`, `access_all_apps_temp_data`, or `access_all_data` is granted)
- `OPENHOST_AUTH_PUBLIC_KEY` — PEM-encoded JWT public key for token verification (only if signing keys are available)
- `OPENHOST_ROUTER_URL` — URL of the router's HTTP server (e.g., `http://host.docker.internal:<port>`)

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
access_all_data = true
```
