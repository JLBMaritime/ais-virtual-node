"""Filesystem-path helpers that work both in a normal Python checkout AND
inside a PyInstaller one-file executable.

The two modes diverge in one important way:

  * In a normal checkout, everything lives under the repo root - both the
    code (`vnode/...`) and the runtime-mutable bits (`config.json`,
    `templates/`, `static/`).

  * In a PyInstaller one-file build, the executable extracts a *fresh*
    copy of the bundled code + data into a temp dir on every launch
    (exposed via `sys._MEIPASS`). If we wrote `config.json` there it
    would be wiped on every restart. So:
      - read-only assets (templates, static) come from `sys._MEIPASS`
      - read/write state (`config.json`) lives **next to the .exe**.

Use `bundle_root()` for the former, `app_root()` for the latter.
"""
from __future__ import annotations

import sys
from pathlib import Path


def _is_frozen() -> bool:
    """True iff we're running inside a PyInstaller (or similar) bundle."""
    return bool(getattr(sys, "frozen", False))


def app_root() -> Path:
    """Directory that holds the user's mutable state (config.json, logs, ...).

    - Frozen: next to the executable, so `config.json` survives upgrades
      and reboots and isn't wiped with the PyInstaller temp dir.
    - Dev:   the repo root (two levels up from this file).
    """
    if _is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def bundle_root() -> Path:
    """Directory that holds read-only assets shipped with the app (templates, static).

    - Frozen: `sys._MEIPASS` - the per-launch extraction dir PyInstaller
      sets up for the one-file bootloader.
    - Dev:   same as `app_root()`.
    """
    if _is_frozen():
        # _MEIPASS is set by the PyInstaller bootloader, but only at runtime;
        # `getattr` keeps static analysers happy.
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            return Path(meipass)
    return Path(__file__).resolve().parent.parent
