# Creating an App for OpenHost

This guide walks through building an app that runs on OpenHost.

## Deploying your app

From the dashboard, click "Deploy New App" and provide a git repo URL (public or private - private GitHub repos will prompt for auth).

The router reads `openhost.toml`, builds the container image from your `Dockerfile` using rootless podman, and starts routing requests to it. Apps are accessible at `https://{app_name}.{zone_domain}/` (e.g., `https://my-app.user.host.imbue.com/`).


## Writing an app to run on OpenHost

Apps can be anything that can run in an OCI container, and accessed via HTTP(s). OpenHost runs every app under rootless podman, so container-root maps to an unprivileged subuid on the host rather than real root.

An `openhost.toml` manifest must be placed at the root of your repo, to indicate to OpenHost how to run your app. See the [manifest spec](manifest_spec.md) for the full field reference.

### Rootless constraints

A few things that work under classical Docker don't work here:

- `[[ports]].host_port` values below 25 are rejected at manifest parse time — rootless podman cannot bind to privileged ports under 25 (the router lowers the unprivileged-port floor from 1024 to 25 so SMTP, HTTP, and HTTPS all work).
- `[runtime.container].capabilities` is a tight allowlist.  Safe caps for rootless user namespaces (`NET_ADMIN`, `NET_RAW`, `NET_BIND_SERVICE`, `CHOWN`, `DAC_OVERRIDE`, `SETUID`, `SETGID`, `KILL`, `MKNOD`, `SYS_CHROOT`, `IPC_LOCK`, a few others) are accepted; capabilities that require real host privilege (`SYS_ADMIN`, `SYS_MODULE`, `SYS_PTRACE`, ...) are rejected.  The exact list lives in `compute_space.core.manifest.SAFE_CAPABILITIES`.
- `[runtime.container].devices` declares **extra** host devices to pass through on top of the OCI baseline.  The character devices `/dev/null`, `/dev/zero`, `/dev/random`, `/dev/urandom`, `/dev/full`, `/dev/tty` and `/dev/console` are mounted inside every container automatically and do **not** need to be listed.  Extras are restricted to a tight allowlist (`/dev/net/tun`, `/dev/fuse`, `/dev/ttyS*`, `/dev/ttyUSB*`, `/dev/ttyACM*`).  Requests for anything outside the list — `/dev/mem`, `/dev/kvm`, raw block devices, etc. — are rejected at manifest parse time.

Here's an example of a simple app:

### Directory structure

```
my-app/
├── openhost.toml
├── Dockerfile
├── pyproject.toml          # or package.json, go.mod, etc.
├── app.py                  # your app code
└── entrypoint.sh           # optional startup script
```

### openhost.toml

```toml
[app]
name = "my-app"
version = "0.1.0"
description = "What it does"

[runtime.container]
image = "Dockerfile"          # path to Dockerfile relative to repo root
port = 8080                   # port your app listens on inside the container

[routing]
public_paths = ["/webhook"]   # routes accessible without auth

[resources]
memory_mb = 128
cpu_millicores = 100

[data]
sqlite = ["main"]
app_data = true
```

### Dockerfile

```dockerfile
FROM python:3.12-alpine

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app
COPY pyproject.toml .
RUN uv sync
COPY . .

EXPOSE 8080
CMD ["uv", "run", "python", "-u", "app.py"]
```

### App code

Your app should listen on `0.0.0.0:<port>` where `<port>` matches `runtime.container.port` in the manifest. The router handles TLS and proxies requests to your container as HTTP.

```python
from flask import Flask
import os

app = Flask(__name__)

@app.route("/")
def index():
    return "<h1>Hello from OpenHost</h1>"

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
```

### Notes

