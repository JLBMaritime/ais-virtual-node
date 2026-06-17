"""Poll AIS Friends, AISHub and Kpler for vessels in a bounding box.

All three source pollers return a list of normalised vessel dicts:

    {
        "mmsi": int,
        "name": str,                # may be ""
        "callsign": str,            # may be ""
        "ais_type": int | None,     # AIS shiptype code
        "lat": float,
        "lon": float,
        "sog": float,               # speed over ground, knots
        "cog": float,               # course over ground, degrees
        "heading": int | None,      # 511 = N/A
        "to_bow": int | None,
        "to_stern": int | None,
        "to_port": int | None,
        "to_starboard": int | None,
        "source": "aisfriends" | "aishub" | "kpler",
        "ts": float,                # epoch seconds
    }
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

import requests

# ---------- backend selection for AIS Friends ----------------------------
# Cloudflare in front of aisfriends.com escalates HTTP-only clients to a
# JavaScript / WebAssembly challenge that nothing in the Python ecosystem
# can solve on its own. The reliable workaround is FlareSolverr, a tiny
# Docker container running headless Chromium that we proxy requests
# through. The other backends are kept as no-Docker fallbacks; they only
# work on networks that haven't been escalated to JS challenge.
#
#   "flaresolverr"  - POST through http://<host>:8191/v1 (recommended)
#   "curl_cffi"     - direct, with real Chrome TLS+H2 fingerprint
#   "cloudscraper"  - direct, basic JS-challenge solver
#   "requests"      - direct plain HTTP (almost certainly 403 now)
#
# The "active" backend below is just what's *available* at import time;
# the actual choice for any given poll comes from the per-call `backend`
# argument so the user can switch from the Credentials page without
# restarting the app.
try:
    from curl_cffi import requests as cffi_requests  # type: ignore
    CURL_CFFI_AVAILABLE: bool = True
except ImportError:
    cffi_requests = None  # type: ignore
    CURL_CFFI_AVAILABLE = False

try:
    import cloudscraper  # type: ignore
    CLOUDSCRAPER_AVAILABLE: bool = True
except ImportError:
    cloudscraper = None  # type: ignore
    CLOUDSCRAPER_AVAILABLE = False

FLARESOLVERR_AVAILABLE: bool = True  # always available - just needs `requests`

#: best-effort guess at what we'd use if the user picked "auto"
AF_BACKEND: str = (
    "flaresolverr" if FLARESOLVERR_AVAILABLE else
    "curl_cffi"    if CURL_CFFI_AVAILABLE   else
    "cloudscraper" if CLOUDSCRAPER_AVAILABLE else
    "requests"
)


BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
COMMON_HEADERS = {
    "User-Agent": BROWSER_UA,
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",
}

AISFRIENDS_BASE  = "https://www.aisfriends.com/api/public/v1"
AISFRIENDS_URL   = AISFRIENDS_BASE + "/vessels/bounding-box"
AISHUB_URL       = "https://data.aishub.net/ws.php"

# Kpler defaults. The Auth0 tenant + GraphQL gateway are public knowledge
# from Kpler's Maritime 2.0 docs; per-customer audience / api URL are
# user-configurable via the Credentials page so this works for whichever
# product their portal-issued client_id is granted to.
KPLER_DEFAULT_TOKEN_URL = "https://auth.kpler.com/oauth/token"
KPLER_DEFAULT_AUDIENCE  = "https://api.kpler.com"
KPLER_DEFAULT_API_URL   = "https://api.sml.kpler.com/graphql"
KPLER_DEFAULT_FLAVOUR   = "graphql"  # "graphql" | "messages"


# ---------- session caches (rebuilt on credential / backend change) ------

_AF_SESSIONS: Dict[str, Any] = {}  # backend name -> session-like object


def _make_direct_session(backend: str):
    """Build a *direct* (non-FlareSolverr) AIS Friends session for the given backend."""
    if backend == "curl_cffi" and CURL_CFFI_AVAILABLE:
        for impersonate in ("chrome124", "chrome120", "chrome"):
            try:
                return cffi_requests.Session(impersonate=impersonate)
            except Exception:
                continue
        # fall through to next-best
    if backend == "cloudscraper" and CLOUDSCRAPER_AVAILABLE:
        sess = cloudscraper.create_scraper(
            browser={"browser": "chrome", "platform": "windows", "mobile": False}
        )
        sess.headers.update({"Accept-Language": "en-US,en;q=0.9"})
        try:
            sess.get("https://www.aisfriends.com/", timeout=15)
        except Exception:
            pass
        return sess
    sess = requests.Session()
    sess.headers.update(COMMON_HEADERS)
    return sess


def _get_af_session(backend: str):
    """Return a cached session for the named backend, building it on first use."""
    if backend not in _AF_SESSIONS:
        _AF_SESSIONS[backend] = _make_direct_session(backend)
    return _AF_SESSIONS[backend]


def reset_af_session(backend: Optional[str] = None) -> None:
    """Tear down cached AIS Friends sessions (e.g. after credentials change)."""
    if backend is None:
        for s in _AF_SESSIONS.values():
            try:
                s.close()
            except Exception:
                pass
        _AF_SESSIONS.clear()
    else:
        s = _AF_SESSIONS.pop(backend, None)
        if s is not None:
            try:
                s.close()
            except Exception:
                pass


def _make_ah_session() -> requests.Session:
    sess = requests.Session()
    sess.headers.update(COMMON_HEADERS)
    return sess


_AH_SESSION = _make_ah_session()


# ---------- small helpers -------------------------------------------------

def _to_float(v: Any) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _to_int(v: Any) -> Optional[int]:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _first(d: Dict[str, Any], *keys: str) -> Any:
    """Return the first key from d that is present AND not None / ""."""
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
    return None


# ---------- FlareSolverr proxy -------------------------------------------

class FlareSolverrError(RuntimeError):
    """Raised when FlareSolverr itself reports a problem."""


#: cache of last-known good (cookies, user-agent) per FlareSolverr URL so we
#: don't re-solve the Cloudflare challenge on every poll.
_FS_COOKIE_CACHE: Dict[str, Dict[str, Any]] = {}


def _flaresolverr_solve(flaresolverr_url: str, target_url: str, timeout: int) -> Dict[str, Any]:
    """Hit FlareSolverr once to obtain a valid cf_clearance cookie + matching UA.

    Returns {"cookies": {name: value, ...}, "user_agent": str}.

    Why not just let FlareSolverr issue the API call? In v3.0+ FlareSolverr
    silently dropped support for custom request headers, so our
    `Authorization: Bearer ...` would never reach AIS Friends - the API
    would just return the public Cloudflare interstitial. The standard
    workaround is to use FlareSolverr to *only* solve the challenge, then
    issue the real authenticated request ourselves with the resulting
    cookie + UA via curl_cffi (which also matches Chrome's TLS+H2
    fingerprint, so Cloudflare keeps letting us through).
    """
    endpoint = flaresolverr_url.rstrip("/") + "/v1"
    payload = {
        "cmd":        "request.get",
        "url":        target_url,
        "maxTimeout": max(30_000, timeout * 1000),
    }
    try:
        r = requests.post(endpoint, json=payload, timeout=timeout + 30)
    except requests.exceptions.ConnectionError as exc:
        raise FlareSolverrError(
            f"FlareSolverr container is not reachable at {endpoint} - "
            f"is it running? Start it with: docker start flaresolverr"
        ) from exc
    if r.status_code >= 500:
        raise FlareSolverrError(f"FlareSolverr returned HTTP {r.status_code}: {r.text[:200]}")
    try:
        body = r.json()
    except ValueError as exc:
        raise FlareSolverrError(f"FlareSolverr returned non-JSON: {r.text[:200]}") from exc
    if body.get("status") != "ok":
        raise FlareSolverrError(
            f"FlareSolverr: {body.get('message') or body.get('status') or 'unknown error'}"
        )
    sol = body.get("solution") or {}
    cookies = {c["name"]: c["value"] for c in (sol.get("cookies") or [])
               if isinstance(c, dict) and "name" in c and "value" in c}
    ua = sol.get("userAgent") or BROWSER_UA
    return {"cookies": cookies, "user_agent": ua}


def _aisfriends_via_flaresolverr(
    full_url: str,
    bearer_token: str,
    flaresolverr_url: str,
    timeout: int,
) -> Any:
    """Fetch the AIS Friends API via FlareSolverr-warmed cookies + curl_cffi.

    Strategy:
      1. Ask FlareSolverr for a valid `cf_clearance` cookie + the UA its
         bundled Chromium used. Cache it so we only re-solve when the cookie
         expires.
      2. Issue the real API request ourselves through curl_cffi (Chrome
         TLS+H2 fingerprint) carrying that cookie + UA + our
         `Authorization: Bearer ...` header.
      3. On 401/403 (cookie went stale or token bad), nuke the cache and
         retry exactly once.
    """
    def _do_request(cookies: Dict[str, str], user_agent: str):
        headers = {
            "Authorization":   f"Bearer {bearer_token}",
            "Accept":          "application/json",
            "User-Agent":      user_agent,
            "Accept-Language": "en-US,en;q=0.9",
        }
        if CURL_CFFI_AVAILABLE:
            for impersonate in ("chrome124", "chrome120", "chrome"):
                try:
                    return cffi_requests.get(
                        full_url,
                        headers=headers,
                        cookies=cookies,
                        impersonate=impersonate,
                        timeout=timeout,
                    )
                except Exception:
                    continue
        # Fallback - plain requests. TLS fingerprint will look like Python,
        # but with a fresh cf_clearance cookie Cloudflare often still
        # passes us through.
        return requests.get(full_url, headers=headers, cookies=cookies, timeout=timeout)

    # Step 1: warm cookies (cached per FlareSolverr URL).
    cached = _FS_COOKIE_CACHE.get(flaresolverr_url)
    if cached is None:
        cached = _flaresolverr_solve(flaresolverr_url, "https://www.aisfriends.com/", timeout)
        _FS_COOKIE_CACHE[flaresolverr_url] = cached

    # Step 2: real request.
    resp = _do_request(cached["cookies"], cached["user_agent"])

    # Step 3: on cookie-staleness, re-solve once.
    if resp.status_code in (401, 403):
        _FS_COOKIE_CACHE.pop(flaresolverr_url, None)
        cached = _flaresolverr_solve(flaresolverr_url, "https://www.aisfriends.com/", timeout)
        _FS_COOKIE_CACHE[flaresolverr_url] = cached
        resp = _do_request(cached["cookies"], cached["user_agent"])

    # Re-raise as a real HTTPError so the web layer can classify 401/403/etc.
    if resp.status_code >= 400:
        fake = requests.Response()
        fake.status_code = resp.status_code
        try:
            fake._content = resp.content if isinstance(resp.content, bytes) else str(resp.text).encode("utf-8", "replace")
        except Exception:
            fake._content = b""
        fake.url = full_url
        raise requests.HTTPError(
            f"AIS Friends returned HTTP {resp.status_code}",
            response=fake,
        )

    # Parse JSON. curl_cffi has .json(); plain requests too.
    try:
        return resp.json()
    except ValueError as exc:
        body = ""
        try:
            body = resp.text[:200]
        except Exception:
            pass
        raise FlareSolverrError(
            f"AIS Friends body was not JSON (first 200 chars): {body}"
        ) from exc



# ---------- AIS Friends ---------------------------------------------------

def poll_aisfriends(
    bbox: Dict[str, float],
    token: str,
    timeout: int = 30,
    backend: str = "flaresolverr",
    flaresolverr_url: str = "http://localhost:8191",
) -> List[Dict[str, Any]]:
    """Returns normalised vessels from AIS Friends, or raises on error.

    `backend` selects the transport:
       "flaresolverr" | "curl_cffi" | "cloudscraper" | "requests"
    """
    if not token:
        raise RuntimeError("AIS Friends token is not set")

    params = {
        "lat_min": bbox["latmin"],
        "lat_max": bbox["latmax"],
        "lon_min": bbox["lonmin"],
        "lon_max": bbox["lonmax"],
        "format":  "json",
    }

    if backend == "flaresolverr":
        # Build the full URL with query string ourselves; FlareSolverr just
        # GETs whatever URL we give it.
        prepared = requests.Request("GET", AISFRIENDS_URL, params=params).prepare()
        data = _aisfriends_via_flaresolverr(
            prepared.url, token, flaresolverr_url, timeout
        )
    else:
        sess = _get_af_session(backend)
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        resp = sess.get(AISFRIENDS_URL, params=params, headers=headers, timeout=timeout)
        resp.raise_for_status()
        data = resp.json()

    if not isinstance(data, list):
        return []

    now = time.time()
    out: List[Dict[str, Any]] = []
    for v in data:
        mmsi = _to_int(v.get("mmsi"))
        # The docs use "latitude"/"longitude". Tolerate "lat"/"lon" too.
        lat  = _to_float(_first(v, "latitude",  "lat"))
        lon  = _to_float(_first(v, "longitude", "lon"))
        if mmsi is None or lat is None or lon is None:
            continue
        out.append({
            "mmsi":         mmsi,
            "name":         (str(v.get("name") or v.get("reported_name") or "")).strip()[:20],
            "callsign":     (str(v.get("call_sign") or "")).strip()[:7],
            "ais_type":     _to_int(v.get("ais_type")),
            "lat":          lat,
            "lon":          lon,
            # Per the AIS Friends API docs:
            #   speed_over_ground, course_over_ground, true_heading
            # (older non-documented fields "speed"/"course"/"heading" kept
            # as a fallback so we don't silently break if the API ever
            # changes back.)
            "sog":          _to_float(_first(v, "speed_over_ground",  "speed"))  or 0.0,
            "cog":          _to_float(_first(v, "course_over_ground", "course")) or 0.0,
            "heading":      _to_int  (_first(v, "true_heading",       "heading")),
            "to_bow":       _to_int(v.get("to_bow")),
            "to_stern":     _to_int(v.get("to_stern")),
            "to_port":      _to_int(v.get("to_port")),
            "to_starboard": _to_int(v.get("to_starboard")),
            "source":       "aisfriends",
            "ts":           now,
        })
    return out


# ---------- AISHub --------------------------------------------------------

def poll_aishub(bbox: Dict[str, float], username: str, timeout: int = 30) -> List[Dict[str, Any]]:
    """Returns normalised vessels from AISHub, or [] on error."""
    if not username:
        raise RuntimeError("AISHub username is not set")
    params = {
        "username": username,
        "output":   "json",
        "format":   "1",    # human readable
        "latmin":   bbox["latmin"],
        "latmax":   bbox["latmax"],
        "lonmin":   bbox["lonmin"],
        "lonmax":   bbox["lonmax"],
    }
    resp = _AH_SESSION.get(AISHUB_URL, params=params, timeout=timeout)
    resp.raise_for_status()
    body = resp.json()
    # AISHub returns: [<meta>, [<vessels>]]
    if not isinstance(body, list) or len(body) < 2:
        # error envelope?
        if isinstance(body, list) and body and isinstance(body[0], dict) and body[0].get("ERROR"):
            raise RuntimeError(f"AISHub error: {body[0].get('ERROR_MESSAGE')}")
        return []
    vessels = body[1] if isinstance(body[1], list) else []
    now = time.time()
    out: List[Dict[str, Any]] = []
    for v in vessels:
        mmsi = _to_int(v.get("MMSI"))
        lat  = _to_float(v.get("LATITUDE"))
        lon  = _to_float(v.get("LONGITUDE"))
        if mmsi is None or lat is None or lon is None:
            continue
        # AISHub: A=to_bow B=to_stern C=to_port D=to_starboard
        out.append({
            "mmsi":         mmsi,
            "name":         (v.get("NAME") or "").strip()[:20],
            "callsign":     (v.get("CALLSIGN") or "").strip()[:7],
            "ais_type":     _to_int(v.get("TYPE")),
            "lat":          lat,
            "lon":          lon,
            "sog":          _to_float(v.get("SOG")) or 0.0,
            "cog":          _to_float(v.get("COG")) or 0.0,
            "heading":      _to_int(v.get("HEADING")),
            "to_bow":       _to_int(v.get("A")),
            "to_stern":     _to_int(v.get("B")),
            "to_port":      _to_int(v.get("C")),
            "to_starboard": _to_int(v.get("D")),
        "source":       "aishub",
            "ts":           now,
        })
    return out


# ---------- Kpler ---------------------------------------------------------
#
# Kpler's Maritime APIs live behind an OAuth2 client-credentials flow
# served by Auth0 at auth.kpler.com. The credentials a customer mints in
# developers.kpler.com/my-api-keys arrive as a single base64 string that
# decodes to "client_id:client_secret" - which is what we ask for in the
# Credentials UI (split-or-paste-whole both supported).
#
# Two API flavours are supported behind the same poller signature:
#
#   "graphql"  -> POST <api_url> with a Bearer access token and a
#                 vessels(areaOfInterest:{polygon:...}) query.
#                 Recommended - one normalised row per vessel.
#
#   "messages" -> GET  <api_url>?fields=decoded&position=<GeoJSON polygon>
#                 with a Bearer access token. Returns raw AIS messages;
#                 we de-dupe to the latest per MMSI.
#
# audience / token_url / api_url are user-configurable because Kpler's
# Auth0 tenant has multiple registered audiences (api.kpler.com vs
# terminal.kpler.com vs others added per-product), and which one this
# customer's client_id is granted to depends on their plan.

class KplerError(RuntimeError):
    """Raised when a Kpler poll fails in a way we can categorise."""


# Cached access tokens, keyed by (client_id, token_url, audience). The
# token + the absolute epoch-second expiry at which we should refresh
# (we refresh slightly early to avoid edge-cases).
_KPLER_TOKEN_CACHE: Dict[tuple, Dict[str, Any]] = {}


def _kpler_split_credential(raw: str) -> tuple:
    """Accept either '<client_id>:<client_secret>' or a base64 of same.

    Returns (client_id, client_secret). Raises KplerError if the input is
    obviously not credentials.
    """
    import base64
    raw = (raw or "").strip()
    if not raw:
        raise KplerError("Kpler credentials are not set")
    # Already in "id:secret" form?
    if raw.count(":") == 1 and " " not in raw and "/" not in raw and "+" not in raw:
        cid, sec = raw.split(":", 1)
        if cid and sec:
            return cid, sec
    # Try base64 -> "id:secret"
    try:
        decoded = base64.b64decode(raw, validate=False).decode("ascii", "replace")
    except Exception as exc:
        raise KplerError("Kpler credential is neither 'id:secret' nor base64") from exc
    if ":" not in decoded:
        raise KplerError(
            "Kpler credential decoded but doesn't contain ':' - expected 'client_id:client_secret'"
        )
    cid, sec = decoded.split(":", 1)
    if not cid or not sec:
        raise KplerError("Kpler credential decoded but client_id or client_secret is empty")
    return cid, sec


def _kpler_get_token(
    credential: str,
    token_url: str,
    audience: str,
    timeout: int,
) -> str:
    """Mint or reuse an Auth0 access token for the given credentials + audience.

    Cached until ~60s before expiry. Re-mints on cache miss or expiry.
    """
    cid, sec = _kpler_split_credential(credential)
    key = (cid, token_url, audience)
    cached = _KPLER_TOKEN_CACHE.get(key)
    now = time.time()
    if cached and cached.get("expires_at", 0) > now + 60:
        return cached["access_token"]

    body = {
        "grant_type":    "client_credentials",
        "client_id":     cid,
        "client_secret": sec,
        "audience":      audience,
    }
    try:
        r = requests.post(token_url, data=body, timeout=timeout)
    except requests.exceptions.ConnectionError as exc:
        raise KplerError(f"Could not reach Kpler token endpoint {token_url}: {exc}") from exc
    except requests.exceptions.Timeout as exc:
        raise KplerError(f"Timeout contacting Kpler token endpoint {token_url}") from exc

    if r.status_code != 200:
        # Try to pull a clean error message out of the Auth0 envelope.
        msg = r.text[:400]
        try:
            j = r.json()
            err  = j.get("error") or ""
            desc = j.get("error_description") or ""
            msg  = (f"{err}: {desc}" if err or desc else msg).strip(": ")
        except Exception:
            pass
        raise KplerError(f"Kpler token endpoint returned HTTP {r.status_code} - {msg}")

    try:
        data = r.json()
    except ValueError as exc:
        raise KplerError(f"Kpler token endpoint returned non-JSON: {r.text[:200]}") from exc

    access_token = data.get("access_token")
    if not access_token:
        raise KplerError(f"Kpler token endpoint did not return an access_token: {data}")

    expires_in = int(data.get("expires_in") or 3600)
    _KPLER_TOKEN_CACHE[key] = {
        "access_token": access_token,
        "expires_at":   now + expires_in,
    }
    return access_token


def reset_kpler_cache() -> None:
    """Drop cached Kpler access tokens (e.g. after credentials change)."""
    _KPLER_TOKEN_CACHE.clear()


def _bbox_to_polygon(bbox: Dict[str, float]) -> Dict[str, Any]:
    """Turn our 4-tuple bbox into a closed GeoJSON Polygon ring."""
    la1, la2 = bbox["latmin"], bbox["latmax"]
    lo1, lo2 = bbox["lonmin"], bbox["lonmax"]
    return {
        "type": "Polygon",
        "coordinates": [[
            [lo1, la1],
            [lo2, la1],
            [lo2, la2],
            [lo1, la2],
            [lo1, la1],
        ]],
    }


# Selection-set used by the graphql flavour. We ask for the smallest set
# of fields we need to populate a vessel dict, plus a couple of common
# aliases (`latestPosition` is used by some plans rather than
# `lastPosition`). The poller transparently picks whichever the server
# actually returns.
_KPLER_GRAPHQL_QUERY = """
query VesselsInAOI($polygon: JSON!) {
  vessels(areaOfInterest: { polygon: $polygon }) {
    mmsi
    imo
    name
    callSign
    shipType
    dimensions { toBow toStern toPort toStarboard }
    lastPosition { latitude longitude sog cog heading timestamp }
  }
}
""".strip()


def _normalise_kpler_vessel(v: Dict[str, Any], now: float) -> Optional[Dict[str, Any]]:
    """Map a single Kpler vessel dict (GraphQL OR messages flavour) to our shape."""
    mmsi = _to_int(v.get("mmsi") or v.get("MMSI"))
    if mmsi is None:
        return None

    # position: lastPosition / latestPosition (graphql) or a flat record (messages)
    pos = v.get("lastPosition") or v.get("latestPosition") or v
    lat = _to_float(pos.get("latitude") if isinstance(pos, dict) else None) \
          if pos is not v else _to_float(v.get("latitude") or v.get("lat"))
    lon = _to_float(pos.get("longitude") if isinstance(pos, dict) else None) \
          if pos is not v else _to_float(v.get("longitude") or v.get("lon"))
    if lat is None or lon is None:
        return None

    sog = _to_float((pos or {}).get("sog") if isinstance(pos, dict) else v.get("sog")) or 0.0
    cog = _to_float((pos or {}).get("cog") if isinstance(pos, dict) else v.get("cog")) or 0.0
    hdg = _to_int  ((pos or {}).get("heading") if isinstance(pos, dict) else v.get("heading"))

    dims = v.get("dimensions") or {}
    return {
        "mmsi":         mmsi,
        "name":         (str(v.get("name") or v.get("vesselName") or "")).strip()[:20],
        "callsign":     (str(v.get("callSign") or v.get("callsign") or "")).strip()[:7],
        "ais_type":     _to_int(v.get("shipType") or v.get("aisShipType") or v.get("type")),
        "lat":          lat,
        "lon":          lon,
        "sog":          sog,
        "cog":          cog,
        "heading":      hdg,
        "to_bow":       _to_int(dims.get("toBow")       or v.get("toBow")       or v.get("a")),
        "to_stern":     _to_int(dims.get("toStern")     or v.get("toStern")     or v.get("b")),
        "to_port":      _to_int(dims.get("toPort")      or v.get("toPort")      or v.get("c")),
        "to_starboard": _to_int(dims.get("toStarboard") or v.get("toStarboard") or v.get("d")),
        "source":       "kpler",
        "ts":           now,
    }


def poll_kpler(
    bbox: Dict[str, float],
    credential: str,
    api_url: str = KPLER_DEFAULT_API_URL,
    token_url: str = KPLER_DEFAULT_TOKEN_URL,
    audience: str = KPLER_DEFAULT_AUDIENCE,
    flavour: str = KPLER_DEFAULT_FLAVOUR,
    timeout: int = 30,
) -> List[Dict[str, Any]]:
    """Returns normalised vessels from Kpler, or raises KplerError on error.

    `credential` may be either `<client_id>:<client_secret>` or its base64
    encoding - the form `developers.kpler.com/my-api-keys` provides.
    """
    flavour = (flavour or KPLER_DEFAULT_FLAVOUR).lower()
    if flavour not in ("graphql", "messages"):
        raise KplerError(f"Unknown Kpler flavour: {flavour!r} (use 'graphql' or 'messages')")

    token = _kpler_get_token(credential, token_url, audience, timeout)
    polygon = _bbox_to_polygon(bbox)
    now = time.time()

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept":        "application/json",
        "User-Agent":    BROWSER_UA,
    }

    if flavour == "graphql":
        try:
            r = requests.post(
                api_url,
                json={"query": _KPLER_GRAPHQL_QUERY, "variables": {"polygon": polygon}},
                headers={**headers, "Content-Type": "application/json"},
                timeout=timeout,
            )
        except requests.exceptions.RequestException as exc:
            raise KplerError(f"Could not reach Kpler GraphQL endpoint {api_url}: {exc}") from exc
        if r.status_code == 401:
            reset_kpler_cache()
            raise KplerError("HTTP 401 from Kpler GraphQL - access token rejected. "
                             "The token was minted but the API didn't accept it; "
                             "verify the audience matches the product your key grants.")
        if r.status_code >= 400:
            raise KplerError(f"HTTP {r.status_code} from Kpler GraphQL: {r.text[:300]}")
        try:
            body = r.json()
        except ValueError as exc:
            raise KplerError(f"Kpler GraphQL returned non-JSON: {r.text[:200]}") from exc
        if body.get("errors"):
            first = body["errors"][0] if isinstance(body["errors"], list) else body["errors"]
            msg = (first or {}).get("message") if isinstance(first, dict) else str(first)
            raise KplerError(f"Kpler GraphQL error: {msg}")
        data = (body.get("data") or {}).get("vessels") or []
        # Some plans wrap the list in {edges:[{node:...}]}, Relay-style.
        if isinstance(data, dict) and "edges" in data:
            data = [edge.get("node") for edge in data.get("edges") or [] if edge]
        if not isinstance(data, list):
            return []
        out: List[Dict[str, Any]] = []
        for v in data:
            if not isinstance(v, dict):
                continue
            n = _normalise_kpler_vessel(v, now)
            if n is not None:
                out.append(n)
        return out

    # ---- messages flavour ----
    import json as _json
    params = {
        "fields":   "decoded",
        "position": _json.dumps(polygon, separators=(",", ":")),
    }
    try:
        r = requests.get(api_url, params=params, headers=headers, timeout=timeout)
    except requests.exceptions.RequestException as exc:
        raise KplerError(f"Could not reach Kpler Messages endpoint {api_url}: {exc}") from exc
    if r.status_code == 401:
        reset_kpler_cache()
        raise KplerError("HTTP 401 from Kpler Messages - access token rejected.")
    if r.status_code >= 400:
        raise KplerError(f"HTTP {r.status_code} from Kpler Messages: {r.text[:300]}")
    try:
        body = r.json()
    except ValueError as exc:
        raise KplerError(f"Kpler Messages returned non-JSON: {r.text[:200]}") from exc

    # Response could be a flat list, or {data:[...]} wrapped.
    if isinstance(body, dict):
        msgs = body.get("data") or body.get("messages") or body.get("results") or []
    else:
        msgs = body
    if not isinstance(msgs, list):
        return []

    # De-dupe to latest-per-MMSI by timestamp.
    by_mmsi: Dict[int, Dict[str, Any]] = {}
    for m in msgs:
        if not isinstance(m, dict):
            continue
        mmsi = _to_int(m.get("mmsi") or m.get("MMSI"))
        if mmsi is None:
            continue
        ts = m.get("timestamp") or m.get("time") or 0
        existing = by_mmsi.get(mmsi)
        if existing is None or (ts and ts > (existing.get("timestamp") or 0)):
            by_mmsi[mmsi] = m

    out = []
    for m in by_mmsi.values():
        n = _normalise_kpler_vessel(m, now)
        if n is not None:
            out.append(n)
    return out
