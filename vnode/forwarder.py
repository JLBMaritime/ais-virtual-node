"""TCP / UDP forwarder for NMEA sentences with retry + log buffer."""
from __future__ import annotations

import collections
import logging
import socket
import threading
import time
from typing import Any, Deque, Dict, List, Optional

log = logging.getLogger(__name__)


class Forwarder:
    """Sends NMEA sentences to one configured endpoint (TCP or UDP).

    For TCP: maintains a persistent socket, reconnecting on failure.
    For UDP: sends datagrams; no connection state.
    """

    def __init__(self, protocol: str, host: str, port: int, name: str = ""):
        self.protocol = protocol.lower()
        self.host = host
        self.port = int(port)
        self.name = name or f"{self.protocol}://{host}:{port}"
        self._sock: Optional[socket.socket] = None
        self._lock = threading.Lock()
        self.last_error: str = ""
        self.connected: bool = False
        self.sent_count: int = 0
        self._last_attempt = 0.0

    def close(self) -> None:
        with self._lock:
            if self._sock is not None:
                try:
                    self._sock.close()
                except Exception:
                    pass
                self._sock = None
            self.connected = False

    def _connect_tcp(self) -> bool:
        try:
            s = socket.create_connection((self.host, self.port), timeout=10)
            s.settimeout(10)
            self._sock = s
            self.connected = True
            self.last_error = ""
            log.info("Connected to %s:%d (TCP)", self.host, self.port)
            return True
        except OSError as exc:
            self.connected = False
            self.last_error = f"connect failed: {exc}"
            log.warning("TCP connect to %s:%d failed: %s", self.host, self.port, exc)
            return False

    def send(self, sentences: List[str]) -> int:
        """Send each sentence followed by CRLF. Returns the number actually sent."""
        if not sentences:
            return 0
        data = "".join(s if s.endswith("\r\n") else s + "\r\n" for s in sentences).encode("ascii", "replace")

        with self._lock:
            if self.protocol == "udp":
                try:
                    if self._sock is None:
                        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                    self._sock.sendto(data, (self.host, self.port))
                    self.sent_count += len(sentences)
                    self.last_error = ""
                    self.connected = True
                    return len(sentences)
                except OSError as exc:
                    self.last_error = f"udp send failed: {exc}"
                    self.connected = False
                    return 0

            # TCP path
            now = time.time()
            if self._sock is None:
                # Throttle reconnect attempts to once every 3s
                if now - self._last_attempt < 3:
                    return 0
                self._last_attempt = now
                if not self._connect_tcp():
                    return 0
            try:
                assert self._sock is not None
                self._sock.sendall(data)
                self.sent_count += len(sentences)
                self.last_error = ""
                return len(sentences)
            except OSError as exc:
                self.last_error = f"tcp send failed: {exc}"
                log.warning("TCP send to %s:%d failed: %s - reconnecting next time",
                            self.host, self.port, exc)
                try:
                    self._sock.close()
                except Exception:
                    pass
                self._sock = None
                self.connected = False
                return 0

    def status(self) -> Dict[str, Any]:
        return {
            "name":       self.name,
            "protocol":   self.protocol,
            "host":       self.host,
            "port":       self.port,
            "connected":  self.connected,
            "sent_count": self.sent_count,
            "last_error": self.last_error,
        }


class SentenceLog:
    """Thread-safe ring buffer for the last N NMEA sentences."""

    def __init__(self, size: int = 500):
        self._buf: Deque[Dict[str, Any]] = collections.deque(maxlen=size)
        self._lock = threading.Lock()
        self._seq = 0

    def add(self, sentence: str, source: str = "") -> Dict[str, Any]:
        with self._lock:
            self._seq += 1
            entry = {
                "id":  self._seq,
                "ts":  time.time(),
                "src": source,
                "txt": sentence.rstrip("\r\n"),
            }
            self._buf.append(entry)
            return entry

    def latest(self, after_id: int = 0, limit: int = 200) -> List[Dict[str, Any]]:
        with self._lock:
            items = [e for e in self._buf if e["id"] > after_id]
        return items[-limit:]

    def clear(self) -> None:
        with self._lock:
            self._buf.clear()
