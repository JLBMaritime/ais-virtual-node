#!/usr/bin/env bash
# install.sh - one-shot installer for a fresh Raspberry Pi OS Bookworm
#
# What this does, in order:
#   1. apt-installs the OS deps we need (python venv, network-manager, sudo,
#      git, curl, ca-certs, build tools for any wheels that don't ship arm64).
#   2. Sets the hostname to 'ais-virtual' (configurable with --hostname=NAME).
#   3. Creates the service user 'jlbmaritime' if it doesn't already exist.
#      The user lives in 'netdev' (so nmcli sees them as a legitimate caller)
#      and gets a scoped NOPASSWD sudoers entry just for /usr/bin/nmcli.
#   4. Moves / copies this source tree to /home/jlbmaritime/ais-virtual-node
#      and creates the venv at .venv/, installs requirements.txt.
#   5. Seeds config.json from config.example.json if it doesn't exist.
#   6. Installs Tailscale (apt repo) so you can reach the box from anywhere -
#      `sudo tailscale up` after install completes.
#   7. Installs Docker + FlareSolverr container (skip with --without-flaresolverr).
#   8. Renders systemd/ais-virtual-node.service into /etc/systemd/system/,
#      enables + starts it.
#
# Re-runnable. Each step is idempotent.
#
# Usage:
#   sudo ./install.sh                       # full default install
#   sudo ./install.sh --without-flaresolverr  # skip Docker + FlareSolverr
#   sudo ./install.sh --hostname=fishtank   # custom hostname
#
set -euo pipefail

# --- flag parsing -----------------------------------------------------------

WITH_FLARESOLVERR=1
HOSTNAME_NEW="ais-virtual"
SERVICE_USER="jlbmaritime"
WITHOUT_TAILSCALE=0

for arg in "$@"; do
  case "$arg" in
    --without-flaresolverr) WITH_FLARESOLVERR=0 ;;
    --without-tailscale)    WITHOUT_TAILSCALE=1 ;;
    --hostname=*)           HOSTNAME_NEW="${arg#*=}" ;;
    --user=*)               SERVICE_USER="${arg#*=}" ;;
    -h|--help)
      sed -n '2,33p' "$0"; exit 0 ;;
    *)
      echo "Unknown flag: $arg" >&2; exit 2 ;;
  esac
done

if [[ $EUID -ne 0 ]]; then
  echo "Re-run with sudo: sudo $0 $*" >&2
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
APPDIR="/home/${SERVICE_USER}/ais-virtual-node"
SERVICE="ais-virtual-node"

step() { printf "\n\033[1;36m==> %s\033[0m\n" "$*"; }

# --- 1. apt deps ------------------------------------------------------------

step "Installing OS packages (python, network-manager, build tools, curl, git, sudo)"
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y --no-install-recommends \
  python3 python3-venv python3-pip python3-dev \
  build-essential \
  network-manager \
  sudo git curl ca-certificates gnupg lsb-release \
  iproute2 jq

# Make sure NM is up - on a fresh Bookworm with the legacy `dhcpcd` it might
# need a kick. Harmless if already active.
systemctl enable --now NetworkManager.service || true

# --- 2. hostname ------------------------------------------------------------

CURRENT_HOST="$(hostname)"
if [[ "$CURRENT_HOST" != "$HOSTNAME_NEW" ]]; then
  step "Setting hostname: $CURRENT_HOST -> $HOSTNAME_NEW"
  hostnamectl set-hostname "$HOSTNAME_NEW"
  # /etc/hosts: replace the loopback line so `sudo` stops complaining about
  # "unable to resolve host".
  if grep -qE "127\.0\.1\.1\s" /etc/hosts; then
    sed -i -E "s/^127\.0\.1\.1\s.*/127.0.1.1\t${HOSTNAME_NEW}/" /etc/hosts
  else
    echo -e "127.0.1.1\t${HOSTNAME_NEW}" >> /etc/hosts
  fi
else
  step "Hostname already '${HOSTNAME_NEW}', leaving alone"
fi

# --- 3. service user + sudoers ---------------------------------------------

if id "$SERVICE_USER" >/dev/null 2>&1; then
  step "User '$SERVICE_USER' already exists"
else
  step "Creating service user '$SERVICE_USER'"
  # No password - login is via SSH key or local console as 'pi' / sudo only.
  useradd --create-home --shell /bin/bash --user-group "$SERVICE_USER"
fi
# 'netdev' is NetworkManager's "trusted caller" group; doesn't grant nmcli
# write access by itself but it's the conventional place to put a service
# user that talks to NM.
usermod -aG netdev "$SERVICE_USER" || true

step "Installing scoped sudoers fragment for nmcli"
SUDOERS_FILE="/etc/sudoers.d/${SERVICE}-nmcli"
TMP_SUDO="$(mktemp)"
cat > "$TMP_SUDO" <<EOF
# Installed by ${SERVICE} install.sh. Allows the service user to manage
# NetworkManager via the Wi-Fi page in the web UI. Scoped to /usr/bin/nmcli
# only - nothing else.
${SERVICE_USER} ALL=(root) NOPASSWD: /usr/bin/nmcli
EOF
# visudo -c -f validates the syntax. If it fails we DO NOT install it -
# a broken sudoers can lock the user out.
if visudo -c -f "$TMP_SUDO" >/dev/null; then
  install -m 0440 "$TMP_SUDO" "$SUDOERS_FILE"
else
  echo "ERROR: generated sudoers file failed visudo -c, refusing to install:" >&2
  cat "$TMP_SUDO" >&2
  rm -f "$TMP_SUDO"
  exit 1
fi
rm -f "$TMP_SUDO"

