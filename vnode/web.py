"""Flask web UI for the Virtual AIS Node."""
from __future__ import annotations

import time
from datetime import timedelta
from typing import Any, Dict

from flask import (Flask, jsonify, redirect, render_template, request,
                   session, url_for)

from . import auth as auth_mod
from . import config as cfgmod
from ._paths import bundle_root
from .worker import WORKER

# In a PyInstaller one-file build the templates/ and static/ folders live
# inside `sys._MEIPASS`, not next to the .exe. `bundle_root()` returns the
# right directory for both frozen and dev runs.
_ROOT = bundle_root()
app = Flask(__name__,
            template_folder=str(_ROOT / "templates"),
            static_folder=str(_ROOT / "static"))

# Signing key for the session cookie.  Lazily loaded from auth.SECRET_KEY_PATH
# so importing this module doesn't touch the disk unless Flask is actually
# starting up.  See auth.load_secret_key() for the on-disk format.
app.secret_key = auth_mod.load_secret_key()
# Sessions survive 12h of inactivity but evaporate when the browser closes
# unless the user ticks "remember me" (we don't expose that – sessions are
# permanent by default once signed in, see auth.login_user()).
app.permanent_session_lifetime = timedelta(hours=12)


# ---------------------------------------------------------------------------
# No-cache for API responses
# ---------------------------------------------------------------------------
@app.after_request
def _no_cache_api(resp):
    """Forbid every browser from caching JSON API responses.

    Without this, iOS WebKit (Safari + "Chrome" on iPhone, which is just
    Safari under the hood) applies *heuristic freshness* to same-origin
    fetches and reuses a single /api/status payload for ~30 s – the
    Dashboard's status pill goes stale and the user thinks the worker
    has stopped.  Belt-and-braces: client side already appends a
    cache-buster too.
    """
    if request.path.startswith("/api/"):
        resp.headers["Cache-Control"] = "no-store, max-age=0"
        resp.headers["Pragma"] = "no-cache"
    return resp


@app.context_processor
def _inject_user():
    """Make ``current_user`` available to every template (notably base.html)."""
    return {"current_user": auth_mod.current_user()}


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------
@app.route("/login", methods=["GET", "POST"])
def login():
    """Sign-in page.  Honours an optional ?next= for deep links."""
    user = auth_mod.load_user()
    # The "first-run defaults are JLBMaritime/Admin" hint is only shown
    # while the must_change_password flag is still set – once the user
    # has chosen a real password we don't want a stale hint advertising
    # credentials that no longer work.
    show_hint = user["must_change_password"]

    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        next_url = request.form.get("next") or ""
        if auth_mod.verify(username, password):
            auth_mod.login_user(username)
            # If the password is still the default, force-change first.
            if session.get("must_change_password"):
                return redirect(url_for("change_password_page"))
            # Only redirect to next= if it's a same-site relative path,
            # to avoid open-redirect via crafted ?next=https://evil.example.
            if next_url.startswith("/") and not next_url.startswith("//"):
                return redirect(next_url)
            return redirect(url_for("index"))
        return render_template(
            "login.html",
            error="Invalid username or password.",
            username=username,
            next_url=next_url,
            show_default_hint=show_hint,
        ), 401

    return render_template(
        "login.html",
        next_url=request.args.get("next", ""),
        show_default_hint=show_hint,
    )


@app.route("/logout")
def logout():
    auth_mod.logout_user()
    return redirect(url_for("login"))


