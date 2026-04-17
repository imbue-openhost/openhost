#!/usr/bin/env bash
# Sculptor workspace start command: redirect beads to home repo if it exists.
#
# Usage (Sculptor workspace start command):
#   ./scripts/sculptor-workspace-setup.sh

set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel)"

# Get home repo path from Sculptor's `local` remote
HOME_REPO=$(git remote get-url local 2>/dev/null) || {
    echo "warn: no 'local' remote found — skipping beads setup" >&2
    exit 0
}

HOME_BEADS="$HOME_REPO/.beads"

if [ ! -d "$HOME_BEADS" ]; then
    exit 0
fi

# Create minimal .beads/ with redirect to home repo's database
mkdir -p "$REPO_ROOT/.beads"
echo "$HOME_BEADS" > "$REPO_ROOT/.beads/redirect"
echo "beads: redirected to $HOME_BEADS"
