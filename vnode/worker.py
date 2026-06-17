"""Background worker that orchestrates the Virtual AIS Node."""
from __future__ import annotations

import importlib
import logging
import threading
import time
from typing import Any, Dict, List, Optional

from . import config as cfgmod
from . import encoder, sources
from . import forwarder as forwarder_mod
from .forwarder import Forwarder, SentenceLog


log = logging.getLogger(__name__)


class Worker:
    def __init__(self):
        self.cfg = cfgmod.load()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.sentence_log = SentenceLog(size=int(self.cfg["web"].get("log_size", 500)))
        self.forwarders: List[Forwarder] = []

        # tracked vessels keyed by MMSI -> last vessel dict + last static ts
        self.vessels: Dict[int, Dict[str, Any]] = {}
        self._static_emitted_at: Dict[int, float] = {}

        # status / counters
        self.status: Dict[str, Any] = {
            "running": False,
            "polls":   {"aisfriends": {"last_ok": 0, "last_err": "", "vessels": 0, "next": 0},
                        "aishub":     {"last_ok": 0, "last_err": "", "vessels": 0, "next": 0},
                        "kpler":      {"last_ok": 0, "last_err": "", "vessels": 0, "next": 0}},
            "sentences_sent": 0,
            "started_at": 0,
        }

    # ---- lifecycle ----
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.is_running():
            return
        self.cfg = cfgmod.load()

        # Guard 3: hot-reload leaf modules so source edits made while the
        # interpreter is still running are picked up on Stop -> Start, without
        # needing to drop to a terminal and re-launch the whole process.
        reloaded = []
        for mod in (encoder, sources, forwarder_mod):
            try:
                importlib.reload(mod)
                reloaded.append(mod.__name__.rsplit(".", 1)[-1])
            except Exception as exc:
                log.warning("Reload of %s failed: %s", mod.__name__, exc)
        if reloaded:
            log.info("Hot-reloaded modules on start: %s", ", ".join(reloaded))

        self._stop.clear()
        self._rebuild_forwarders()
        self._thread = threading.Thread(target=self._run, name="vnode-worker", daemon=True)
        self.status["started_at"] = time.time()
        self.status["running"] = True
        self._thread.start()
        log.info("Worker started")


    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        for f in self.forwarders:
            f.close()
        self.status["running"] = False
        log.info("Worker stopped")

    def reload_config(self) -> None:
        """Pick up config changes (called by web layer after save)."""
        self.cfg = cfgmod.load()
        self.sentence_log = SentenceLog(size=int(self.cfg["web"].get("log_size", 500)))
        # Forwarders are recreated on next start; if running we replace them on the fly.
        if self.is_running():
            self._rebuild_forwarders()

    # ---- internals ----
    def _rebuild_forwarders(self) -> None:
        for f in self.forwarders:
            f.close()
        new: List[Forwarder] = []
        for entry in self.cfg.get("outputs", []):
            if not entry.get("enabled", True):
                continue
            new.append(Forwarder(
                protocol=entry.get("protocol", "tcp"),
                host=entry.get("host", "127.0.0.1"),
                port=int(entry.get("port", 10110)),
                name=entry.get("name", ""),
            ))
        self.forwarders = new

    def _emit(self, sentences: List[str], source: str) -> None:
        if not sentences:
            return
        for f in self.forwarders:
            f.send(sentences)
        for s in sentences:
            self.sentence_log.add(s, source=source)
        self.status["sentences_sent"] += sum(1 for f in self.forwarders) and len(sentences)

    def _process_vessels(self, vessels: List[Dict[str, Any]], source: str) -> None:
        now = time.time()
        enc_cfg = self.cfg["encoding"]
        talker  = enc_cfg.get("talker_id", "AIVDM")
        static_every = int(enc_cfg.get("static_every_sec", 360))

        for v in vessels:
            mmsi = v["mmsi"]
            prev = self.vessels.get(mmsi)
            self.vessels[mmsi] = v

            # static: first time we see it, OR if it's been a while since last static
            last_static = self._static_emitted_at.get(mmsi, 0)
            if prev is None or (now - last_static) >= static_every:
                self._emit(encoder.encode_static_24(v, talker_id=talker), source=source)
                self._static_emitted_at[mmsi] = now

            # position
            self._emit(encoder.encode_position_18(v, talker_id=talker), source=source)

    def _poll_one(self, source: str) -> None:
        bbox = self.cfg["bbox"]
        srccfg = self.cfg["sources"].get(source, {})
        if not srccfg.get("enabled", True):
            return
        try:
            if source == "aisfriends":
                vessels = sources.poll_aisfriends(
                    bbox,
                    srccfg.get("token", ""),
                    backend=srccfg.get("backend", "flaresolverr"),
                    flaresolverr_url=srccfg.get("flaresolverr_url", "http://localhost:8191"),
                )
            elif source == "aishub":
                vessels = sources.poll_aishub(bbox, srccfg.get("username", ""))
            elif source == "kpler":
                vessels = sources.poll_kpler(
                    bbox,
                    srccfg.get("credential", ""),
                    api_url=srccfg.get("api_url",  sources.KPLER_DEFAULT_API_URL),
                    token_url=srccfg.get("token_url", sources.KPLER_DEFAULT_TOKEN_URL),
                    audience=srccfg.get("audience",   sources.KPLER_DEFAULT_AUDIENCE),
                    flavour=srccfg.get("flavour",     sources.KPLER_DEFAULT_FLAVOUR),
                )
            else:
                return
            self.status["polls"][source]["last_ok"] = time.time()
            self.status["polls"][source]["last_err"] = ""
            self.status["polls"][source]["vessels"] = len(vessels)
            log.info("Polled %s -> %d vessels", source, len(vessels))
            self._process_vessels(vessels, source=source)
        except Exception as exc:
            self.status["polls"][source]["last_err"] = str(exc)
            log.warning("Poll %s failed: %s", source, exc)

    def _sweep_expired(self) -> int:
        """Remove vessels not heard from in the last `vessel_ttl_seconds`."""
        ttl = int(self.cfg["encoding"].get("vessel_ttl_seconds", 60))
        if ttl <= 0:
            return 0
        cutoff = time.time() - ttl
        expired = [m for m, v in self.vessels.items() if v.get("ts", 0) < cutoff]
        for m in expired:
            self.vessels.pop(m, None)
            self._static_emitted_at.pop(m, None)
        return len(expired)

    def _run(self) -> None:
        interval = int(self.cfg["poll"].get("interval_seconds", 60))
        stagger  = int(self.cfg["poll"].get("stagger_seconds", 30))

        # Run every known source - even if a source is currently disabled
        # we keep it in the schedule so _poll_one() can no-op cheaply, and
        # so toggling it on at runtime starts producing data on the next
        # tick without needing a worker restart.
        order = ["aisfriends", "aishub", "kpler"]

        # Initial offsets - evenly stagger sources so we always have a
        # fresh frame arriving every `stagger` seconds.
        t0 = time.time()
        next_at = {src: t0 + i * stagger for i, src in enumerate(order)}
        for src in order:
            self.status["polls"][src]["next"] = next_at[src]

        while not self._stop.is_set():
            now = time.time()
            for src in order:
                if now >= next_at[src]:
                    self._poll_one(src)
                    next_at[src] = now + interval
                    self.status["polls"][src]["next"] = next_at[src]
            # Drop stale vessels every second so the map fades them out
            # as soon as the TTL elapses, without waiting for the next poll.
            self._sweep_expired()
            self._stop.wait(timeout=1.0)


# Module-level singleton so the web layer + CLI share one worker
WORKER = Worker()