@app.route("/change-password", methods=["GET", "POST"])
def change_password_page():
    """Handles both the forced-on-first-login flow and voluntary changes.

    The two flows share a template; the only behavioural difference is
    that the voluntary flow re-verifies the current password.  Either
    way, on success the must_change_password flag is cleared, the
    session is refreshed and the user is bounced to the dashboard.
    """
    user = auth_mod.current_user()
    if not user:
        # If you hit this URL without a session, send you back through
        # /login first.  This also covers the case where the session
        # cookie was cleared (e.g. a server-side secret_key rotation).
        return redirect(url_for("login", next=request.path))

    forced = bool(session.get("must_change_password"))

    if request.method == "POST":
        new_password     = request.form.get("new_password") or ""
        confirm_password = request.form.get("confirm_password") or ""
        current_password = request.form.get("current_password") or ""

        # Common validation
        if len(new_password) < 8:
            return render_template("change_password.html", forced=forced,
                                   error="Password must be at least 8 characters."), 400
        if new_password != confirm_password:
            return render_template("change_password.html", forced=forced,
                                   error="The two new passwords do not match."), 400

        # Voluntary flow: prove the user actually knows the current pw.
        if not forced:
            if not auth_mod.verify(user, current_password):
                return render_template("change_password.html", forced=forced,
                                       error="Current password is incorrect."), 400

        auth_mod.set_password(new_password, clear_must_change=True)
        # Refresh the session so the must_change_password flag isn't
        # stale (otherwise login_required would keep redirecting us
        # back here for the rest of the session lifetime).
        auth_mod.login_user(user)
        return redirect(url_for("index"))

    return render_template("change_password.html", forced=forced)


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------
@app.route("/")
@auth_mod.login_required
def index():
    return render_template("dashboard.html", page="dashboard")


@app.route("/config")
@auth_mod.login_required
def config_page():
    return render_template("config.html", page="config", cfg=cfgmod.load())


@app.route("/credentials")
@auth_mod.login_required
def credentials_page():
    cfg = cfgmod.load()
    return render_template("credentials.html", page="credentials", cfg=cfg)


@app.route("/logs")
@auth_mod.login_required
def logs_page():
    return render_template("logs.html", page="logs")


@app.route("/wifi")
@auth_mod.login_required
def wifi_page():
    return render_template("wifi.html", page="wifi")


# ---------------------------------------------------------------------------
# Public liveness probe – the *only* unauthenticated endpoint.  Used by
# external monitors / uptime checks.  Deliberately discloses zero
# user-meaningful state.
# ---------------------------------------------------------------------------
@app.route("/api/healthz")
def api_healthz():
    return jsonify({"ok": True, "running": WORKER.is_running()})


# ---------------------------------------------------------------------------
# API: status / control
# ---------------------------------------------------------------------------
@app.route("/api/status")
@auth_mod.login_required
def api_status():
    s = dict(WORKER.status)
    s["running"]    = WORKER.is_running()
    s["forwarders"] = [f.status() for f in WORKER.forwarders]
    s["vessels"]    = len(WORKER.vessels)
    s["now"]        = time.time()
    return jsonify(s)


@app.route("/api/start", methods=["POST"])
@auth_mod.login_required
def api_start():
    WORKER.start()
    return jsonify({"running": WORKER.is_running()})


@app.route("/api/stop", methods=["POST"])
@auth_mod.login_required
def api_stop():
    WORKER.stop()
    return jsonify({"running": WORKER.is_running()})


@app.route("/api/vessels")
@auth_mod.login_required
def api_vessels():
    out = []
    for v in WORKER.vessels.values():
        out.append({
            "mmsi":     v["mmsi"],
            "name":     v.get("name", ""),
            "lat":      v["lat"],
            "lon":      v["lon"],
            "sog":      v.get("sog"),
            "cog":      v.get("cog"),
            "heading":  v.get("heading"),
            "ais_type": v.get("ais_type"),
            "src":      v.get("source"),
            "ts":       v.get("ts"),
        })
    return jsonify(out)


@app.route("/api/log")
@auth_mod.login_required
def api_log():
    after = int(request.args.get("after", 0))
    limit = int(request.args.get("limit", 200))
    return jsonify(WORKER.sentence_log.latest(after_id=after, limit=limit))


# ---------------------------------------------------------------------------
# API: config
# ---------------------------------------------------------------------------
@app.route("/api/config", methods=["GET"])
@auth_mod.login_required
def api_config_get():
    return jsonify(cfgmod.load())


@app.route("/api/config", methods=["POST"])
@auth_mod.login_required
def api_config_post():
    data: Dict[str, Any] = request.get_json(silent=True) or {}
    cfgmod.update(data)
    WORKER.reload_config()
    return jsonify(cfgmod.load())


