"""Turn normalised vessel dicts into AIS NMEA sentences using pyais.

We always emit Class B:
  * position  -> Type 18 (Standard Class B CS Position Report)
  * static    -> Type 24 (Class B static, two parts: 24A + 24B)
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List

try:
    from pyais.encode import encode_dict
except ImportError as exc:  # pragma: no cover
    raise SystemExit(
        "The 'pyais' package is required. Install it with: pip install pyais"
    ) from exc

log = logging.getLogger(__name__)


def _clamp(v, lo, hi):
    if v is None:
        return None
    return max(lo, min(hi, v))


def _normalise_talker(talker_id: str) -> str:
    """pyais expects the 2-char talker (e.g. 'AI'), not the full sentence prefix.
    Accept any of 'AI', '!AIVDM', 'AIVDM', 'AIVDO', '!AIVDO', 'AB', etc.
    and return just the first two letters."""
    if not talker_id:
        return "AI"
    t = talker_id.lstrip("!").strip().upper()
    if t.endswith("VDM") or t.endswith("VDO"):
        t = t[:-3]
    if len(t) >= 2:
        return t[:2]
    return "AI"


def _to_vdm(sentence: str) -> str:
    """pyais's encode_dict() emits !xxVDO (own-ship). For a virtual AIS node
    feeding chart plotters we want !xxVDM (received from another vessel),
    so plotters add the targets to the AIS list. Swap the final 'O' of the
    talker block to 'M' and recompute the NMEA-0183 XOR checksum so the
    sentence remains valid."""
    if not sentence or not sentence.startswith("!"):
        return sentence
    try:
        body, sep, _old_cs = sentence[1:].rpartition("*")
        if not sep:
            return sentence
        head, comma, rest = body.partition(",")
        if not comma or not head.endswith("VDO"):
            return sentence  # already VDM, or unexpected format - leave alone
        head = head[:-1] + "M"
        body = head + comma + rest
        cs = 0
        for ch in body:
            cs ^= ord(ch)
        return f"!{body}*{cs:02X}"
    except Exception:
        return sentence




def encode_position_18(vessel: Dict[str, Any], talker_id: str = "AI") -> List[str]:

    """Type 18 - Class B Standard Position Report."""
    sog = vessel.get("sog") or 0.0
    cog = vessel.get("cog") or 0.0
    heading = vessel.get("heading")
    if heading is None or heading > 359:
        heading = 511  # N/A

    payload = {
        "type":     18,
        "repeat":   0,
        "mmsi":     int(vessel["mmsi"]),
        "reserved_1": 0,
        "speed":    _clamp(round(float(sog) * 10) / 10.0, 0.0, 102.2),
        "accuracy": 0,
        "lon":      _clamp(float(vessel["lon"]), -180.0, 180.0),
        "lat":      _clamp(float(vessel["lat"]),  -90.0,  90.0),
        "course":   _clamp(round(float(cog) * 10) / 10.0, 0.0, 359.9),
        "heading":  int(heading),
        "second":   60,    # not available
        "reserved_2": 0,
        "cs":       1,
        "display":  0,
        "dsc":      1,
        "band":     1,
        "msg22":    1,
        "assigned": 0,
        "raim":     0,
        "radio":    0,
    }
    try:
        return [_to_vdm(s.decode() if isinstance(s, bytes) else s)
                for s in encode_dict(payload, talker_id=_normalise_talker(talker_id))]
    except Exception as exc:
        log.warning("Failed to encode Type 18 for MMSI %s: %s", vessel.get("mmsi"), exc)
        return []




def encode_static_24(vessel: Dict[str, Any], talker_id: str = "AI") -> List[str]:
    """Type 24 - Class B static. Returns sentences for part A then part B."""
    sentences: List[str] = []
    mmsi = int(vessel["mmsi"])
    talker = _normalise_talker(talker_id)


    name = (vessel.get("name") or "").upper()
    payload_a = {
        "type":     24,
        "repeat":   0,
        "mmsi":     mmsi,
        "partno":   0,
        "shipname": name[:20],
    }
    try:
        sentences.extend(
            _to_vdm(s.decode() if isinstance(s, bytes) else s)
            for s in encode_dict(payload_a, talker_id=talker)
        )
    except Exception as exc:
        log.warning("Failed to encode Type 24A for MMSI %s: %s", mmsi, exc)


    ais_type = vessel.get("ais_type") or 0
    callsign = (vessel.get("callsign") or "").upper()
    payload_b = {
        "type":     24,
        "repeat":   0,
        "mmsi":     mmsi,
        "partno":   1,
        "shiptype": int(ais_type) if ais_type else 0,
        "vendorid": "VAISN",
        "model":    0,
        "serial":   0,
        "callsign": callsign[:7],
        "to_bow":       int(vessel.get("to_bow")       or 0),
        "to_stern":     int(vessel.get("to_stern")     or 0),
        "to_port":      int(vessel.get("to_port")      or 0),
        "to_starboard": int(vessel.get("to_starboard") or 0),
        "mothership_mmsi": 0,
    }
    try:
        sentences.extend(
            _to_vdm(s.decode() if isinstance(s, bytes) else s)
            for s in encode_dict(payload_b, talker_id=talker)
        )
    except Exception as exc:
        log.warning("Failed to encode Type 24B for MMSI %s: %s", mmsi, exc)


    return sentences
