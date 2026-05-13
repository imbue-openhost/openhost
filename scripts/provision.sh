#!/usr/bin/env bash
# provision.sh — Bootstrap a fresh Ubuntu 24.04 server into a running OpenHost instance.
#
# Usage (run as root on the target server):
#   curl -fsSL https://raw.githubusercontent.com/imbue-openhost/openhost/main/scripts/provision.sh | bash -s -- --domain myhost.example.com
#
# Prerequisites:
#   - Fresh Ubuntu 24.04 server with root access
#   - DNS A record: <domain> -> server IP
#   - DNS NS + A records for subdomain delegation (see docs)
#
# What it does:
#   1. Creates the 'host' user with SSH keys from root
#   2. Installs ansible-core and git
#   3. Clones the openhost repository
#   4. Runs ansible/local_setup.yml (reuses the same tasks as remote setup.yml)
#   5. Generates an ACME account key for TLS certificates
#
# The ansible playbook handles: apt packages, podman, pixi, config, systemd service.

set -euo pipefail

DOMAIN=""
BRANCH="main"
REPO_URL="https://github.com/imbue-openhost/openhost.git"
OPENHOST_DIR="/home/host/openhost"

usage() {
    echo "Usage: $0 --domain <domain> [--branch <branch>] [--repo <repo-url>]"
    echo ""
    echo "  --domain   Required. Domain name (e.g., myhost.example.com)"
    echo "  --branch   Git branch to deploy (default: main)"
    echo "  --repo     Git repo URL (default: imbue-openhost/openhost)"
}

while [[ $# -gt 0 ]]; do
    case $1 in
        --domain)   DOMAIN="$2"; shift 2 ;;
        --branch)   BRANCH="$2"; shift 2 ;;
        --repo)     REPO_URL="$2"; shift 2 ;;
        -h|--help)  usage; exit 0 ;;
        *)          echo "Unknown option: $1"; usage; exit 1 ;;
    esac
done

if [ -z "$DOMAIN" ]; then
    echo "Error: --domain is required"
    usage
    exit 1
fi

if [ "$(id -u)" -ne 0 ]; then
    echo "Error: this script must be run as root"
    exit 1
fi

echo "=== OpenHost Provisioning ==="
echo "  Domain: $DOMAIN"
echo "  Branch: $BRANCH"
echo ""

# ---- Create host user ----
if ! id -u host >/dev/null 2>&1; then
    echo "--- Creating host user ---"
    useradd -m -s /bin/bash -G sudo host
    echo "host ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/host
    chmod 0440 /etc/sudoers.d/host

    # Copy SSH authorized_keys so the user can SSH in after provisioning
    if [ -f /root/.ssh/authorized_keys ]; then
        mkdir -p /home/host/.ssh
        cp /root/.ssh/authorized_keys /home/host/.ssh/authorized_keys
        chown -R host:host /home/host/.ssh
        chmod 700 /home/host/.ssh
        chmod 600 /home/host/.ssh/authorized_keys
    fi
fi

# ---- Install prerequisites ----
echo "--- Installing ansible and git ---"
apt-get update -qq
apt-get install -y -qq ansible-core git > /dev/null 2>&1

# ---- Clone the repo ----
echo "--- Cloning OpenHost ($BRANCH) ---"
if [ -d "$OPENHOST_DIR/.git" ]; then
    cd "$OPENHOST_DIR"
    su host -c "git fetch origin"
    su host -c "git checkout $BRANCH"
    su host -c "git reset --hard origin/$BRANCH"
else
    su host -c "git clone --branch $BRANCH $REPO_URL $OPENHOST_DIR"
fi
chown -R host:host "$OPENHOST_DIR"

# ---- Detect public IP ----
PUBLIC_IP=""
PUBLIC_IP=$(hostname -I 2>/dev/null | awk '{print $1}' || true)
if [ -z "$PUBLIC_IP" ] || echo "$PUBLIC_IP" | grep -qE '^(10\.|172\.(1[6-9]|2[0-9]|3[01])\.|192\.168\.)'; then
    PUBLIC_IP=$(curl -sf --max-time 5 https://ifconfig.me 2>/dev/null || true)
fi
echo "  Public IP: ${PUBLIC_IP:-unknown}"

# ---- Run ansible ----
echo "--- Running setup playbook ---"
cd "$OPENHOST_DIR"
ansible-playbook ansible/local_setup.yml \
    -e "domain=$DOMAIN" \
    -e "public_ip=${PUBLIC_IP:-127.0.0.1}" \
    --connection=local \
    -i "localhost,"

# ---- Generate ACME account key if missing ----
ACME_KEY_PATH="$OPENHOST_DIR/ansible/secrets/certbot_private_key.json"
if [ ! -f "$ACME_KEY_PATH" ]; then
    echo "--- Generating ACME account key ---"
    mkdir -p "$(dirname "$ACME_KEY_PATH")"
    # Use pixi's python which has cryptography installed
    su host -c "/home/host/.pixi/bin/pixi run --manifest-path $OPENHOST_DIR/pixi.toml python3 -c '
from cryptography.hazmat.primitives.asymmetric import rsa
import json, base64

key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
nums = key.private_numbers()
pub = nums.public_numbers

def b64(n, length):
    return base64.urlsafe_b64encode(n.to_bytes(length, byteorder=\"big\")).rstrip(b\"=\").decode()

jwk = {\"kty\": \"RSA\", \"n\": b64(pub.n, 256), \"e\": b64(pub.e, 3),
       \"d\": b64(nums.d, 256), \"p\": b64(nums.p, 128), \"q\": b64(nums.q, 128),
       \"dp\": b64(nums.dmp1, 128), \"dq\": b64(nums.dmq1, 128), \"qi\": b64(nums.iqmp, 128)}

with open(\"$ACME_KEY_PATH\", \"w\") as f:
    json.dump(jwk, f, indent=2)
print(\"Generated ACME account key\")
'"
    chmod 600 "$ACME_KEY_PATH"
    chown host:host "$ACME_KEY_PATH"

    # Restart to pick up the key
    systemctl restart openhost 2>/dev/null || true
fi

echo ""
echo "=== OpenHost provisioning complete ==="
echo ""
echo "  Dashboard: https://$DOMAIN"
echo "  SSH:       ssh host@$DOMAIN"
echo ""
echo "  Check status:  systemctl status openhost"
echo "  View logs:     journalctl -u openhost -f"