@app.route("/api/credentials", methods=["POST"])
@auth_mod.login_required
def api_credentials_post():
    data: Dict[str, Any] = request.get_json(silent=True) or {}
    # data keys (all optional):
    #   aisfriends_token, aisfriends_backend, aisfriends_flaresolverr_url,
    #   aishub_usernames (list[str]) OR aishub_username (legacy str)
    updates: Dict[str, Any] = {"sources": {}}
    af: Dict[str, Any] = {}
    if "aisfriends_token" in data:
        af["token"] = (data["aisfriends_token"] or "").strip()
    if "aisfriends_backend" in data:
        b = (data["aisfriends_backend"] or "").strip() or "flaresolverr"
        if b not in ("flaresolverr", "curl_cffi", "cloudscraper", "requests"):
            b = "flaresolverr"
        af["backend"] = b
    if "aisfriends_flaresolverr_url" in data:
        af["flaresolverr_url"] = (data["aisfriends_flaresolverr_url"] or "").strip() or "http://localhost:8191"
    if af:
        updates["sources"]["aisfriends"] = af
    # AISHub: prefer the new list shape, fall back to the single-string
    # field for back-compat with old clients. Both end up writing the
    # canonical `usernames` list. Empty rows are filtered out.
    if "aishub_usernames" in data or "aishub_username" in data:
        raw = data.get("aishub_usernames")
        if raw is None:
            raw = [data.get("aishub_username", "")]
        if not isinstance(raw, list):
            raw = [raw]
        cleaned = []
        seen = set()
        for u in raw:
            s = (u or "").strip() if isinstance(u, str) else ""
            if s and s not in seen:
                seen.add(s)
                cleaned.append(s)
        # Preserve at least one (possibly empty) entry so the UI doesn't
        # come back with zero rows after a save-then-reload cycle.
        if not cleaned:
            cleaned = [""]
        updates["sources"]["aishub"] = {"usernames": cleaned}

    # Kpler ------------------------------------------------------------
    kp: Dict[str, Any] = {}
    if "kpler_credential" in data:
        kp["credential"] = (data["kpler_credential"] or "").strip()
    if "kpler_token_url" in data:
        kp["token_url"]  = (data["kpler_token_url"] or "").strip() or "https://auth.kpler.com/oauth/token"
    if "kpler_audience" in data:
        kp["audience"]   = (data["kpler_audience"] or "").strip() or "https://api.kpler.com"
    if "kpler_api_url" in data:
        kp["api_url"]    = (data["kpler_api_url"] or "").strip() or "https://api.sml.kpler.com/graphql"
    if "kpler_flavour" in data:
        f = (data["kpler_flavour"] or "").strip().lower() or "graphql"
        if f not in ("graphql", "messages"):
            f = "graphql"
        kp["flavour"] = f
    if kp:
        updates["sources"]["kpler"] = kp

    cfgmod.update(updates)
    # Tear down any cached AIS Friends sessions + Kpler tokens so the new
    # backend/token/credential is picked up on the next poll / Test click.
    try:
        from . import sources as src_mod
        src_mod.reset_af_session()
        if hasattr(src_mod, "reset_kpler_cache"):
            src_mod.reset_kpler_cache()
    except Exception:
        pass
    WORKER.reload_config()
    return jsonify({"ok": True})


def _classify_aisfriends_error(exc: BaseException, backend: str, fs_url: str) -> str:
    """Turn a poll_aisfriends exception into a human-actionable message."""
    import requests as _r
    # FlareSolverr-specific errors
    from .sources import FlareSolverrError
    if isinstance(exc, FlareSolverrError):
        return str(exc)
    # requests.HTTPError carries the response's status code
    if isinstance(exc, _r.HTTPError) and exc.response is not None:
        code = exc.response.status_code
        if code == 401:
            return ("HTTP 401 Unauthorized - the AIS Friends token is missing or "
                    "expired. Regenerate it at aisfriends.com -> Account -> "
                    "Details -> API Tokens, paste it above and Save.")
        if code == 403:
            if backend != "flaresolverr":
                return (f"HTTP 403 - Cloudflare blocked the direct request "
                        f"(backend = '{backend}'). Switch backend to "
                        f"'FlareSolverr' and make sure the container is "
                        f"running ({fs_url}).")
            return ("HTTP 403 - Cloudflare blocked the request even via "
                    "FlareSolverr. Try `docker pull "
                    "ghcr.io/flaresolverr/flaresolverr:latest` then "
                    "`docker restart flaresolverr` (the bundled Chromium may "
                    "need updating), or test from a different network.")
        if code == 404:
            return ("HTTP 404 - endpoint not found. The AIS Friends API URL "
                    "may have changed; check https://www.aisfriends.com/docs/api/v1")
        if code == 429:
            return ("HTTP 429 - rate-limited. AIS Friends allows 1 request "
                    "per minute. Wait a minute and try again.")
        return f"HTTP {code} from AIS Friends: {exc.response.text[:200]}"
    # Plain ConnectionError - typically the FlareSolverr container being down
    if isinstance(exc, _r.exceptions.ConnectionError):
        if backend == "flaresolverr":
            return (f"Could not reach FlareSolverr at {fs_url}. Start it "
                    f"with: docker start flaresolverr  (or run the one-line "
                    f"`docker run -d --name flaresolverr ...` from the README "
                    f"if it isn't created yet).")
        return f"Network error reaching aisfriends.com: {exc}"
    return str(exc)


