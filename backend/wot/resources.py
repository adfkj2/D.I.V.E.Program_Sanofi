"""Where the app keeps its files, both from a checkout and from a frozen build.

A frozen (PyInstaller) process has no repository around it: ``__file__`` lives
inside the unpacked bundle and the working directory belongs to whoever launched
the shortcut. These helpers are the single place that knows the difference.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

APP_DIR_NAME = "WeOnceTalked"


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False)) and hasattr(sys, "_MEIPASS")


def resource_root() -> Path:
    """Directory that holds bundled data: the bundle itself, or the checkout."""
    if is_frozen():
        return Path(getattr(sys, "_MEIPASS"))
    return Path(__file__).resolve().parents[2]


def frontend_dist() -> Path | None:
    """Locate the built SPA, allowing for the name the bundler gives it."""
    root = resource_root()
    for candidate in (root / "frontend_dist", root / "frontend" / "dist", root / "dist"):
        if (candidate / "index.html").is_file():
            return candidate
    return None


def public_portraits() -> Path | None:
    """Portraits are copied into the SPA build; a checkout keeps the originals."""
    root = resource_root()
    built = frontend_dist()
    candidates = [
        root / "frontend" / "public" / "portraits",
        built / "portraits" if built is not None else None,
        root / "portraits",
    ]
    for candidate in candidates:
        if candidate is not None and candidate.is_dir():
            return candidate
    return None


def default_data_dir() -> Path:
    """User data must survive uninstall, so it never sits next to the program."""
    override = os.getenv("WOT_DATA_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    if is_frozen():
        base = os.getenv("LOCALAPPDATA") or os.getenv("APPDATA")
        if base:
            return Path(base) / APP_DIR_NAME / "data"
        return Path.home() / ".weoncetalked" / "data"
    return Path.cwd() / ".local"


def log_dir(base: Path | None = None) -> Path:
    """Logs belong to the data directory so an uninstall keeps the history."""
    return (base if base is not None else default_data_dir()) / "logs"
