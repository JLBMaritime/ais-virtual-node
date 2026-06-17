"""Wi-Fi manager - thin wrapper around NetworkManager's `nmcli`.

Target environment is Raspberry Pi OS Bookworm / modern Debian / Ubuntu where
NetworkManager is the default network stack. We never call NetworkManager's
D-Bus API directly - `nmcli` is the supported, stable interface and is what
NM upstream guarantees.

Privilege model
---------------
NetworkManager refuses unauthenticated callers for anything that mutates the
connection list (connect, forget, hotspot). The service runs as the
non-privileged `jlbmaritime` user, so `install.sh` drops a tightly scoped
sudoers fragment:

    jlbmaritime ALL=(root) NOPASSWD: /usr/bin/nmcli

That's the *only* privilege escalation granted - `nmcli`, nothing else.

This module always invokes `sudo -n nmcli ...` so it works identically when
the process is the `jlbmaritime` service, the `pi` user under `sudo`, or root.

Parsing
-------
We use `nmcli -t -f <fields> ...` which is a colon-separated, terse output
designed for scripts. Fields containing literal `:` or `\` are backslash-
escaped by nmcli (e.g. an SSID `My:House` becomes `My\:House`). `_split_row`
honours that escaping.

Public API
----------
Every public function returns a plain dict / list of dicts ready for JSON
serialisation, or raises `WifiError` with a single-line, human-readable
message that the UI can render verbatim.
"""
from __future__ import annotations

import logging
import shutil
import subprocess
from typing import Any, Dict, List, Optional

log = logging.getLogger("vnode.wifi")

NMCLI = "/usr/bin/nmcli"
SUDO  = "/usr/bin/sudo"
# 15 seconds is comfortably longer than any nmcli call except `device wifi
# rescan` (which blocks for ~5s) and `connection up` on a new SSID (~10s).
_DEFAULT_TIMEOUT = 30


class WifiError(RuntimeError):
    """Raised on any nmcli/Wi-Fi failure. The message is shown to the UI as-is."""


# ---------------------------------------------------------------------------
# Low-level helpers
# ---------------------------------------------------------------------------

def _have_nmcli() -> bool:
    """True if nmcli is on PATH. The Wi-Fi page disables itself if False."""
    return shutil.which(NMCLI) is not None or shutil.which("nmcli") is not None


