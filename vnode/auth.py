"""Session-based authentication for the Virtual AIS Node web UI.

Mirrors the same conventions as the sister AIS-Server project so the two
apps feel identical to operate:

  * Default credentials: ``JLBMaritime`` / ``Admin``.
  * The default password is *forced-change-on-first-login* – the user
    cannot reach any other page until they have set a real password.
  * Passwords are hashed with ``werkzeug.security`` (PBKDF2-SHA256, salted)
    – no extra runtime dependency, ``werkzeug`` is already pulled in by
    Flask.
  * Credentials live in ``auth.json`` next to ``config.json`` – kept out
    of ``config.json`` itself so a casual backup of the config file
    cannot leak the hash.
  * Flask's signing key lives in ``secret_key`` next to ``auth.json``,
    auto-generated on first run and written atomically so a power cut
    can't half-write it.

Lock-out / reset
----------------
If the user forgets their password the recovery is:

    1. Stop the service.
    2. Delete ``auth.json`` from the data directory.
    3. Start the service – it recreates the file with the default
       ``JLBMaritime`` / ``Admin`` credentials and re-arms the
       forced-change-on-first-login flag.

This is exactly how the AIS-Server project handles it, and matches what
the README documents.
"""
from __future__ import annotations

import json
import logging
import os
import secrets
import tempfile
import threading
from functools import wraps
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from flask import (g, jsonify, redirect, request, session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

from ._paths import app_root

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
AUTH_PATH       = app_root() / "auth.json"
SECRET_KEY_PATH = app_root() / "secret_key"

DEFAULT_USERNAME = "JLBMaritime"
DEFAULT_PASSWORD = "Admin"

_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# Atomic writes – avoid half-written files if the box loses power mid-save.
# ---------------------------------------------------------------------------
def _atomic_write(path: Path, data: str, mode: int = 0o600) -> None:
    """Write ``data`` to ``path`` atomically (write-temp-then-rename).

    Permissions are best-effort: ``os.chmod`` is a no-op on Windows but
    still useful on Linux/macOS where the data dir might be shared.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        try:
            os.chmod(tmp, mode)
        except OSError:
            pass
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# Secret key (Flask session signing)
# ---------------------------------------------------------------------------
def load_secret_key() -> str:
    """Return the Flask session secret, creating it on first call.

    256 bits of randomness from :mod:`secrets`, hex-encoded so the file
    is human-inspectable.  We deliberately *do not* fall back to a
    constant default – losing the key just signs everyone out, which is
    the safe failure mode.
    """
    if SECRET_KEY_PATH.exists():
        try:
            text = SECRET_KEY_PATH.read_text(encoding="utf-8").strip()
            if text:
                return text
        except OSError as exc:
            log.warning("Could not read %s (%s) – regenerating.",
                        SECRET_KEY_PATH, exc)
    key = secrets.token_hex(32)
    _atomic_write(SECRET_KEY_PATH, key, mode=0o600)
    log.info("Generated new Flask secret_key at %s", SECRET_KEY_PATH)
    return key


# ---------------------------------------------------------------------------
# Credentials store
# ---------------------------------------------------------------------------
def _default_record() -> Dict[str, Any]:
    return {
        "username":             DEFAULT_USERNAME,
        "password_hash":        generate_password_hash(DEFAULT_PASSWORD),
        "must_change_password": True,
    }


def _read() -> Dict[str, Any]:
    """Read ``auth.json`` from disk, falling back to fresh defaults.

    A missing or corrupt file is rewritten with the default record so
    the user can always get back in by deleting the file.
    """
    if AUTH_PATH.exists():
        try:
            return json.loads(AUTH_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("auth.json unreadable (%s) – recreating defaults.", exc)
    rec = _default_record()
    _write(rec)
    return rec


def _write(rec: Dict[str, Any]) -> None:
    _atomic_write(AUTH_PATH, json.dumps(rec, indent=2), mode=0o600)


def load_user() -> Dict[str, Any]:
    """Public read accessor – never returns the hash."""
    with _LOCK:
        rec = _read()
    return {
        "username":             rec.get("username", DEFAULT_USERNAME),
        "must_change_password": bool(rec.get("must_change_password", False)),
    }


def verify(username: str, password: str) -> bool:
    """Constant-time check of credentials."""
    with _LOCK:
        rec = _read()
    # Compare usernames case-insensitively so phone keyboards that
    # auto-capitalise the first letter don't lock the user out.
    if (username or "").strip().lower() != str(rec.get("username", "")).lower():
        # Still run check_password_hash against a dummy hash so the
        # response time doesn't leak whether the username existed.
        check_password_hash(
            generate_password_hash("dummy"), password or "")
        return False
    return check_password_hash(rec.get("password_hash", ""), password or "")


def set_password(new_password: str, *, clear_must_change: bool = True) -> None:
    """Hash and persist a new password.  Caller validates length etc."""
    with _LOCK:
        rec = _read()
        rec["password_hash"] = generate_password_hash(new_password)
        if clear_must_change:
            rec["must_change_password"] = False
        _write(rec)


# ---------------------------------------------------------------------------
# Flask integration
# ---------------------------------------------------------------------------
SESSION_KEY = "user"  # the username currently signed in


def current_user() -> Optional[str]:
    return session.get(SESSION_KEY)


def _wants_json() -> bool:
    """True when the request looks like an XHR/JSON call rather than a
    page load.  We want API callers to get a JSON 401 instead of an HTML
    redirect they can't follow."""
    if request.path.startswith("/api/"):
        return True
    accept = request.headers.get("Accept", "")
    return "application/json" in accept and "text/html" not in accept


def login_required(view: Callable) -> Callable:
    """Decorator: require an authenticated session for ``view``.

    Behaviour:
      * No session → JSON 401 for API routes, redirect to /login for pages.
      * Session present but the user still has ``must_change_password``
        set → redirect (or 403 JSON) to /change-password.  The forced
        change page itself is excluded so the user can actually reach it.
    """
    @wraps(view)
    def wrapped(*args, **kwargs):
        user = current_user()
        if not user:
            if _wants_json():
                return jsonify({"ok": False, "error": "authentication required"}), 401
            return redirect(url_for("login", next=request.path))

        # Force-change short-circuit: don't let the user touch anything
        # else until they have a real password set.
        if session.get("must_change_password"):
            # The forced-change page and its POST handler are allowed
            # through; logout too so the user isn't stuck.
            allowed = {"change_password_page", "logout"}
            if request.endpoint not in allowed:
                if _wants_json():
                    return jsonify({"ok": False,
                                    "error": "password change required"}), 403
                return redirect(url_for("change_password_page"))

        g.user = user
        return view(*args, **kwargs)
    return wrapped


def login_user(username: str) -> None:
    """Mark the session as signed in as ``username`` and refresh the
    must-change flag from disk so a stale True doesn't survive a reset."""
    session.clear()
    session.permanent = True
    session[SESSION_KEY] = username
    session["must_change_password"] = load_user()["must_change_password"]


def logout_user() -> None:
    session.clear()
