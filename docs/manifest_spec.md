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
| `app_data` | boolean | no | false | Request access to permanent filesystem directory (backed up) |
| `app_temp_data` | boolean | no | false | Request access to temporary filesystem directory (not backed up) |
| `app_archive` | boolean | no | false | Request access to the elastic archive directory for bulk content. Backed by S3 (via JuiceFS) once the operator configures it from the dashboard; apps with this flag won't install until S3 is configured. Requires `app_data` (or `sqlite`, or `access_all_data`) — see Data Directory Structure below for the rationale. |
| `sqlite` | string[] | no | [] | SQLite database names to provision (implicitly enables `app_data`) |
| `access_vm_data` | boolean | no | false | Whether the app can access the VM's shared data directory |
| `access_all_data` | boolean | no | false | Full access to permanent data, temp data, archive, and vm data |

## Data Directory Structure

Apps have three storage areas, each with different durability + size + latency tradeoffs. **By default, apps have no filesystem access.** Each must be explicitly requested:

- **Permanent data** (`/data/app_data/{app_name}/`) — local disk. Small, fast, backed up. Enabled by `app_data = true` or by requesting `sqlite` entries. **SQLite databases must live here**, not in `app_archive`: the archive tier may be backed by a network FS with close-to-open consistency that corrupts SQLite WAL.
- **Temporary data** (`/data/app_temp_data/{app_name}/`) — local disk scratch. Not backed up, recreatable. Enabled by `app_temp_data = true`.
- **Archive data** (`/data/app_archive/{app_name}/`) — elastic, S3-backed via JuiceFS. Disabled by default on fresh zones; the operator configures the backend one-shot from the dashboard. Large, higher-latency on uncached reads, durability tied to the operator's S3 provider SLA. Intended for bulk content (videos, photos, attachments) — anything that needs near-unlimited capacity but tolerates network-FS latency. Enabled by `app_archive = true`. Apps with this flag are blocked from install/reload until the operator configures the S3 backend. Requires the local-data tier (`app_data`, `sqlite`, or `access_all_data`) so working state has somewhere safe to live.
- **VM data** (`/data/vm_data/`) — router database and VM-level shared data. Only accessible if `access_vm_data = true`.

The archive tier is disabled by default. The operator configures it one-shot from the dashboard, supplying S3 credentials and a per-zone prefix; archive bytes then route through a JuiceFS mount of the operator-supplied bucket. Apps see `/data/app_archive/<app>/` as a normal POSIX directory and don't need to know JuiceFS is involved.

The host operator can optionally set `storage_min_free_mb` in the OpenHost config to require a minimum amount of free disk space. When free space drops below this threshold, the storage guard stops running apps until space is freed.

All data dirs live under `/data/` in the container. All apps see the same path structure regardless of permissions — only the dirs they have access to are mounted. With `access_all_data`, the parent dirs are mounted instead (`/data/app_data/`, `/data/app_temp_data/`, `/data/app_archive/`) so the app can see all apps' data.

## Environment Variable Injection

The host provisions requested data services and injects connection info as environment variables:

- `OPENHOST_SQLITE_<name>` — filesystem path to the named sqlite database (only if `sqlite` entries requested)
- `OPENHOST_APP_DATA_DIR` — `/data/app_data/{app_name}` (only if app_data access granted)
- `OPENHOST_APP_TEMP_DIR` — `/data/app_temp_data/{app_name}` (only if app_temp_data access granted)
- `OPENHOST_APP_ARCHIVE_DIR` — `/data/app_archive/{app_name}` (only if app_archive access granted)
- `OPENHOST_AUTH_PUBLIC_KEY` — PEM-encoded JWT public key for token verification (only if signing keys are available)
- `OPENHOST_ROUTER_URL` — URL of the router's HTTP server, reachable from inside the container.

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
