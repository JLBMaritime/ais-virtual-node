#!/usr/bin/env bash
# scripts/update.sh
#
# Pull the latest source from the git remote, refresh the Python venv, and
# bounce the systemd unit. Run as the service user (jlbmaritime).
#
# Usage:
#   cd ~/ais-virtual-node && ./scripts/update.sh
#
# Notes:
#   - This script never touches config.json (it's in .gitignore for a reason).
#   - If you've made local edits, `git pull --ff-only` will refuse to rewrite
#     them - resolve manually with `git stash` / `git pull` / `git stash pop`.
set -euo pipefail

# Resolve the app directory from the script's own location so this works no
# matter where the user runs it from.
APPDIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$APPDIR"

SERVICE="ais-virtual-node"

echo "==> Pulling latest source in $APPDIR"
git fetch --all --prune
git pull --ff-only

echo "==> Refreshing Python dependencies"
# Re-use the venv install.sh created. We deliberately don't recreate it -
# `pip install -U` is fast and idempotent, and avoids losing pinned wheels.
./.venv/bin/pip install --upgrade pip wheel setuptools
./.venv/bin/pip install --upgrade -r requirements.txt

# Re-stage the systemd unit in case the template changed (paths, hardening
# directives, ...). install.sh substitutes __USER__/__APPDIR__ on first
# install; we do the same here.
if [[ -f systemd/ais-virtual-node.service ]]; then
  TMP="$(mktemp)"
  sed -e "s|__USER__|$USER|g" -e "s|__APPDIR__|$APPDIR|g" \
      systemd/ais-virtual-node.service > "$TMP"
  if ! sudo cmp -s "$TMP" "/etc/systemd/system/${SERVICE}.service"; then
    echo "==> Updating /etc/systemd/system/${SERVICE}.service"
    sudo install -m 0644 "$TMP" "/etc/systemd/system/${SERVICE}.service"
    sudo systemctl daemon-reload
  fi
  rm -f "$TMP"
fi

echo "==> Restarting $SERVICE"
sudo systemctl restart "$SERVICE"

echo "Done. Tail logs with:  journalctl -u $SERVICE -f"
