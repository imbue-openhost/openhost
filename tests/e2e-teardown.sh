#!/bin/bash
# Unified e2e teardown: delete instance + DNS records.
#
# Provider-agnostic — reads PROVIDER from the env file written by e2e-setup.sh
# and dispatches to the appropriate provider_teardown function.
#
# Idempotent — safe to run multiple times or after partial failures.
#
# Usage:
#   tests/e2e-teardown.sh
set -uo pipefail
# Note: -e is intentionally omitted — we want to attempt all cleanup steps.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$SCRIPT_DIR/common.sh"

ENV_FILE="${E2E_ENV_FILE:-/tmp/e2e_env.sh}"
if [ -f "$ENV_FILE" ]; then
    source "$ENV_FILE"
fi

PROVIDER="${PROVIDER:-}"
if [ -z "$PROVIDER" ]; then
    echo "WARNING: PROVIDER not set — cannot teardown instance" >&2
fi

echo "=== Tearing down e2e resources (provider: ${PROVIDER:-unknown}) ==="

# ── 1. Teardown cloud instance ───────────────────────────────────────────

if [ -n "$PROVIDER" ]; then
    PROVIDER_SCRIPT="$SCRIPT_DIR/providers/${PROVIDER}.sh"
    if [ -f "$PROVIDER_SCRIPT" ]; then
        source "$PROVIDER_SCRIPT"
        provider_teardown
    else
        echo "  WARNING: Provider script not found: $PROVIDER_SCRIPT"
    fi
fi

# ── 2. Delete Route53 DNS records ─────────────────────────────────────────

DOMAIN="${OPENHOST_DOMAIN:-}"
PUBLIC_IP="${OPENHOST_PUBLIC_IP:-}"
HOSTED_ZONE_ID="${E2E_HOSTED_ZONE_ID:-}"

if [ -n "$DOMAIN" ] && [ -n "$HOSTED_ZONE_ID" ] && [ -n "$PUBLIC_IP" ]; then
    delete_route53_dns "$DOMAIN" "$PUBLIC_IP" "$HOSTED_ZONE_ID"
else
    echo "  Missing domain/zone/IP info — skipping DNS cleanup"
fi

# ── 3. Clean up env file ─────────────────────────────────────────────────

if [ -f "$ENV_FILE" ]; then
    rm -f "$ENV_FILE"
    echo "  Cleaned up $ENV_FILE"
fi

echo "=== Teardown complete ==="
