**NOTE: this is an exploratory project, it is not yet ready for general use! it is being made public for narrow beta testing, you are welcome to try it, but please don't file issues or open PRs; they will not be responded to.**

**This code is currently being provided under the AGPL-3.0 license (loosely, you can use it for any personal use, but if it's distributed commercially, the commercial use must also be made open-source). We may change to a different license in the future (eg [a fair source license](https://fair.io/licenses/)), with the intent that personal usage will always be totally allowed, with no restrictions or exclusions, but commercial use may be restricted so that we can develop a sustainable business model to continue to build and maintain the project.**

**the "openhost" name is a placeholder and will be changed before this project is launched**.

# openhost

FOSS apps on your own compute and data. users bring their own hosting (cloud server or local hardware), and run open source apps via a self-hosting platform that handles routing, containers, storage, and auth.

## quick start

### local development

```bash
# run the router directly on your machine (needs rootless podman)
openhost up --dev

# optional: test app subdomains locally with lvh.me
# (for example: http://my-app.lvh.me:8080)
openhost up --dev --zone-domain lvh.me

# check prerequisites
openhost doctor

# stop everything
openhost down

# update code (git pull + dependency sync)
openhost update
```

### server deployment

```bash
# full setup on a fresh Ubuntu 24.04 server
ansible-playbook ansible/setup.yml -i <IP>, -e domain=<domain> -e initial_user=root \
  --private-key=~/.ssh/YOUR_SSH_KEY

# fast re-deploy (sync code + restart service)
ansible-playbook ansible/deploy.yml -i <IP>, -e domain=<domain> \
  --private-key=~/.ssh/YOUR_SSH_KEY

# verify
curl https://<domain>/health
```

see `ansible/readme.md` for prerequisites and full details.

## how it works

the router is a Python app (Quart/Hypercorn) that provides a web dashboard for deploying and managing apps. it reads `openhost.toml` manifests from app repos, builds container images from each app's `Dockerfile` using rootless podman, runs each app in its own user namespace, and reverse-proxies HTTP requests to the right app by subdomain or path prefix.

### server mode

when deployed to a server (via ansible), the router runs as a systemd service alongside CoreDNS and Caddy:

```
  ┌─────────────────────────────────────────────┐
  │  Server (Ubuntu 24.04)                      │
  │                                             │
  │  CoreDNS (:53)  -- ACME DNS-01 challenges   │
  │  Caddy (:443)   -- TLS termination          │
  │  Router (:8080) -- app mgmt + reverse proxy │
  │  rootless Podman -- app containers           │
  └─────────────────────────────────────────────┘
```

CoreDNS serves DNS for the deployment domain, enabling automatic TLS certificate acquisition via ACME DNS-01. Caddy terminates TLS on :443 and proxies to the router on :8080.

all persistent data (database, TLS certs, app data) lives under the configured `data_root_dir` (default: `/opt/openhost`).

### dev mode

`openhost up --dev` runs the router directly on your machine. HTTP only on port 8080, no TLS, no CoreDNS, no Caddy. requires rootless podman for running app containers.

## cloudflare tunnel setup (no port forwarding)

Use this when you want public internet access without opening home router ports. See `docs/cloudflare-local-tls-plan.md` for the full runbook and `docs/cloudflare-local-laack-xyz-setup-log.md` for a real-world setup log.

## local subdomain testing (optional)

If you want to test host-based app routing locally (for example, `app.lvh.me`),
run dev mode with a local zone domain:

```bash
openhost up --dev --zone-domain lvh.me
```

Then open app URLs like:

- `http://my-app.lvh.me:8080`
- `http://another-app.lvh.me:8080`

Notes:

- This is optional and intended for local development only.
- The default local flow remains `http://localhost:8080`.
- Port `80` is not required; if you want no port in the URL, use your own local
  port-forwarding setup from `:80` to `:8080`.

## operating modes

| mode | command | what it does |
|------|---------|-------------|
| dev | `openhost up --dev` | runs router directly on host, HTTP only (needs rootless podman) |
| server | `ansible-playbook ansible/setup.yml` | deploy to any VPS or bare metal with automatic HTTPS (see `ansible/readme.md`) |

## components

| component | path | what it does | status |
|-----------|------|-------------|--------|
| CLI | `self_host_cli/` | `openhost` command: up, down, doctor, update | buggy — has stale VM flags, being reworked |
| compute_space CLI | `compute_space_cli/` | compute space management CLI | working |
| compute_space | `compute_space/compute_space/` | HTTP routing, app deployment, auth, reverse proxy | working |
| dau_tracker | `apps/dau_tracker/` | DAU tracking + version check | working |
| partaay | `apps/partaay/` | distributed event hosting | working |

## development setup

```bash
# install pre-commit hooks (requires pre-commit: pip install pre-commit)
pre-commit install
```

## testing

```bash
# run all lightweight tests (from project root)
pixi run -e dev pytest

# run with podman integration tests enabled
pixi run -e dev pytest --run-containers
```

Server deployment prerequisites and test setup are documented in `ansible/readme.md`.

## key docs

- `docs/cloudflare-local-tls-plan.md` - Cloudflare Tunnel setup runbook
- `docs/cloudflare-local-laack-xyz-setup-log.md` - example real-world setup log and troubleshooting
- `docs/creating_an_app.md` - guide to building apps
- `docs/manifest_spec.md` - `openhost.toml` app manifest specification
