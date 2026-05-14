---
name: openhost-deployment
description: Reference information about the OpenHost platform, its architecture, and how apps are deployed and managed. This is a purely informational skill -- it describes how things work, not what to do.
---

# OpenHost Deployment Reference

This document describes the OpenHost platform, its architecture, and how
applications are deployed and managed on it. It is intended as a reference
for anyone working with OpenHost -- human or agent.

## Platform overview

OpenHost is a self-hosting platform. A user provisions a server (cloud VPS
or bare metal), installs the OpenHost software via Ansible, and then deploys
applications to it. Each application runs in its own rootless podman
container. The platform handles DNS, TLS, routing, authentication, data
storage, and cross-app services.

The main components:

- **Router** -- a Quart/Hypercorn Python app on port 8080. Serves the web
  dashboard, manages app lifecycle (deploy, reload, stop, remove), and
  reverse-proxies HTTP/WebSocket traffic to app containers.
- **Caddy** -- TLS termination on ports 443/80, reverse-proxying to the
  router. Not used in dev mode.
- **CoreDNS** -- authoritative DNS for the zone's wildcard subdomain. Not
  used in dev mode.
- **Rootless podman** -- container runtime. Each app gets its own user
  namespace.

## Network architecture

```
Internet
  |
  v
CoreDNS (:53)          -- wildcard *.zone.domain -> server IP
  |
  v
Caddy (:443 / :80)    -- TLS termination, reverse proxy to :8080
  |
  v
Router (:8080)         -- auth check, subdomain/path routing, proxy
  |
  v
App container          -- 127.0.0.1:{allocated_port}
```

In dev mode (`openhost up --dev`), CoreDNS and Caddy are absent. The
router serves HTTP directly on :8080.

### Subdomain routing

Apps are accessible at `https://{app_name}.{zone_domain}/`. The router
extracts the app name from the `Host` header and proxies to the app's
allocated local port.

### Path prefix routing

Fallback: `https://{zone_domain}/{app_name}/...`. The router strips the
prefix before proxying.

### Container networking

Rootless podman with pasta shares the host's IP stack. A dummy interface
`openhost0` at `10.200.0.1` is created so containers can reach the host.
Inside containers, `host.containers.internal` resolves to this address.

## App manifest (`openhost.toml`)

Every app has an `openhost.toml` at the repository root. This is the sole
declaration of how the app should be built, configured, and run.

### `[app]` -- required

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `name` | string | yes | -- | Unique identifier. Lowercase alphanumeric + hyphens. Becomes the subdomain. |
| `version` | string | yes | -- | Semver version string. |
| `description` | string | no | `""` | Short description. |
| `authors` | string[] | no | `[]` | Author names. |
| `hidden` | bool | no | `false` | If true, excluded from dashboard listing. |

Reserved names (cannot be used): `/`, `/dashboard`, `/login`, `/logout`,
`/add_app`, `/remove_app`, `/stop_app`, `/reload_app`, `/api`, `/health`,
`/app`, `/setup`, `/.well-known`, `/handle_invite`, `/terminal`,
`/toggle-ssh`, `/identity`, `/settings`, `/system`, `/docs`.

### `[runtime.container]` -- required

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `image` | string | yes | -- | Path to Dockerfile relative to repo root. |
| `port` | int | yes | -- | Port the app listens on inside the container. |
| `command` | string | no | -- | Override container CMD (split on spaces). |
| `capabilities` | string[] | no | `[]` | Extra Linux capabilities beyond the OCI baseline. Restricted to a safe allowlist (NET_ADMIN, NET_RAW, NET_BIND_SERVICE, CHOWN, DAC_OVERRIDE, FOWNER, FSETID, KILL, SETUID, SETGID, SETPCAP, MKNOD, AUDIT_WRITE, SYS_CHROOT, IPC_LOCK, IPC_OWNER, DAC_READ_SEARCH, SETFCAP, NET_BROADCAST). |
| `devices` | string[] | no | `[]` | Host devices to pass through. Allowlist: `/dev/net/tun`, `/dev/fuse`, `/dev/ttyS0-7`, `/dev/ttyUSB0-7`, `/dev/ttyACM0-7`. |
| `shm_mb` | int | no | `0` | Shared memory size in MiB. 0 = podman default (64 MiB). |

### `[[ports]]` -- optional, repeatable

Extra port mappings beyond the main HTTP port. Each entry maps a container
port to a host port (TCP+UDP on 0.0.0.0).

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `label` | string | yes | -- | Unique label for this mapping. |
| `container_port` | int | yes | -- | Port inside the container. |
| `host_port` | int | no | `0` | Port on host. 0 = auto-assign from 9000-9999 range. Must be >= 25. |

Ports 80 and 443 are used by Caddy and cannot be claimed by apps.

### `[routing]` -- optional

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `health_check` | string | no | -- | Path the router polls to determine readiness. |
| `public_paths` | string[] | no | `[]` | Route prefixes accessible without zone authentication. |

