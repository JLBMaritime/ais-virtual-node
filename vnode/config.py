"""Persistent configuration for the Virtual AIS Node."""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any, Dict

from ._paths import app_root

# Config lives next to the .exe in frozen builds, repo-root in dev. The
# helper picks the right one - see `_paths.py` for the rationale.
CONFIG_PATH = app_root() / "config.json"

DEFAULT_CONFIG: Dict[str, Any] = {
    # Bounding box (English Channel by default - small enough for both APIs)
    "bbox": {"latmin": 49.0, "latmax": 51.5, "lonmin": -5.0, "lonmax": 2.5},
    # Sources
    "sources": {
        # AIS Friends sits behind Cloudflare. The default backend is
        # "flaresolverr" - a tiny Docker container running headless
        # Chromium that solves the JS challenge for us. See README for
        # the one-line `docker run ...` to start it. The other backends
        # ("curl_cffi", "cloudscraper", "requests") are no-Docker
        # fallbacks for networks that haven't been escalated yet.
        "aisfriends": {
            "enabled":          True,
            "token":            "",
            "backend":          "flaresolverr",
            "flaresolverr_url": "http://localhost:8191",
        },
        "aishub":     {"enabled": True,  "username": ""},

        # Kpler Maritime API. Disabled by default - the user adds their
        # base64'd `developers.kpler.com/my-api-keys` credential, picks a
        # flavour, and saves on the Credentials page.
        "kpler": {
            "enabled":   False,
            # `credential` is either `<client_id>:<client_secret>` or its
            # base64 - whatever the dev portal hands the user.
            "credential": "",
            "token_url":  "https://auth.kpler.com/oauth/token",
            "audience":   "https://api.kpler.com",
            "api_url":    "https://api.sml.kpler.com/graphql",
            "flavour":    "graphql",  # "graphql" | "messages"
        },
    },

    # Polling - 60s per source, staggered evenly across however many
    # sources are enabled. The worker computes per-source offsets from
    # `stagger_seconds` at start time, so just bump this to ~20 when
    # running all three sources to get a fresh frame every 20s.
    "poll": {
        "interval_seconds": 60,
        "stagger_seconds":  20,
    },
    # Output forwarders (list of destinations)
    "outputs": [
        {"enabled": True, "protocol": "tcp", "host": "127.0.0.1", "port": 10110},
    ],
    # Encoding behaviour
    "encoding": {
        "force_class_b":       True,    # always emit Type 18 / 24
        "talker_id":           "AIVDM",
        "static_every_sec":    360,    # re-emit Type 24 every N seconds per vessel
        "vessel_ttl_seconds":  60,     # drop a vessel from the tracked set if not
                                        # re-reported within this many seconds
    },
    # Web UI
    "web": {
        "host":     "0.0.0.0",  # bind to all - rely on Tailscale for access control
        "port":     5000,
        "log_size": 500,
    },
    # Runtime (not user-editable from UI)
    "autostart": False,
}

_LOCK = threading.Lock()


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            out[k] = _deep_merge(base[k], v)
        else:
            out[k] = v
    return out


def load() -> Dict[str, Any]:
    if CONFIG_PATH.exists():
        try:
            with CONFIG_PATH.open("r", encoding="utf-8") as f:
                data = json.load(f)
            return _deep_merge(DEFAULT_CONFIG, data)
        except Exception:
            pass
    return json.loads(json.dumps(DEFAULT_CONFIG))  # deep copy


def save(cfg: Dict[str, Any]) -> None:
    with _LOCK:
        CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


def update(updates: Dict[str, Any]) -> Dict[str, Any]:
    """Deep-merge updates into the on-disk config and return the new config."""
    current = load()
    merged = _deep_merge(current, updates)
    save(merged)
    return merged