def _run(args: List[str], *, timeout: int = _DEFAULT_TIMEOUT,
         input_text: Optional[str] = None) -> str:
    """Run `sudo -n nmcli ...`, return stdout, raise WifiError on failure.

    `sudo -n` means non-interactive: if the sudoers entry isn't in place we
    fail fast with a clear message rather than hanging on a password prompt.
    """
    cmd = [SUDO, "-n", NMCLI, *args]
    log.debug("wifi: %s", " ".join(cmd))
    try:
        cp = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            input=input_text,
            check=False,
        )
    except FileNotFoundError as exc:
        raise WifiError(
            "sudo or nmcli is not installed. Run `sudo apt-get install -y "
            "network-manager sudo`, then re-install the app."
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise WifiError(f"nmcli timed out after {timeout}s: {' '.join(args)}") from exc

    if cp.returncode != 0:
        err = (cp.stderr or cp.stdout or "").strip().splitlines()
        msg = err[-1] if err else f"nmcli exit {cp.returncode}"
        # Detect the specific "no NOPASSWD entry" path and explain how to fix it.
        if "a password is required" in msg.lower() or "sudo: a password" in msg.lower():
            msg = ("nmcli refused: this user has no NOPASSWD sudoers entry "
                   "for /usr/bin/nmcli. Re-run install.sh, or add: "
                   "`<user> ALL=(root) NOPASSWD: /usr/bin/nmcli` to "
                   "/etc/sudoers.d/ais-virtual-node-nmcli.")
        raise WifiError(msg)
    return cp.stdout


def _split_row(line: str) -> List[str]:
    """Split a single `nmcli -t` row, honouring backslash-escaped colons.

    `nmcli -t` escapes each literal `:` inside a field with a backslash, so
    `MyHouse\\:5G` is a single field, not two. Same for literal `\\`.
    """
    out: List[str] = []
    buf: List[str] = []
    i = 0
    n = len(line)
    while i < n:
        c = line[i]
        if c == "\\" and i + 1 < n:
            buf.append(line[i + 1])
            i += 2
            continue
        if c == ":":
            out.append("".join(buf))
            buf.clear()
            i += 1
            continue
        buf.append(c)
        i += 1
    out.append("".join(buf))
    return out


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def available() -> bool:
    """True iff Wi-Fi management is usable on this host."""
    return _have_nmcli()


def status() -> Dict[str, Any]:
    """Snapshot of every active link plus the current default route.

    Returns:
        {
          "interfaces": [
            {"device": "eth0",  "type": "ethernet",
             "state": "connected", "connection": "Wired",
             "ipv4": "192.168.1.42",
             "is_default": true,         # current default route
             "metric": 100,
             # Wi-Fi-only extras:
             "ssid": null, "signal": null, "security": null,
            },
            ...
          ],
          "hostname": "ais-virtual",
          "nmcli_available": true
        }
    """
    if not _have_nmcli():
        return {"interfaces": [], "hostname": _hostname(), "nmcli_available": False}

    # 1) device list with current connection name
    dev_rows = _run([
        "-t", "-f", "DEVICE,TYPE,STATE,CONNECTION", "device", "status",
    ]).strip().splitlines()
    devices: Dict[str, Dict[str, Any]] = {}
    for row in dev_rows:
        if not row:
            continue
        cols = _split_row(row)
        if len(cols) < 4:
            continue
        dev, dtype, state, conn = cols[:4]
        if dtype in ("loopback", "bridge", "tun"):
            continue
        devices[dev] = {
            "device": dev,
            "type": dtype,
            "state": state,
            "connection": conn or None,
            "ipv4": None,
            "is_default": False,
            "metric": None,
            "ssid": None,
            "signal": None,
            "security": None,
        }

    # 2) IPv4 address per device
    for dev, info in devices.items():
        try:
            out = _run(["-t", "-f", "IP4.ADDRESS", "device", "show", dev]).strip()
        except WifiError:
            continue
        # IP4.ADDRESS[1]:192.168.1.42/24
        for line in out.splitlines():
            cols = _split_row(line)
            if len(cols) >= 2 and cols[1]:
                info["ipv4"] = cols[1].split("/", 1)[0]
                break

    # 3) Current Wi-Fi SSID + signal per wireless device
    try:
        wifi_rows = _run([
            "-t", "-f", "DEVICE,ACTIVE,SSID,SIGNAL,SECURITY",
            "device", "wifi", "list",
        ]).strip().splitlines()
    except WifiError:
        wifi_rows = []
    for row in wifi_rows:
        cols = _split_row(row)
        if len(cols) < 5:
            continue
        dev, active, ssid, signal, sec = cols[:5]
        if active != "yes" or dev not in devices:
            continue
        devices[dev]["ssid"] = ssid or None
        try:
            devices[dev]["signal"] = int(signal)
        except (TypeError, ValueError):
            devices[dev]["signal"] = None
        devices[dev]["security"] = sec or "Open"

    # 4) Default route -> which device holds it, and at what metric
    try:
        import shutil as _sh
        ip_bin = _sh.which("ip") or "/usr/sbin/ip"
        cp = subprocess.run(
            [ip_bin, "-4", "route", "show", "default"],
            capture_output=True, text=True, timeout=5, check=False,
        )
        for line in (cp.stdout or "").splitlines():
            # default via 192.168.1.1 dev eth0 proto dhcp src ... metric 100
            parts = line.split()
            try:
                dev_idx = parts.index("dev")
                dev = parts[dev_idx + 1]
            except (ValueError, IndexError):
                continue
            metric = None
            if "metric" in parts:
                try:
                    metric = int(parts[parts.index("metric") + 1])
                except (ValueError, IndexError):
                    metric = None
            if dev in devices:
                # Lowest metric wins. We sort by metric and mark only the best.
                cur = devices[dev].get("_route_metric")
                if cur is None or (metric is not None and metric < cur):
                    devices[dev]["_route_metric"] = metric
        # Now find the minimum-metric device across the lot.
        best_dev, best_metric = None, None
        for dev, info in devices.items():
            m = info.pop("_route_metric", None)
            if m is None:
                continue
            if best_metric is None or m < best_metric:
                best_dev, best_metric = dev, m
        if best_dev is not None:
            devices[best_dev]["is_default"] = True
            devices[best_dev]["metric"] = best_metric
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    return {
        "interfaces": list(devices.values()),
        "hostname": _hostname(),
        "nmcli_available": True,
    }


def scan(rescan: bool = True) -> List[Dict[str, Any]]:
    """List nearby Wi-Fi networks, strongest first.

    Args:
        rescan: When True, ask NM to re-scan the air (~5 s blocking).
                Set False for a snappy refresh of the last cached scan.

    Returns each entry as ``{"ssid", "signal", "security", "in_use"}``.
    Hidden / empty-SSID networks are skipped.
    """
    if rescan:
        try:
            _run(["device", "wifi", "rescan"], timeout=20)
        except WifiError:
            # rescan often fails with "Scanning not allowed at this moment"
            # when NM just scanned. The cached list is still useful.
            pass
    out = _run([
        "-t", "-f", "IN-USE,SSID,SIGNAL,SECURITY",
        "device", "wifi", "list",
    ]).strip().splitlines()
    seen: Dict[str, Dict[str, Any]] = {}
    for row in out:
        cols = _split_row(row)
        if len(cols) < 4:
            continue
        in_use, ssid, signal, sec = cols[:4]
        if not ssid:
            continue  # hidden
        try:
            sig = int(signal)
        except (TypeError, ValueError):
            sig = 0
        # nmcli emits one row per BSSID; collapse to one per SSID, keeping the
        # strongest signal. "in_use" sticks if any of the rows say so.
        cur = seen.get(ssid)
        if cur is None or sig > cur["signal"]:
            seen[ssid] = {
                "ssid": ssid,
                "signal": sig,
                "security": sec or "Open",
                "in_use": (in_use == "*") or (cur is not None and cur["in_use"]),
            }
        elif in_use == "*":
            cur["in_use"] = True
    return sorted(seen.values(), key=lambda r: -r["signal"])


def saved() -> List[Dict[str, Any]]:
    """List saved Wi-Fi connection profiles.

    Returns ``[{"name", "ssid", "autoconnect"}]``. Non-Wi-Fi connections
    (ethernet, tailscale, loopback) are filtered out.
    """
    out = _run([
        "-t", "-f", "NAME,TYPE,AUTOCONNECT", "connection", "show",
    ]).strip().splitlines()
    profiles: List[Dict[str, Any]] = []
    for row in out:
        cols = _split_row(row)
        if len(cols) < 3:
            continue
        name, ctype, autoconn = cols[:3]
        if ctype not in ("802-11-wireless", "wifi"):
            continue
        # The SSID is usually equal to the name unless the user renamed the
        # profile; fetch it explicitly so the UI is accurate.
        try:
            ssid_out = _run([
                "-t", "-f", "802-11-wireless.ssid",
                "connection", "show", name,
            ]).strip()
            # 802-11-wireless.ssid:MyHomeWiFi
            ssid = _split_row(ssid_out)[1] if ssid_out else name
        except WifiError:
            ssid = name
        profiles.append({
            "name": name,
            "ssid": ssid,
            "autoconnect": autoconn.lower() == "yes",
        })
    return profiles


def connect(ssid: str, password: Optional[str] = None,
            hidden: bool = False) -> Dict[str, Any]:
    """Join `ssid`. Re-uses a saved profile of the same SSID if present.

    Raises WifiError on any failure - the message string is the exact tail of
    nmcli's stderr, so the UI surfaces useful errors verbatim
    ("Secrets were required but not provided", "No network with SSID 'x'", ...).

    Returns ``{"ok": true, "ssid": ssid}`` on success.
    """
    ssid = (ssid or "").strip()
    if not ssid:
        raise WifiError("SSID is required")
    args = ["device", "wifi", "connect", ssid]
    if password:
        args += ["password", password]
    if hidden:
        args += ["hidden", "yes"]
    # The connect can legitimately take a while: DHCP + key exchange on a
    # weak signal can push 20+ seconds. Cap at 45.
    _run(args, timeout=45)
    return {"ok": True, "ssid": ssid}


def forget(ssid_or_name: str) -> Dict[str, Any]:
    """Delete the saved Wi-Fi profile matching the given name or SSID."""
    target = (ssid_or_name or "").strip()
    if not target:
        raise WifiError("Profile name is required")
    # Try by connection name first; on miss, look up the matching saved
    # profile and delete by name.
    try:
        _run(["connection", "delete", target])
        return {"ok": True, "name": target}
    except WifiError:
        for p in saved():
            if p["ssid"] == target or p["name"] == target:
                _run(["connection", "delete", p["name"]])
                return {"ok": True, "name": p["name"]}
        raise WifiError(f"No saved Wi-Fi profile matching '{target}'.")


# ---------------------------------------------------------------------------
# Misc
# ---------------------------------------------------------------------------

def _hostname() -> str:
    try:
        import socket
        return socket.gethostname()
    except Exception:
        return ""
