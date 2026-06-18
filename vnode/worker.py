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
        # status.polls.aishub is kept as the AGGREGATE across all AISHub
        # keys (sum of last vessel counts, most-recent OK/err timestamps)
        # so the existing dashboard tile keeps working unchanged. Per-key
        # state is in status.polls.aishub["keys"][i] for any future UI
        # that wants to drill down.
        self.status: Dict[str, Any] = {
            "running": False,
            "polls":   {"aisfriends": {"last_ok": 0, "last_err": "", "vessels": 0, "next": 0},
                        "aishub":     {"last_ok": 0, "last_err": "", "vessels": 0, "next": 0,
                                       "keys": []},
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

    @staticmethod
    def _mask(s: str) -> str:
        """Tail-mask an AISHub username for safe display ('AH_3943_*****EA')."""
        if not s:
            return ""
        if len(s) <= 4:
            return "*" * len(s)
        return s[:3] + "…" + s[-2:]

    def _poll_one(self, slot: str) -> None:
        """Run a single scheduled slot.

        `slot` is either a plain source name (`aisfriends`, `kpler`) or an
        AISHub key slot in the form `aishub#<index>` so we can route the
        result back to the right per-key status bucket.
        """
        bbox = self.cfg["bbox"]

        # ---- AISHub: one slot per username --------------------------------
        if slot.startswith("aishub#"):
            srccfg = self.cfg["sources"].get("aishub", {})
            if not srccfg.get("enabled", True):
                return
            usernames = cfgmod.aishub_usernames(self.cfg)
            try:
                idx = int(slot.split("#", 1)[1])
            except ValueError:
                return
            if idx >= len(usernames):
                return  # username was removed since the schedule was built
            user = usernames[idx]
            ah_status = self.status["polls"]["aishub"]
            # Make sure the per-key bucket exists (the schedule builder
            # also does this, but be defensive in case of hot-reload).
            while len(ah_status["keys"]) <= idx:
                ah_status["keys"].append({"last_ok": 0, "last_err": "", "vessels": 0,
                                          "next": 0, "username_masked": ""})
            key_status = ah_status["keys"][idx]
            key_status["username_masked"] = self._mask(user)
            try:
                vessels = sources.poll_aishub(bbox, user)
                now = time.time()
                key_status["last_ok"]  = now
                key_status["last_err"] = ""
                key_status["vessels"]  = len(vessels)
                # Aggregate tile: most-recent OK, sum of vessels, clear err.
                ah_status["last_ok"]  = max(ah_status.get("last_ok", 0), now)
                ah_status["last_err"] = ""
                ah_status["vessels"]  = sum(k.get("vessels", 0) for k in ah_status["keys"])
                log.info("Polled aishub key #%d (%s) -> %d vessels",
                         idx, key_status["username_masked"], len(vessels))
                self._process_vessels(vessels, source="aishub")
            except Exception as exc:
                key_status["last_err"] = str(exc)
                ah_status["last_err"]  = f"key #{idx + 1}: {exc}"
                log.warning("Poll aishub key #%d (%s) failed: %s",
                            idx, key_status["username_masked"], exc)
            return

        # ---- other sources unchanged --------------------------------------
        source = slot
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

    def _build_schedule(self, t0: float) -> Dict[str, float]:
        """Build the {slot_name: next_run_epoch} dict from the current config.

        The slot ordering is:

            aisfriends                @ t0
            aishub#0 ... aishub#N-1   @ t0 + stagger + i·(interval/N)
            kpler                     @ t0 + 2·stagger + (N-1)·(interval/N)

        Each AISHub key polls at the full `interval_seconds` cadence (so
        every individual key stays within AISHub's 1 req/min limit), but
        the N keys are evenly spread across the interval so the effective
        AISHub frame rate is `interval_seconds / N`.

        Also resizes status.polls.aishub["keys"] to match N, preserving
        any prior per-key telemetry where indices still line up.
        """
        interval = int(self.cfg["poll"].get("interval_seconds", 60))
        stagger  = int(self.cfg["poll"].get("stagger_seconds", 30))
        usernames = cfgmod.aishub_usernames(self.cfg)
        n = max(1, len(usernames))         # always schedule at least one slot
        key_gap = interval / float(n)

        next_at: Dict[str, float] = {}
        next_at["aisfriends"] = t0
        for i in range(n):
            next_at[f"aishub#{i}"] = t0 + stagger + i * key_gap
        next_at["kpler"] = t0 + 2 * stagger + (n - 1) * key_gap

        # Resize the per-key status array, masking the username for display.
        keys_status = self.status["polls"]["aishub"].setdefault("keys", [])
        while len(keys_status) < n:
            keys_status.append({"last_ok": 0, "last_err": "", "vessels": 0,
                                "next": 0, "username_masked": ""})
        del keys_status[n:]
        for i, user in enumerate(usernames):
            keys_status[i]["username_masked"] = self._mask(user)
            keys_status[i]["next"] = next_at[f"aishub#{i}"]
        # Aggregate "next" = the soonest pending AISHub slot, which is
        # what the existing dashboard tile reads.
        self.status["polls"]["aishub"]["next"] = (
            min((next_at[f"aishub#{i}"] for i in range(n)), default=0)
        )
        self.status["polls"]["aisfriends"]["next"] = next_at["aisfriends"]
        self.status["polls"]["kpler"]["next"]     = next_at["kpler"]
        return next_at

    def _run(self) -> None:
        t0 = time.time()
        next_at = self._build_schedule(t0)

        # Snapshot the AISHub key count so we know when the user has
        # added/removed keys via the Credentials page (web.py calls
        # reload_config() which mutates self.cfg under us) and we need to
        # rebuild the schedule mid-flight.
        last_ah_n = len(cfgmod.aishub_usernames(self.cfg))

        while not self._stop.is_set():
            # Rebuild on key-count change (cheap; preserves per-key telemetry).
            cur_ah_n = len(cfgmod.aishub_usernames(self.cfg))
            if cur_ah_n != last_ah_n:
                log.info("AISHub key count changed %d -> %d, rebuilding schedule",
                         last_ah_n, cur_ah_n)
                next_at = self._build_schedule(time.time())
                last_ah_n = cur_ah_n

            now = time.time()
            for slot, due in list(next_at.items()):
                if now >= due:
                    interval = int(self.cfg["poll"].get("interval_seconds", 60))
                    self._poll_one(slot)
                    next_at[slot] = now + interval
                    # Mirror the new "next" into the right status bucket.
                    if slot.startswith("aishub#"):
                        idx = int(slot.split("#", 1)[1])
                        keys = self.status["polls"]["aishub"]["keys"]
                        if idx < len(keys):
                            keys[idx]["next"] = next_at[slot]
                        # Aggregate "next" = soonest AISHub slot.
                        self.status["polls"]["aishub"]["next"] = min(
                            (k["next"] for k in keys if k.get("next")),
                            default=next_at[slot],
                        )
                    else:
                        self.status["polls"][slot]["next"] = next_at[slot]
            # Drop stale vessels every second so the map fades them out
            # as soon as the TTL elapses, without waiting for the next poll.
            self._sweep_expired()
            self._stop.wait(timeout=1.0)


# Module-level singleton so the web layer + CLI share one worker
WORKER = Worker()
