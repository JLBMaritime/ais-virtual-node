#!/usr/bin/env bash
# scripts/uninstall.sh
#
# Reverse install.sh:
#   - stop & disable the systemd unit, remove /etc/systemd/system/*.service
#   - remove the scoped sudoers fragment for nmcli
#   - optionally remove the FlareSolverr container + image
#   - optionally remove the source tree and the jlbmaritime user
#
# Defaults to a *conservative* removal: it keeps the user account, the source
# tree (so config.json survives) and the FlareSolverr image. Pass --purge to
# tear everything down.
#
# Usage:
#   sudo ./scripts/uninstall.sh             # service + sudoers only
#   sudo ./scripts/uninstall.sh --purge     # service + sudoers + flaresolverr + source tree + user
set -euo pipefail

PURGE=0
for arg in "$@"; do
  case "$arg" in
    --purge) PURGE=1 ;;
    -h|--help)
      sed -n '2,16p' "$0"; exit 0 ;;
    *) echo "unknown flag: $arg" >&2; exit 2 ;;
  esac
done

if [[ $EUID -ne 0 ]]; then
  echo "Re-run with sudo." >&2
  exit 1
fi

SERVICE="ais-virtual-node"
SUDOERS_FILE="/etc/sudoers.d/ais-virtual-node-nmcli"
SERVICE_USER="jlbmaritime"
APPDIR="/home/${SERVICE_USER}/ais-virtual-node"

echo "==> Stopping & disabling ${SERVICE}.service"
systemctl stop    "${SERVICE}.service" 2>/dev/null || true
systemctl disable "${SERVICE}.service" 2>/dev/null || true
rm -f "/etc/systemd/system/${SERVICE}.service"
systemctl daemon-reload

echo "==> Removing scoped sudoers fragment"
rm -f "$SUDOERS_FILE"

if [[ $PURGE -eq 1 ]]; then
  echo "==> --purge: removing FlareSolverr container & image"
  if command -v docker >/dev/null 2>&1; then
    docker rm -f flaresolverr 2>/dev/null || true
    docker rmi ghcr.io/flaresolverr/flaresolverr:latest 2>/dev/null || true
  fi

  echo "==> --purge: removing app directory ${APPDIR}"
  rm -rf "$APPDIR"

  if id "$SERVICE_USER" >/dev/null 2>&1; then
    echo "==> --purge: removing user '$SERVICE_USER' and its home"
    # --remove drops the home dir too. Leave the mail spool alone.
    userdel --remove "$SERVICE_USER" 2>/dev/null || true
  fi

  echo "==> --purge complete. Docker engine itself was left installed -"
  echo "    remove it with:  sudo apt-get purge -y docker-ce docker-ce-cli containerd.io"
else
  echo "Done. The source tree at $APPDIR and any FlareSolverr container"
  echo "were left untouched. Pass --purge to remove them too."
fi
