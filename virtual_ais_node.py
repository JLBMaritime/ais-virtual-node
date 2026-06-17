"""Virtual AIS Node - entrypoint.

Polls AIS Friends, AISHub and Kpler for vessels inside a bounding box,
converts each position to NMEA AIS Class B sentences (Type 18 + Type 24A/B),
forwards them to configured TCP/UDP endpoints, and exposes a small Flask web
UI on port 5000.

Two ways to run:

    # 1) Source checkout
    pip install -r requirements.txt
    python virtual_ais_node.py

    # 2) Frozen Windows build (single-file PyInstaller exe)
    VirtualAISNode.exe        # double-click; browser opens automatically

Then open http://localhost:5000/ (or your Tailscale IP:5000 from another device).
"""
from __future__ import annotations

import argparse
import logging
import pathlib
import shutil
import sys
import threading
import time
import webbrowser

# --- Guard 1: nuke any stale __pycache__ so we never re-run an old build ----
# This is a dev-checkout safety net; PyInstaller bundles don't have __pycache__
# dirs (everything's pre-compiled into the bootloader archive), so skip it when
# frozen to avoid a misleading PermissionError on the read-only _MEIPASS dir.
if not getattr(sys, "frozen", False):
    _HERE = pathlib.Path(__file__).resolve().parent
    for _cache in (_HERE / "vnode" / "__pycache__",):
        if _cache.exists():
            shutil.rmtree(_cache, ignore_errors=True)
# ----------------------------------------------------------------------------

from vnode import config as cfgmod
from vnode import encoder as enc_mod
from vnode import sources as src_mod
from vnode import web
from vnode.worker import WORKER


def _open_browser_after(url: str, delay_seconds: float = 1.5) -> None:
    """Fire-and-forget timer that pops the user's default browser open.

    We delay a bit so the Flask server has had time to bind the listening
    socket; otherwise the browser races the server and shows a connection
    error before retrying.
    """
    def _open():
        try:
            webbrowser.open(url, new=2)
        except Exception:
            # If no browser is registered (headless box, etc.) just swallow -
            # the URL is in the console log either way.
            pass
    threading.Timer(delay_seconds, _open).start()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="virtual_ais_node",
                                     description="Virtual AIS Node")
    parser.add_argument("--no-browser", action="store_true",
                        help="Don't auto-open the browser at startup.")
    parser.add_argument("--host", default=None,
                        help="Override the web UI bind address (default from config.json).")
    parser.add_argument("--port", type=int, default=None,
                        help="Override the web UI port (default from config.json).")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    log = logging.getLogger("virtual_ais_node")

    log.info(
        "AIS Friends backend: %s   (curl_cffi=%s, cloudscraper=%s)",
        src_mod.AF_BACKEND, src_mod.CURL_CFFI_AVAILABLE, src_mod.CLOUDSCRAPER_AVAILABLE,
    )

    # Guard 2: encoder self-test - prove the code on disk is the code we'll run.
    try:
        probe = enc_mod.encode_position_18({
            "mmsi": 368175660, "lat": 41.07, "lon": -70.56,
            "sog": 1.2, "cog": 81.0, "heading": 80,
        })
        if not probe:
            log.error("Encoder probe FAILED: empty output")
        elif not probe[0].startswith("!AIVDM"):
            log.error("Encoder probe FAILED: emitted %r (expected !AIVDM...)", probe[0][:8])
        else:
            log.info("Encoder probe OK: %s", probe[0])
    except Exception as exc:
        log.exception("Encoder probe raised: %s", exc)

    if src_mod.AF_BACKEND == "requests":
        log.warning(
            "Neither curl_cffi nor cloudscraper is installed - AIS Friends requests "
            "will almost certainly be blocked by Cloudflare with HTTP 403. "
            "Run: pip install -r requirements.txt   (in this same environment) and restart."
        )

    cfg = cfgmod.load()
    cfgmod.save(cfg)  # write defaults out on first run
    log.info("Config file: %s", cfgmod.CONFIG_PATH)

    host = args.host or cfg["web"].get("host", "0.0.0.0")
    port = int(args.port or cfg["web"].get("port", 5000))

    if cfg.get("autostart"):
        WORKER.start()
        log.info("Worker autostarted")

    # Auto-open the browser when running the .exe directly. We point at
    # 127.0.0.1 even if the server bound 0.0.0.0, since that's what the
    # local user actually wants.
    if not args.no_browser:
        url = f"http://127.0.0.1:{port}/"
        log.info("Opening %s in your browser...", url)
        _open_browser_after(url, delay_seconds=1.5)

    log.info("Starting web UI on http://%s:%d/", host, port)
    try:
        web.run(host=host, port=port, debug=False)
    except KeyboardInterrupt:
        log.info("Interrupted - shutting down.")
    finally:
        WORKER.stop()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        # Catch the second Ctrl+C (during shutdown) so the user doesn't see a
        # traceback. The first one is caught inside main().
        sys.exit(0)
