"""Filesystem layout, frozen-build aware resource paths and temp management.

Windows:  %APPDATA%\\CorteIA Studio   (config, projects, models, cache)
          %LOCALAPPDATA%\\CorteIA Studio\\Temp  (transient render files)
Linux:    ~/.config/CorteIA Studio, XDG cache.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

APP_NAME = "CorteIA Studio"
APP_ID = "CorteIA Studio"
APP_SLUG = "corteia-studio"
ORG = "CorteIA"
VERSION = "1.0.0"


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def bundle_root() -> Path:
    """Directory holding files shipped with the executable."""
    if is_frozen():
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).resolve().parent


def resources_dir() -> Path:
    """``corteia/resources`` both in source tree and in a PyInstaller bundle."""
    cand = bundle_root() / "corteia" / "resources"
    if not cand.exists():
        cand = bundle_root() / "resources"
    return cand


def models_dir() -> Path:
    """Writable directory for downloaded Whisper models.

    These are multi-hundred-megabyte assets, so the default home is the cache
    directory instead of the configuration directory: a settings backup should
    stay small, and "restore my preferences" must never mean re-downloading
    3 GB of weights.  An install that already keeps them next to the config is
    detected and preferred, so nothing is downloaded twice.
    """
    legacy = data_dir() / "models"
    try:
        if legacy.is_dir() and any(child.is_dir() for child in legacy.iterdir()):
            return legacy
    except OSError:
        pass
    p = cache_dir() / "models"
    p.mkdir(parents=True, exist_ok=True)
    return p


def bundled_model(name: str) -> Path:
    """Path of a model shipped with the app, falling back to the writable dir."""
    for base in (resources_dir() / "models", models_dir()):
        p = base / name
        if p.exists():
            return p
    return resources_dir() / "models" / name


def data_dir() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
        p = base / APP_NAME
    elif sys.platform == "darwin":
        p = Path.home() / "Library" / "Application Support" / APP_NAME
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
        p = base / APP_SLUG
    p.mkdir(parents=True, exist_ok=True)
    return p


def cache_dir() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        p = base / APP_NAME / "Cache"
    elif sys.platform == "darwin":
        p = Path.home() / "Library" / "Caches" / APP_NAME
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
        p = base / APP_SLUG
    p.mkdir(parents=True, exist_ok=True)
    return p


def projects_dir() -> Path:
    p = data_dir() / "projects"
    p.mkdir(parents=True, exist_ok=True)
    return p


def logs_dir() -> Path:
    p = data_dir() / "logs"
    p.mkdir(parents=True, exist_ok=True)
    return p


def exports_dir_default() -> Path:
    p = Path.home() / "Videos" / "CorteIA"
    p.mkdir(parents=True, exist_ok=True)
    return p


def temp_root() -> Path:
    """Root for transient files. Controlled: everything goes here, nothing beside it."""
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        p = base / APP_NAME / "Temp"
    else:
        p = Path(tempfile.gettempdir()) / APP_SLUG
    p.mkdir(parents=True, exist_ok=True)
    return p


def temp_sub(dirname: str) -> Path:
    p = temp_root() / dirname
    p.mkdir(parents=True, exist_ok=True)
    return p


def temp_file(suffix: str = "", subdir: str = "misc") -> Path:
    d = temp_sub(subdir)
    fd, path = tempfile.mkstemp(suffix=suffix, dir=d)
    os.close(fd)
    return Path(path)


def free_bytes_temp() -> int:
    try:
        return shutil.disk_usage(temp_root()).free
    except OSError:
        return 0


def dir_size(p: Path) -> int:
    total = 0
    if not p.exists():
        return 0
    for root, _dirs, files in os.walk(p):
        for f in files:
            try:
                total += (Path(root) / f).stat().st_size
            except OSError:
                pass
    return total


def temp_usage_bytes() -> int:
    return dir_size(temp_root())


def projects_usage_bytes() -> int:
    return dir_size(data_dir())


def clean_temp(older_than_seconds: float = 0.0, keep_dirs: bool = True) -> int:
    """Delete transient files. Returns bytes freed. Files still in use by a
    running render are skipped silently."""
    freed = 0
    root = temp_root()
    if not root.exists():
        return 0
    now = time.time()
    for path in sorted(root.rglob("*"), key=lambda x: len(str(x)), reverse=True):
        try:
            if path.is_dir():
                if not keep_dirs and path.exists() and not any(path.iterdir()):
                    path.rmdir()
                continue
            if older_than_seconds and (now - path.stat().st_mtime) < older_than_seconds:
                continue
            size = path.stat().st_size
            path.unlink()
            freed += size
        except OSError:
            continue
    return freed


def human_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024.0:
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} {unit}"
        n /= 1024.0
    return f"{n:.1f} PB"
