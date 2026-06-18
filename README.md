# AIS Virtual Node

A headless Raspberry Pi appliance that pulls AIS targets from one or more
upstream APIs (**AIS Friends**, **AISHub**, **Kpler**), encodes them as
**ITU-1371 AIVDM / AIVDO** sentences, and forwards the stream over TCP/UDP to
plotters, OpenCPN, SignalK or any other NMEA-0183 consumer on your LAN.

The whole thing is configured through a small Flask web UI on port `5000`:
Dashboard, Configuration, Credentials, Logs and Wi-Fi.

| Page          | What you do there                                                            |
| ------------- | ---------------------------------------------------------------------------- |
| Dashboard     | Live map of vessels currently in the bounding box, sentence counter, log tail |
| Configuration | Drag the bounding box, pick poll cadence, choose sources, list TCP/UDP outputs |
| Credentials   | AIS Friends token, one or more AISHub usernames (see "Multiple AISHub keys" below), Kpler client_id\:secret, per-source / per-key "Test" |
| Logs          | Full NMEA console with source colouring                                       |
| Wi-Fi         | List nearby SSIDs, join/forget networks, see which interface holds the default route |

---

## Hardware

- Raspberry Pi 4 or 5 (2 GB+ RAM if you want FlareSolverr on-box)
- Raspberry Pi OS Bookworm (64-bit recommended)
- Ethernet **and** Wi-Fi both wired up — see "Networking & failover" below

The whole stack also runs on a Pi Zero 2 W if you skip FlareSolverr
(`--without-flaresolverr`) and use only AISHub / Kpler.

---

## Install

Flash Raspberry Pi OS Bookworm, enable SSH, log in as `pi`, then:

```bash
# 1. Bring the fresh image fully up to date (kernel + firmware + packages).
#    On a brand-new Pi this can take a few minutes.
sudo apt update
sudo apt full-upgrade -y

# 2. Reboot if the upgrade pulled in a new kernel / firmware. It's safe to
#    always run this - it's a no-op if nothing changed, and it avoids
#    surprises when install.sh later adds NetworkManager, Docker, Tailscale.
sudo reboot
```

After the Pi comes back up, SSH in again and:

```bash
# 3. Git ships with Pi OS Desktop but NOT with Pi OS Lite, so install it
#    explicitly. The line is a no-op if git is already present.
sudo apt install -y git

# 4. Clone the repo into your home directory.
git clone https://github.com/JLBMaritime/ais-virtual-node.git
cd ais-virtual-node

# 5. Make the installer + maintenance scripts executable. The +x bit can
#    be lost on some file transfers (USB stick, Windows-formatted SD,
#    SFTP without preserve-permissions), so set it explicitly.
chmod +x install.sh scripts/*.sh

# 6. Run the installer.
sudo ./install.sh
```

`install.sh` is idempotent — re-run it any time. By default it will:

1. apt-install Python 3, `network-manager`, build tools, `sudo`, `curl`, `jq`
2. Set the hostname to **ais-virtual** so the UI is reachable at
   `http://ais-virtual.local:5000` from any other Bonjour/mDNS-aware device
3. Create user **jlbmaritime** (no password, login is via the `pi` account)
   and add a *scoped* `NOPASSWD` sudoers entry for **only** `/usr/bin/nmcli`
   — that's the entire privilege escalation the daemon gets
4. Mirror the source tree into `/home/jlbmaritime/ais-virtual-node`,
   create a `.venv` and install `requirements.txt`
5. Seed `config.json` from `config.example.json` (skipped if it exists)
6. Install **Tailscale** so you can reach the box from anywhere
   (skip with `--without-tailscale`)
7. Install **Docker** + the **FlareSolverr** container that AIS Friends needs
   to bypass Cloudflare (skip with `--without-flaresolverr`)
8. Install and `enable --now` the `ais-virtual-node.service` systemd unit

### Flags

```text
sudo ./install.sh --without-flaresolverr   # no Docker, no FlareSolverr
sudo ./install.sh --without-tailscale      # don't install Tailscale
sudo ./install.sh --hostname=fishtank      # custom hostname
sudo ./install.sh --user=skipper           # custom service user
```

### Tailscale

After install completes:

```bash
sudo tailscale up --ssh
```

Follow the URL it prints, sign in. From then on every device on your Tailnet
can reach the box at `http://ais-virtual:5000` regardless of LAN.

### First boot

Open `http://ais-virtual.local:5000` and:

1. **Credentials** → paste your tokens, click **Save**, click **Test** on
   each source. Errors are reported verbatim with actionable hints.
2. **Configuration** → drag the rectangle over the area you want AIS for,
   add at least one TCP or UDP output, click **Save** then **Start**.
3. **Dashboard** → vessels appear within a poll cycle (default 60 s).

### Multiple AISHub keys

AISHub rate-limits to **1 request per minute per username**, not per IP — so
the API key page at `aishub.net` lets you mint more than one and combining
them is the legitimate way to get a faster effective frame rate.

The **Credentials → AISHub** card therefore accepts a list of usernames:

- Click **+ Add another AISHub key** to add a row, paste each username, then
  **Save credentials**.
- Each row has its own **Test** button so you can verify keys individually
  before they go live.
- The badge in the card header shows the live effective frame rate, e.g.
  *"every 20s · 3 keys"*. The maths: each key still polls at the full
  `poll.interval_seconds` cadence (60 s by default, respecting AISHub's
  per-key limit), but the N keys are interleaved evenly across the
  interval, so the AISHub data lands every `interval ÷ N` seconds in
  aggregate.