# --- 4. source tree + venv --------------------------------------------------

step "Staging source tree into $APPDIR"
mkdir -p "$APPDIR"
# If we're running from somewhere other than APPDIR, mirror the tree across.
# rsync preserves perms and skips the git/venv/cache directories.
if [[ "$SCRIPT_DIR" != "$APPDIR" ]]; then
  if command -v rsync >/dev/null 2>&1; then
    rsync -a --delete \
      --exclude '.venv/' --exclude '__pycache__/' \
      --exclude 'config.json' \
      "$SCRIPT_DIR/" "$APPDIR/"
  else
    # Fallback: cp -a then strip the bits we don't want.
    cp -a "$SCRIPT_DIR/." "$APPDIR/"
    rm -rf "$APPDIR/.venv" "$APPDIR/__pycache__"
  fi
fi
chown -R "${SERVICE_USER}:${SERVICE_USER}" "$APPDIR"

step "Creating Python virtualenv at $APPDIR/.venv"
sudo -u "$SERVICE_USER" python3 -m venv "$APPDIR/.venv"
sudo -u "$SERVICE_USER" "$APPDIR/.venv/bin/pip" install --upgrade pip wheel setuptools
sudo -u "$SERVICE_USER" "$APPDIR/.venv/bin/pip" install -r "$APPDIR/requirements.txt"

# --- 5. seed config.json ----------------------------------------------------

if [[ ! -f "$APPDIR/config.json" ]]; then
  step "Seeding $APPDIR/config.json from config.example.json"
  cp "$APPDIR/config.example.json" "$APPDIR/config.json"
  chown "${SERVICE_USER}:${SERVICE_USER}" "$APPDIR/config.json"
  chmod 0640 "$APPDIR/config.json"
else
  step "config.json already exists, leaving alone"
fi

# --- 6. Tailscale -----------------------------------------------------------

if [[ $WITHOUT_TAILSCALE -eq 0 ]]; then
  if command -v tailscale >/dev/null 2>&1; then
    step "Tailscale already installed: $(tailscale version | head -n1)"
  else
    step "Installing Tailscale (official apt repo)"
    # One-liner install script from the Tailscale team. Adds the apt repo,
    # imports the signing key, installs the daemon, enables tailscaled.
    curl -fsSL https://tailscale.com/install.sh | sh
  fi
  echo "Tailscale is installed but NOT logged in. Run:"
  echo "    sudo tailscale up --ssh"
  echo "to enrol this device and (optionally) expose SSH over Tailnet."
fi

# --- 7. Docker + FlareSolverr ----------------------------------------------

if [[ $WITH_FLARESOLVERR -eq 1 ]]; then
  if ! command -v docker >/dev/null 2>&1; then
    step "Installing Docker engine (get.docker.com convenience script)"
    curl -fsSL https://get.docker.com | sh
    # Let the service user run docker commands without sudo. The Wi-Fi page
    # doesn't need this; the start-flaresolverr.sh script does.
    usermod -aG docker "$SERVICE_USER" || true
    systemctl enable --now docker
  else
    step "Docker already installed: $(docker --version)"
  fi

  step "Starting FlareSolverr container"
  # Run as the service user so the container is owned by the right user.
  sudo -u "$SERVICE_USER" -- bash -lc "cd '$APPDIR' && ./scripts/start-flaresolverr.sh" || {
    echo "WARNING: FlareSolverr failed to start. The service will still run -" >&2
    echo "         AIS Friends will be unavailable until you re-run:" >&2
    echo "             cd $APPDIR && ./scripts/start-flaresolverr.sh" >&2
  }
else
  step "Skipping Docker + FlareSolverr (--without-flaresolverr)"
  echo "       AIS Friends source will not work until you set the credentials"
  echo "       backend to something other than 'flaresolverr', or install it later."
fi

# --- 8. systemd unit --------------------------------------------------------

step "Installing systemd unit /etc/systemd/system/${SERVICE}.service"
TMP_UNIT="$(mktemp)"
sed -e "s|__USER__|${SERVICE_USER}|g" -e "s|__APPDIR__|${APPDIR}|g" \
    "$APPDIR/systemd/${SERVICE}.service" > "$TMP_UNIT"
install -m 0644 "$TMP_UNIT" "/etc/systemd/system/${SERVICE}.service"
rm -f "$TMP_UNIT"

systemctl daemon-reload
systemctl enable "${SERVICE}.service"
systemctl restart "${SERVICE}.service"

# --- summary ----------------------------------------------------------------

IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
cat <<EOF

================================================================
 Install complete.
================================================================

 Web UI:      http://${HOSTNAME_NEW}.local:5000
              http://${IP:-<this-pi>}:5000

 Service:     systemctl status ${SERVICE}
 Logs:        journalctl -u ${SERVICE} -f
 Wi-Fi page:  http://${HOSTNAME_NEW}.local:5000/wifi

 Next steps:
   1. Open the Credentials page and paste your AIS Friends token /
      AISHub username / Kpler key, save, click Test.
   2. Open the Configuration page, drag the bounding-box rectangle
      over the area you care about, set the output TCP/UDP targets,
      save, then click Start.
EOF

if [[ $WITHOUT_TAILSCALE -eq 0 ]]; then
  cat <<EOF
   3. Bring Tailscale up:
          sudo tailscale up --ssh
      Then you can reach the Pi by its Tailnet name from anywhere.
EOF
fi

cat <<EOF

 Warning: If you change Wi-Fi credentials over Wi-Fi and get them
 wrong, you will lock yourself out of the LAN. Always do that from
 wired Ethernet or over Tailscale.
================================================================
EOF