### `[resources]` -- optional

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `memory_mb` | int | no | `128` | Max memory in MB. |
| `cpu_millicores` | int | no | `100` | CPU allocation. 1000 = 1 core. |
| `gpu` | bool | no | `false` | Whether GPU access is needed. |

### `[data]` -- optional

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `app_data` | bool | no | `false` | Permanent directory (backed up). |
| `app_temp_data` | bool | no | `false` | Temporary directory (not backed up). |
| `app_archive` | bool | no | `false` | Elastic S3-backed archive (operator must configure S3 first). |
| `sqlite` | string[] | no | `[]` | SQLite database names to provision. Implicitly enables `app_data`. |
| `access_vm_data` | bool | no | `false` | Read-only access to VM shared data. |
| `access_all_data` | bool | no | `false` | Full access to all data directories (all apps + VM data). |

### `[services]` / `[[services.v2.provides]]` / `[[services.v2.consumes]]`

Cross-app service declarations. See the `cross_app_services.md` doc in the
OpenHost repository for the full specification.

## Environment variables

The router injects these environment variables into every app container:

| Variable | Description |
|----------|-------------|
| `OPENHOST_APP_NAME` | App name as registered. Also the subdomain. |
| `OPENHOST_APP_TOKEN` | Random 43-char url-safe token for cross-app service auth. |
| `OPENHOST_ROUTER_URL` | Internal URL of the router (`http://host.containers.internal:{port}`). |
| `OPENHOST_ZONE_DOMAIN` | Zone domain (e.g. `user.host.imbue.com`). |
| `OPENHOST_MY_REDIRECT_DOMAIN` | Shared OAuth redirect domain. |

Conditional variables (only set when the corresponding data access is granted):

| Variable | Condition | Value |
|----------|-----------|-------|
| `OPENHOST_APP_DATA_DIR` | `app_data`, `sqlite`, or `access_all_data` | `/data/app_data/{app_name}` |
| `OPENHOST_APP_TEMP_DIR` | `app_temp_data` or `access_all_data` | `/data/app_temp_data/{app_name}` |
| `OPENHOST_APP_ARCHIVE_DIR` | `app_archive` or `access_all_data` | `/data/app_archive/{app_name}` |
| `OPENHOST_SQLITE_{NAME}` | Per entry in `sqlite = [...]` | `/data/app_data/{app_name}/sqlite/{name}.db` |

## Data storage tiers

Apps have no filesystem access by default. Each tier has different
characteristics:

- **Permanent data** (`app_data`) -- local NVMe. Fast, small, backed up.
  SQLite databases and other embedded stores must live here (strict POSIX
  consistency required for WAL). Path: `/data/app_data/{app_name}/`.

- **Temporary data** (`app_temp_data`) -- local disk scratch. Not backed
  up. For caches, thumbnails, transcoding work files. Path:
  `/data/app_temp_data/{app_name}/`.

- **Archive data** (`app_archive`) -- S3-backed via JuiceFS. Elastic,
  higher latency on uncached reads. Disabled by default; operator must
  configure S3 from the dashboard first. Apps with `app_archive = true`
  cannot be installed until S3 is configured. Path:
  `/data/app_archive/{app_name}/`.

## Authentication and routing

By default, all routes require the zone owner to be authenticated (JWT
cookie). The router checks auth before proxying.

To make routes publicly accessible, list their prefixes in
`routing.public_paths`. Anonymous visitors can reach these paths without
logging in.

For authenticated requests from the zone owner, the router strips all
inbound `X-OpenHost-*` headers (it is the sole authority) and injects:

| Header | When | Value |
|--------|------|-------|
| `X-OpenHost-Is-Owner` | Owner is authenticated | `"true"` |
| `X-Forwarded-For` | Always | Client IP |
| `X-Forwarded-Proto` | Always | `"https"` (or `"http"` in dev) |
| `X-Forwarded-Host` | Always | Original Host header |

For cross-app service calls, additional headers are injected:

| Header | Value |
|--------|-------|
| `X-OpenHost-Consumer-Name` | Calling app's name |
| `X-OpenHost-Consumer-Id` | Calling app's ID |
| `X-OpenHost-Permissions` | JSON array of granted permission payloads |

## App lifecycle

### Statuses

`building` -> `starting` -> `running` | `error`

Also: `stopped`, `removing`.

### Deploy

1. Repository is cloned (GitHub OAuth flow may be required for private repos).
2. `openhost.toml` is parsed and validated.
3. A local port is allocated and a unique app ID generated.
4. Data directories are provisioned based on the `[data]` section.
5. The container image is built with `podman build` (up to 3 retries on
   transient failures).
6. The container is started with `podman run`.
7. The router polls the app (any HTTP response < 500) for up to 60 seconds.
8. Status transitions to `running` or `error`.

### Reload

1. Optionally runs `git pull` to fetch new code (if `update=1` is passed).
2. Re-reads `openhost.toml` and updates routing/manifest in the database.
3. Stops the existing container.
4. Rebuilds the image and starts a new container.
5. Waits for readiness.

### Stop

Stops and removes the container. Status becomes `stopped`.