- Apps are available at `{app_name}.{compute_space_url}`.
- Data directories are mounted into the container at `/data/`. See [Environment variables](#environment-variables) below.
- The router handles authentication. By default, all routes require the compute space owner to be logged in. To make specific routes public, list them in `public_paths` in the manifest.
- For apps that implement their own auth, routes can be set as public, and requests that have been authenticated by the router will bear a `X-OpenHost-Is-Owner=true` header.
- The router also injects `X-OpenHost-Identity`: a short-lived (~60s) RS256 JWT bound to *this* app via the `aud` claim. Apps that want cryptographic proof of the caller's identity (rather than trusting the `X-OpenHost-Is-Owner` header on faith) should verify this JWT using the router's public key from `/.well-known/jwks.json`, and assert that `aud == OPENHOST_APP_NAME`. Tokens minted for a different app are rejected on the audience check.

The router injects these environment variables into your app:

| Variable | Example | Description                                                                                                     |
|----------|---------|-----------------------------------------------------------------------------------------------------------------|
| `OPENHOST_APP_NAME` | `my-app` | Your app's name, as registered with OpenHost. This will be the subdomain the app is routeable at.               |
| `OPENHOST_APP_TOKEN` | `kF3xP_2qA-bN4...` (43-char url-safe token) | Random per-app token used to authenticate cross-app service calls                                               |
| `OPENHOST_ROUTER_URL` | `http://host.containers.internal:8080` | internal URL of the router, used for constructing service requests. |
| `OPENHOST_ZONE_DOMAIN` | `user.host.imbue.com` | The compute space's domain                                                                                      |
| `OPENHOST_MY_REDIRECT_DOMAIN` | `my.selfhost.imbue.com` | The shared `my.*` OAuth redirect domain. This hosts a browser-local page that redirects the user to their zone. |
| `OPENHOST_APP_DATA_DIR` | `/data/app_data/my-app` | Path to the app's persistent data directory. Set when `app_data`, `sqlite`, or `access_all_data` is requested   |
| `OPENHOST_APP_TEMP_DIR` | `/data/app_temp_data/my-app` | Path to the app's temporary data directory. Set when `app_temp_data` or `access_all_data` is requested          |
| `OPENHOST_SQLITE_<NAME>` | `/data/app_data/my-app/sqlite/main.db` (for `sqlite = ["main"]`) | Path to a provisioned SQLite database file. Set once per entry in `sqlite`                                      |

### Data storage

Apps have no filesystem access by default. Request what you need in the `[data]` section of your manifest:

- **`sqlite = ["db_name"]`** — Provisions a SQLite database. Access the file at `OPENHOST_SQLITE_<NAME>`.
- **`app_data = true`** — mounts a persistent directory at `/data/app_data/{app_name}/`. Backed up.
- **`app_temp_data = true`** — mounts a temporary directory at `/data/app_temp_data/{app_name}/`. Not backed up, can be recreated.
- **`access_vm_data = true`** — read-only access to the VM's shared data at `/data/vm_data/`.
- **`access_all_data = true`** — full access to all data directories (all apps' data + VM data).

The host operator can optionally set `storage_min_free_mb` in the OpenHost config to require a minimum amount of free persistent storage. When free space drops below this threshold, running apps are stopped until space is freed. The storage guard can be temporarily paused from the System page to allow starting a file-browser app for cleanup.

See the [manifest spec](manifest_spec.md) for the full reference.

### Services

See [Cross-App Services](./cross_app_services.md) for how services work.

See [OAuth in Apps](./oauth.md) for an example - getting oauth tokens to external services (eg gmail or github).

### Identity

For apps that should be available to multiple users and require more than trivial publicly routes or token-protected public routes, see [User Identity](./user_identity.md) for how to implement this with the OpenHost user identity provider.


## Development / Debugging workflow

In general, the debugging flow is something like:
1. Create your app in its own repo
2. Install it into your compute space (from the dashboard or CLI)
3. Test it
4. Fix bugs / make changes, commit and push
5. "Update and reload" from the app details page (pulls new code and rebuilds)
6. Retest and repeat

We find AI tools work best when they can directly reference openhost code+docs. So we'd suggest:
```cd some_dir && git clone https://github.com/imbue-ai/openhost.git```
then point them at that checkout and tell them to read this doc.

## CLI

There is a CLI interface, `oh`, that can be used for interacting with your compute space, if you prefer that style of workflow.

If you have a local clone, do
```bash
cd OPENHOST_CLONE_DIR/compute_space_cli && uv tool install --editable .
```
this will automatically get updates if you pull new changes from the openhost repo.

or if not,
```bash
uv tool install "oh @ git+https://github.com/imbue-ai/openhost.git#subdirectory=compute_space_cli"
```
Run `oh instance login` to login to your compute space.

## AI Agent Development

We'd suggest letting your AI agent do the full "fix bugs, commit+push, update and reload, test" loop. The `oh` CLI makes this easy to automate, although the CLI will need to be logged in by the user manually first.

Here's some example `oh` commands, although you should run `oh --help` to get the most up-to-date command list.

```bash
oh status                                    # check if compute space is reachable

oh app list                                  # list apps and status
oh app deploy https://github.com/you/myapp   # deploy from git repo
oh app deploy https://github.com/you/myapp --name cool-app --wait
oh app status cool-app                       # check status
oh app logs cool-app                         # view logs
oh app logs cool-app --follow                # tail logs
oh app reload cool-app                       # rebuild + restart
oh app reload cool-app --update --wait       # git pull, rebuild, wait
oh app stop cool-app                         # stop app
oh app remove cool-app                       # remove app + data
oh app remove cool-app --keep-data           # remove but keep data
oh app rename cool-app new-name              # rename app

oh tokens list                               # list API tokens
oh tokens create --name "ci" --expiry-hours 72
oh tokens delete 3                           # delete by token ID
```

Note: cloning a private GitHub repo for the first time requires an OAuth flow in the browser. The CLI will print a link to authorize. After that, subsequent deploys and updates work without browser interaction.
