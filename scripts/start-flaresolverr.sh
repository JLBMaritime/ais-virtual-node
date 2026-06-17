#!/usr/bin/env bash
# scripts/start-flaresolverr.sh
#
# Start (or re-create) the FlareSolverr container that the AIS Friends source
# uses to clear Cloudflare's interstitial. install.sh runs this at the end of
# a default install; you can re-run it any time to refresh the image or fix
# a wedged container.
#
# Why FlareSolverr?
#   aisfriends.com sits behind Cloudflare's bot-protection challenge. Plain
#   `requests` / `curl_cffi` get a 403 "Just a moment..." page; FlareSolverr
#   spins up headless Chromium, solves the JS challenge, and returns the
#   resulting cookies + body. The Pi only talks to the local container on
#   :8191, never directly to Cloudflare.
set -euo pipefail

IMAGE="ghcr.io/flaresolverr/flaresolverr:latest"
NAME="flaresolverr"
PORT="${FLARESOLVERR_PORT:-8191}"
TZ="${TZ:-Etc/UTC}"

if ! command -v docker >/dev/null 2>&1; then
  echo "docker is not installed. Run install.sh (default install adds Docker)," >&2
  echo "or install.sh --without-flaresolverr if you don't need AIS Friends." >&2
  exit 1
fi

# Pull (or refresh) the image. Multi-arch manifest covers arm64 (Pi 4/5) and amd64.
echo "==> docker pull $IMAGE"
docker pull "$IMAGE"

# If a container with this name already exists, recreate it so we pick up the
# new image. `docker rm -f` is a no-op when nothing matches.
if docker ps -a --format '{{.Names}}' | grep -qx "$NAME"; then
  echo "==> Removing existing container '$NAME' so we can recreate it"
  docker rm -f "$NAME" >/dev/null
fi

echo "==> Starting '$NAME' on 127.0.0.1:${PORT}"
# 127.0.0.1 only - no reason to expose Chromium to the LAN. The Python code
# talks to http://localhost:8191 from the same box.
docker run -d \
  --name "$NAME" \
  --restart unless-stopped \
  -p "127.0.0.1:${PORT}:8191" \
  -e "LOG_LEVEL=info" \
  -e "TZ=${TZ}" \
  "$IMAGE" >/dev/null

# Tiny health-check so the user sees something actionable instead of a
# silent success when Chromium fails to launch.
sleep 2
if curl -fsS "http://127.0.0.1:${PORT}/" >/dev/null 2>&1; then
  echo "FlareSolverr is up on http://127.0.0.1:${PORT}"
else
  echo "FlareSolverr container started but isn't answering yet." >&2
  echo "  - tail the container log:    docker logs -f $NAME" >&2
  echo "  - common cause on Pi:        out of RAM (Chromium needs ~400 MB)" >&2
fi
