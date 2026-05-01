
## prerequisites

- ansible installed locally: `uv tool install ansible-core` or however you like.
- a fresh ubuntu 24.04 server with root SSH access
- DNS records pointing your domain (and `*.domain`) to the server IP
    - NS record host.example.com -> ns1.host.example.com
    - A record ns1.host.example.com -> machine IP
- ACME account key at `ansible/secrets/certbot_private_key.json` (not in git; CI pulls from `ACME_ACCOUNT_KEY` GitHub secret — for local deploys, retrieve from there or your secret store)

## runtime

app containers run under rootless podman as the `host` user, sharing podman's default single-user namespace (the one `/etc/subuid` allocates at user-creation time).  every bind mount uses idmapped mounts so container-root writes land on disk owned by the `host` user rather than the mapped subuid.  the server must be running on a filesystem that supports idmapped mounts (ext4, xfs, btrfs, tmpfs).  the bootstrap fails early with a clear error if this is not the case.

## full setup (fresh server)

```bash
ansible-playbook ansible/setup.yml -i <IP>, -e domain=<domain> -e initial_user=root --private-key=~/.ssh/YOUR_SSH_KEY
```

the trailing comma after the IP is required (tells ansible it's a host list, not a file).

## fast re-deploy

syncs code, updates config, restarts the service:

```bash
ansible-playbook ansible/deploy.yml -i <IP>, -e domain=<domain> --private-key=~/.ssh/YOUR_SSH_KEY
```

## after setup

```bash
# check service status
ssh host@<IP> systemctl status openhost

# view logs
ssh host@<IP> journalctl -u openhost -f

# verify
curl https://<domain>/health
```