- Adding or removing a key is picked up live by the worker thread; no
  service restart needed.
- **Schema migration is automatic.** Old `config.json` files that store a
  single `username` string are lifted into the new `usernames` list on the
  first read after `scripts/update.sh`. The migrator runs in
  `vnode/config.py` and is idempotent, so you can leave the on-disk file
  alone if you want.

---

## Networking & failover

The Pi keeps **both** interfaces hot at all times. NetworkManager assigns
each its own default-route metric:

| Interface  | Default metric | What happens                                                  |
| ---------- | -------------- | ------------------------------------------------------------- |
| `eth0`     | 100            | Always preempts Wi-Fi when a cable is plugged in              |
| `wlan0`    | 600            | Takes over within 5–10 s when the cable is unplugged          |
| `tailscale0` | n/a          | Independent overlay — works whichever physical link is up     |

So the failover story is:

- Cable in → `eth0` carries traffic. Wi-Fi is associated but idle.
- Cable out → Wi-Fi takes the default route automatically.
- Tailscale reaches the box over whichever interface currently has internet.

The **Wi-Fi page** shows which interface holds the default route at any moment.

### ⚠️ Wi-Fi credential warning

If you change Wi-Fi creds **while connected over Wi-Fi** and you get the new
SSID/password wrong, you will lock yourself out of the LAN. Always do
Wi-Fi changes from:

- **Wired Ethernet** (safest), or
- **Tailscale** (works even if Wi-Fi dies — Tailscale rides whichever link is up)

Recovery from a bad Wi-Fi change without either of those means plugging a
keyboard + monitor into the Pi and running:

```bash
sudo nmcli connection delete "<bad-ssid>"
sudo nmcli device wifi connect "<good-ssid>" password "<good-password>"
```

### Wi-Fi page shows *"sudo: no new privileges flag is set"*

The Wi-Fi page calls `sudo nmcli` under the hood. If your systemd unit has
`NoNewPrivileges=yes` — either set explicitly **or** implied by a hardening
directive like `ProtectKernelTunables=yes`, `ProtectKernelModules=yes`,
`ProtectClock=yes`, `SystemCallFilter=...`, `RestrictSUIDSGID=yes` or
`LockPersonality=yes` — sudo cannot escalate and every Wi-Fi API call
fails with this error. To fix:

```bash
# Edit systemd/ais-virtual-node.service and remove any of the forbidden
# directives listed above (the shipped unit has them commented).
cd ~/ais-virtual-node
./scripts/update.sh        # re-stages the unit + daemon-reload + restart
```

The shipped unit on this branch already avoids them; you only hit this if
you've customised the unit locally or if a future systemd version starts
implying NNP for one of the remaining directives.

---

## Operations

### Tail the live log

```bash
journalctl -u ais-virtual-node -f
```

### Service control

```bash
sudo systemctl status   ais-virtual-node
sudo systemctl restart  ais-virtual-node
sudo systemctl stop     ais-virtual-node
sudo systemctl disable  ais-virtual-node     # don't start at boot
```

### FlareSolverr control

```bash
docker logs -f flaresolverr           # live log of the Cloudflare bypass
docker restart flaresolverr           # cycle the container
~/ais-virtual-node/scripts/start-flaresolverr.sh   # pull latest + recreate
```

### Update to the latest release

```bash
cd ~/ais-virtual-node
./scripts/update.sh
```

`update.sh` does `git pull --ff-only`, refreshes the venv, re-stages the
systemd unit if it changed, and restarts the service. Your `config.json`
is never touched.

### Uninstall

```bash
sudo ~/ais-virtual-node/scripts/uninstall.sh           # service + sudoers only
sudo ~/ais-virtual-node/scripts/uninstall.sh --purge   # also: FlareSolverr, source tree, user
```

---

## Layout

```
ais-virtual-node/
├── install.sh                       one-shot installer
├── virtual_ais_node.py              entry point launched by systemd
├── requirements.txt
├── config.example.json              checked into git
├── config.json                      created on first boot (NOT in git)
├── vnode/
│   ├── config.py                    JSON config load/merge
│   ├── encoder.py                   ITU-1371 AIVDM/AIVDO encoder
│   ├── forwarder.py                 TCP/UDP fan-out
│   ├── sources.py                   AIS Friends / AISHub / Kpler clients
│   ├── web.py                       Flask app + REST API
│   ├── wifi.py                      nmcli wrapper for the Wi-Fi page
│   └── worker.py                    polling + dedup + emission loop
├── templates/                       Jinja templates (dashboard, config, ...)
├── static/                          CSS, JS, logo
├── systemd/
│   └── ais-virtual-node.service     template (paths substituted by install.sh)
└── scripts/
    ├── start-flaresolverr.sh        idempotent FlareSolverr container start
    ├── update.sh                    pull + venv refresh + service bounce
    └── uninstall.sh                 reverse install.sh
```

---

## Security notes

- The Flask app has **no authentication**. Bind it only to your LAN /
  Tailnet (`web.host` in `config.json` defaults to `0.0.0.0:5000`). If
  you expose it to the public internet, put a reverse proxy with auth
  in front.
- The service user `jlbmaritime` has a `NOPASSWD` sudoers entry for
  exactly one binary: `/usr/bin/nmcli`. Nothing else escalates.
- `config.json` is `chmod 0640` and owned by `jlbmaritime` — your AIS
  Friends token and Kpler client secret don't leak to other users.

---

## License

MIT — see [LICENSE](LICENSE).
