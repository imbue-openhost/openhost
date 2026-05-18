---
name: openhost-deployment
description: Reference for provisioning and deploying OpenHost instances. Covers server requirements, DNS setup, Ansible deployment, and the provision script.
---

# OpenHost Instance Deployment

How to stand up a new OpenHost instance on a server.

## Requirements

- Fresh Ubuntu 24.04 server (cloud VPS or bare metal) with root SSH access
- A domain with DNS control
- Ansible installed locally (`uv tool install ansible-core`)
- ACME account key at `ansible/secrets/certbot_private_key.json`

## DNS records

Two records are needed before deployment:

| Type | Name | Value |
|------|------|-------|
| NS | `host.example.com` | `ns1.host.example.com` |
| A | `ns1.host.example.com` | server IP |

This delegates `*.host.example.com` to the CoreDNS instance that OpenHost runs on the server.

## Option 1: Provision script (run on the server)

SSH into the server as root and run:

```bash
curl -fsSL https://raw.githubusercontent.com/imbue-openhost/openhost/main/scripts/provision.sh | bash -s -- --domain host.example.com
```

Optional flags:
- `--branch <branch>` -- deploy a specific branch (default: main)
- `--repo <url>` -- use a different repo URL

The script:
1. Creates the `host` user, copies SSH keys from root
2. Installs ansible-core and git
3. Clones the openhost repo
4. Runs the ansible playbook (apt packages, podman, pixi, CoreDNS, Caddy, systemd service)
5. Generates an ACME account key (Let's Encrypt) if missing
6. Starts the openhost systemd service

## Option 2: Ansible from a local machine

From a local clone of the repo:

```bash
# Full setup on a fresh server
ansible-playbook ansible/setup.yml \
  -i <IP>, \
  -e domain=<domain> \
  -e initial_user=root \
  --private-key=~/.ssh/YOUR_KEY

# Fast re-deploy (pull code, update config, restart)
ansible-playbook ansible/deploy.yml \
  -i <IP>, \
  -e domain=<domain> \
  --private-key=~/.ssh/YOUR_KEY
```

The trailing comma after the IP is required (tells ansible it's a host list, not a file).

To deploy a specific commit: `-e openhost_commit=$(git rev-parse HEAD)`. The commit must be pushed.

## Option 3: vm-manager

The vm-manager app at `https://vm-manager.openhost-team.selfhost.imbue.com/` can provision instances on GCP, EC2, and Hetzner. It handles server creation, DNS, and ansible deployment automatically.

Create via the web UI at `/create` or POST:

```bash
curl -X POST https://vm-manager.openhost-team.selfhost.imbue.com/create \
  -H "Authorization: Bearer <token>" \
  -d "name=myinstance&provider=hetzner&hetzner_location=hil&server_type=cpx21&branch=main"
```

Teardown:

```bash
curl -X POST https://vm-manager.openhost-team.selfhost.imbue.com/instance/<name>/teardown \
  -H "Authorization: Bearer <token>" \
  -d "provider=<provider>&location=<loc>&ip=<ip>&provider_id="
```

## What gets installed

The ansible playbook configures:

- **podman** (rootless) -- container runtime, runs as the `host` user
- **CoreDNS** (:53) -- authoritative DNS, wildcard `*.domain` to server IP
- **Caddy** (:443/:80) -- TLS termination via ACME DNS-01, reverse proxy to the router
- **OpenHost router** (:8080) -- Python app (Quart/Hypercorn), runs as a systemd service (`openhost.service`)
- **pixi** -- package manager for the Python environment
- Data directories under `/opt/openhost`
- The `openhost0` dummy interface at `10.200.0.1` for container-to-host networking

## Verifying the instance

```bash
# SSH in
ssh host@<domain>

# Check service
systemctl status openhost

# View logs
journalctl -u openhost -f

# Health check
curl https://<domain>/health
```

The dashboard is at `https://<domain>/`. On first visit you set the owner password.

## Dev mode

For local development without DNS or TLS:

```bash
cd openhost
openhost up --dev
```

Serves HTTP on `:8080`. No CoreDNS or Caddy.

## Filesystem requirements

Rootless podman uses idmapped mounts for bind-mounted data directories. The server must use a filesystem that supports idmapped mounts (ext4, xfs, btrfs, tmpfs). The bootstrap fails early if this is not the case.
