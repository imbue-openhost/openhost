
## prerequisites

- ansible installed locally: `uv tool install ansible-core` or however you like.
- a fresh ubuntu 24.04 server with root SSH access
- DNS records pointing your domain (and `*.domain`) to the server IP
    - NS record host.example.com -> ns1.host.example.com
    - A record ns1.host.example.com -> machine IP
- ACME account key at `ansible/secrets/certbot_private_key.json` (not in git; CI pulls from `ACME_ACCOUNT_KEY` GitHub secret — for local deploys, retrieve from there or your secret store). not needed in http-only mode (see below).

## runtime

app containers run under rootless podman as the `host` user, sharing podman's default single-user namespace (the one `/etc/subuid` allocates at user-creation time).  every bind mount uses idmapped mounts so container-root writes land on disk owned by the `host` user rather than the mapped subuid.  the server must be running on a filesystem that supports idmapped mounts (ext4, xfs, btrfs, tmpfs).  the bootstrap fails early with a clear error if this is not the case.

## full setup (fresh server)

```bash
ansible-playbook ansible/setup.yml -i <IP>, -e domain=<domain> -e initial_user=root --private-key=~/.ssh/YOUR_SSH_KEY
```

add `-e openhost_branch=<branch>` to deploy a branch, or `-e openhost_commit=$(git rev-parse HEAD)` to pin an exact commit. otherwise it defaults to remote's `main`. the ref must be pushed to github first.

the trailing comma after the IP is required (tells ansible it's a host list, not a file).

## fast re-deploy

pulls fresh code (default to origin's main), updates config, restarts the service:

```bash
ansible-playbook ansible/deploy.yml -i <IP>, -e domain=<domain> --private-key=~/.ssh/YOUR_SSH_KEY
```

## variables

pass with `-e key=value`.

| variable | default | purpose |
|---|---|---|
| `domain` | *(required)* | zone domain |
| `initial_user` | `root` | SSH user for the first play (creates the `host` user) |
| `public_ip` | target IP | public IP written into config / DNS |
| `openhost_branch` | `main` | git branch to deploy |
| `openhost_commit` | `main` | exact commit SHA, overridden by openhost_branch |
| `openhost_refspec` | *(none)* | extra ref to fetch, e.g. `refs/pull/N/merge` |
| `local_http_only` | `false` | localhost mode: no TLS / CoreDNS / Caddy |
| `bind_host` | `127.0.0.1` | router bind address |
| `claim_token` | random (printed) | claim_token for `/setup` |
| `acme_email` | `openhost@<domain>` | ACME account email (TLS mode) |
| `acme_directory_url` | LE production | override, e.g. staging or a local pebble |
| `skip_apt_upgrade` | `false` | skip `apt upgrade` in the packages step |
| `skip_service_start` | `false` | don't start the service at the end |

version precedence: `openhost_branch` > `openhost_commit` > default `main`.

### cert_api broker (opt-in, alternative to the bring-your-own ACME key)

| variable | purpose |
|---|---|
| `cert_provider` | set to `cert_api` to fetch certs via the openhost-cert-api broker |
| `cert_api_base_url` | override the broker URL (optional) |
| `cert_api_keycloak_issuer_url` | keycloak issuer (required with `cert_api`) |
| `cert_api_keycloak_client_id` | keycloak client id (required with `cert_api`) |
| `cert_api_keycloak_client_secret` | keycloak client secret (required with `cert_api`) |

## after setup

```bash
# check service status
ssh host@<IP> systemctl status openhost

# view logs
ssh host@<IP> journalctl -u openhost -f

# verify
curl https://<domain>/health
```
