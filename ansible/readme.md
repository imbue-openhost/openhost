
## prerequisites

- ansible installed locally: `uv tool install ansible-core`
- a fresh ubuntu 24.04 server with root SSH access
- DNS records pointing your domain (and `*.domain`) to the server IP
    - NS record host.example.com -> ns1.host.example.com
    - A record ns1.host.example.com -> machine IP
- ACME account key at `ansible/secrets/certbot_private_key.json` (not in git; CI pulls from `ACME_ACCOUNT_KEY` GitHub secret — for local deploys, retrieve from there or your secret store)

## full setup (fresh server)

```bash
ansible-playbook ansible/setup.yml -i <IP>, -e domain=<domain> -e initial_user=root --private-key=~/.ssh/YOUR_SSH_KEY
```

the trailing comma after the IP is required (tells ansible it's a host list, not a file).

### container runtime

by default the bootstrap installs docker ce and adds the host user to the docker group. pass `-e openhost_container_runtime=podman` to install rootless podman instead. this is the recommended path for new servers going forward; docker remains the default for now until the migration is fully validated. the two are mutually exclusive per server.

```bash
ansible-playbook ansible/setup.yml -i <IP>, -e domain=<domain> -e openhost_container_runtime=podman --private-key=~/.ssh/YOUR_SSH_KEY
```

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