### Remove

Stops the container, removes the image, and optionally deletes data
directories. If `keep_data` is specified, only temporary data is removed.

## CLI (`oh`)

The `oh` CLI wraps the HTTP API. Key commands:

```
oh status                              # check reachability
oh app list                            # list apps and statuses
oh app deploy <repo_url>               # deploy from git repo
oh app deploy <url> --name <n> --wait  # deploy with custom name, wait
oh app status <name>                   # get app status
oh app logs <name>                     # view logs
oh app logs <name> --follow            # tail logs
oh app reload <name>                   # rebuild + restart
oh app reload <name> --update --wait   # git pull + rebuild + wait
oh app stop <name>                     # stop app
oh app remove <name>                   # remove app + data
oh app remove <name> --keep-data       # remove but keep data
oh app rename <name> <new_name>        # rename app + subdomain
oh tokens list                         # list API tokens
oh tokens create --name "ci"           # create token
oh tokens delete <id>                  # delete token
```

Install via:
```
uv tool install "oh @ git+https://github.com/imbue-ai/openhost.git#subdirectory=compute_space_cli"
```

Or from a local clone:
```
cd openhost/compute_space_cli && uv tool install --editable .
```

Login: `oh instance add <url> <token>` or `oh instance login` (interactive).

## HTTP API

All endpoints require `Authorization: Bearer <token>`.

### App management

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/api/clone_and_get_app_info` | Clone repo, return manifest info. Form: `repo_url`. |
| `POST` | `/api/add_app` | Deploy an app. Form: `repo_url`, optional `app_name`, `clone_dir`, `grant_permissions_v2=1`, `port_override.<label>=<port>`. |
| `GET` | `/api/apps` | List all apps with status. |
| `GET` | `/api/app_status/<app_id>` | Get single app status. |
| `GET` | `/app_logs/<app_id>` | Get app logs (text/plain). |
| `POST` | `/reload_app/<app_id>` | Reload app. Form: `update=1` to git pull first. |
| `POST` | `/stop_app/<app_id>` | Stop app. |
| `POST` | `/remove_app/<app_id>` | Remove app. Form: `keep_data=1` optional. Returns 202. |
| `POST` | `/rename_app/<app_id>` | Rename app. Form: `name`. |
| `GET` | `/api/check_port?port=N` | Check if a host port is available. |

Note: `<app_id>` in URL paths also accepts the app name.

## Rootless podman constraints

A few things that work under classical Docker do not work under rootless
podman:

- Host ports below 25 are rejected (the platform lowers the unprivileged
  port floor from 1024 to 25 so SMTP works).
- Capabilities are restricted to a safe allowlist. Capabilities like
  `SYS_ADMIN` or `SYS_PTRACE` that require real host privilege are
  rejected at manifest parse time.
- Devices are restricted to a safe allowlist (`/dev/net/tun`, `/dev/fuse`,
  serial ports).
- Container-root maps to an unprivileged subuid on the host, not real root.

## Infrastructure deployment

OpenHost is deployed to servers via Ansible. The target is Ubuntu 24.04.
The Ansible playbook installs and configures:

- Rootless podman
- CoreDNS (wildcard DNS for app subdomains)
- Caddy (TLS termination with ACME DNS-01 wildcard certs)
- The router as a systemd service
- Data directories under `/opt/openhost`

Dev mode (`openhost up --dev`) skips CoreDNS and Caddy, serving HTTP
directly on :8080.

## Repository structure

```
openhost/
  compute_space/     -- Quart/Hypercorn app (router, dashboard, container management)
  compute_space_cli/ -- `oh` CLI for managing remote instances
  self_host_cli/     -- `openhost` CLI for local dev mode
  ansible/           -- server deployment playbooks
  apps/              -- built-in apps (test-app, file-browser, secrets, etc.)
  tests/             -- integration and e2e tests
  docs/              -- design docs and specs
  services/          -- cross-app service specifications
```

## Example manifests

### Minimal app

```toml
[app]
name = "my-app"
version = "0.1.0"

[runtime.container]
image = "Dockerfile"
port = 8080

[routing]
health_check = "/health"
```

### App with persistent storage

```toml
[app]
name = "notes"
version = "0.1.0"

[runtime.container]
image = "Dockerfile"
port = 8080

[resources]
memory_mb = 256
cpu_millicores = 500

[data]
sqlite = ["main"]
app_data = true
```

### App with public routes and extra ports

```toml
[app]
name = "my-service"
version = "0.1.0"

[runtime.container]
image = "Dockerfile"
port = 8080

[routing]
health_check = "/healthz"
public_paths = ["/api/public/", "/webhook"]

[[ports]]
label = "metrics"
container_port = 9090
host_port = 9090

[resources]
memory_mb = 512
cpu_millicores = 1000

[data]
app_data = true
app_temp_data = true
```

### Full-access utility app

```toml
[app]
name = "file-browser"
version = "0.1.0"

[runtime.container]
image = "Dockerfile"
port = 5000
command = "/data -A"

[data]
access_all_data = true
```