def _classify_kpler_error(exc: BaseException) -> str:
    """Turn a poll_kpler exception into a human-actionable message."""
    msg = str(exc)
    low = msg.lower()
    # Auth0 client-grant missing - very common, exact text from Auth0.
    if "client-grant" in low or "not authorized to access resource server" in low:
        return (
            "Kpler rejected the credentials: the client_id exists but no "
            "API grant is attached to it yet. Open a ticket via "
            "developers.kpler.com (or your account manager) asking them to "
            "attach the client-grant for the product you subscribed to. "
            "Until they do, no audience will mint a token."
        )
    if "service not enabled within domain" in low:
        return (
            "Kpler rejected the configured audience as unknown. The audience "
            "string in the Credentials page does not match any of Kpler's "
            "registered API audiences. The two publicly known ones are "
            "`https://api.kpler.com` and `https://terminal.kpler.com`; if "
            "neither works your plan may need a product-specific audience - "
            "ask Kpler support."
        )
    if "invalid_client" in low or "unauthorized" in low and "client" in low:
        return ("Kpler returned `invalid_client` - the client_id or "
                "client_secret is wrong. Regenerate the key in "
                "developers.kpler.com/my-api-keys and paste the new value.")
    if "401" in msg and "access token rejected" in low:
        return ("HTTP 401 from the Kpler API even though we minted a token. "
                "The audience minted a token for a different product than the "
                "API URL you set. Either change the API URL to match the "
                "product the audience grants, or change the audience.")
    if "could not reach kpler" in low:
        return msg  # already self-explanatory
    return msg


@app.route("/api/test-source/<source>", methods=["POST"])
@auth_mod.login_required
def api_test_source(source: str):
    """Smoke-test a single source with the saved credentials + bbox."""
    from . import sources as src_mod
    cfg = cfgmod.load()
    bbox = cfg["bbox"]
    afcfg = cfg["sources"].get("aisfriends", {})
    backend = afcfg.get("backend", "flaresolverr")
    fs_url  = afcfg.get("flaresolverr_url", "http://localhost:8191")
    try:
        if source == "aisfriends":
            # Always reset the session so we test with the just-saved settings.
            try:
                src_mod.reset_af_session()
            except Exception:
                pass
            v = src_mod.poll_aisfriends(
                bbox,
                afcfg.get("token", ""),
                backend=backend,
                flaresolverr_url=fs_url,
            )
        elif source == "aishub":
            # Smoke-test with the first non-empty username. The optional
            # per-key variant /api/test-source/aishub/<index> below targets
            # a specific row.
            usernames = cfgmod.aishub_usernames(cfg)
            if not usernames:
                return jsonify({"ok": False, "error": "no AISHub username configured"}), 200
            v = src_mod.poll_aishub(bbox, usernames[0])
        elif source == "kpler":
            kpcfg = cfg["sources"].get("kpler", {})
            try:
                src_mod.reset_kpler_cache()
            except Exception:
                pass
            v = src_mod.poll_kpler(
                bbox,
                kpcfg.get("credential", ""),
                api_url=kpcfg.get("api_url",   src_mod.KPLER_DEFAULT_API_URL),
                token_url=kpcfg.get("token_url", src_mod.KPLER_DEFAULT_TOKEN_URL),
                audience=kpcfg.get("audience",   src_mod.KPLER_DEFAULT_AUDIENCE),
                flavour=kpcfg.get("flavour",     src_mod.KPLER_DEFAULT_FLAVOUR),
            )
        else:
            return jsonify({"ok": False, "error": "unknown source"}), 400
        return jsonify({"ok": True, "vessels": len(v), "backend": backend if source == "aisfriends" else None})
    except Exception as exc:
        if source == "aisfriends":
            err = _classify_aisfriends_error(exc, backend, fs_url)
        elif source == "kpler":
            err = _classify_kpler_error(exc)
        else:
            err = str(exc)
        return jsonify({"ok": False, "error": err, "backend": backend if source == "aisfriends" else None}), 200


@app.route("/api/test-source/aishub/<int:index>", methods=["POST"])
@auth_mod.login_required
def api_test_aishub_key(index: int):
    """Smoke-test a specific AISHub username (0-based index into usernames[]).

    Used by the per-row Test button on the Credentials page so the user can
    verify each key independently. Returns the same JSON envelope as the
    main test endpoint plus the masked username we hit.
    """
    from . import sources as src_mod
    cfg = cfgmod.load()
    usernames = cfgmod.aishub_usernames(cfg)
    if index < 0 or index >= len(usernames):
        return jsonify({"ok": False, "error": f"no AISHub key at index {index}"}), 200
    user = usernames[index]
    # Mirror the worker's masking helper so the UI shows the same value as
    # the per-key status panel.
    masked = (user[:3] + "…" + user[-2:]) if len(user) > 4 else "*" * len(user)
    try:
        v = src_mod.poll_aishub(cfg["bbox"], user)
        return jsonify({"ok": True, "vessels": len(v), "key": masked})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc), "key": masked}), 200


# ---------------------------------------------------------------------------
# API: Wi-Fi (NetworkManager via nmcli)
# ---------------------------------------------------------------------------
#
# All wrapped in try/except so the page degrades gracefully on hosts without
# NetworkManager rather than throwing 500s. The frontend disables the
# controls when nmcli_available is False.

@app.route("/api/wifi/status")
@auth_mod.login_required
def api_wifi_status():
    from . import wifi as wifi_mod
    try:
        return jsonify(wifi_mod.status())
    except wifi_mod.WifiError as exc:
        return jsonify({"interfaces": [], "hostname": "", "nmcli_available": False, "error": str(exc)})


@app.route("/api/wifi/scan")
@auth_mod.login_required
def api_wifi_scan():
    from . import wifi as wifi_mod
    rescan = request.args.get("rescan", "1") != "0"
    try:
        return jsonify({"ok": True, "networks": wifi_mod.scan(rescan=rescan)})
    except wifi_mod.WifiError as exc:
        return jsonify({"ok": False, "error": str(exc), "networks": []})


@app.route("/api/wifi/saved")
@auth_mod.login_required
def api_wifi_saved():
    from . import wifi as wifi_mod
    try:
        return jsonify({"ok": True, "profiles": wifi_mod.saved()})
    except wifi_mod.WifiError as exc:
        return jsonify({"ok": False, "error": str(exc), "profiles": []})


@app.route("/api/wifi/connect", methods=["POST"])
@auth_mod.login_required
def api_wifi_connect():
    from . import wifi as wifi_mod
    data = request.get_json(silent=True) or {}
    ssid     = (data.get("ssid") or "").strip()
    password = data.get("password") or None
    hidden   = bool(data.get("hidden"))
    try:
        return jsonify(wifi_mod.connect(ssid, password=password, hidden=hidden))
    except wifi_mod.WifiError as exc:
        return jsonify({"ok": False, "error": str(exc)})


@app.route("/api/wifi/forget", methods=["POST"])
@auth_mod.login_required
def api_wifi_forget():
    from . import wifi as wifi_mod
    data = request.get_json(silent=True) or {}
    target = (data.get("ssid") or data.get("name") or "").strip()
    try:
        return jsonify(wifi_mod.forget(target))
    except wifi_mod.WifiError as exc:
        return jsonify({"ok": False, "error": str(exc)})


def run(host: str, port: int, debug: bool = False) -> None:
    # Flask >= 3 - use_reloader must be False or worker thread duplicates
    app.run(host=host, port=port, debug=debug, use_reloader=False, threaded=True)
