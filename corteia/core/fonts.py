"""Which font names actually exist on this machine.

libass silently substitutes a missing font, and a "viral" caption in a serif
fallback looks broken.  The UI and the ASS writer therefore ask this module
first, and the settings page shows the honest list.

Detection is real: ``fc-list`` on Linux, the Windows font registry on Windows,
and ``system_profiler``/fontconfig on macOS.  Results are cached.
"""

from __future__ import annotations

import functools
import logging
import os
import shutil
import re
import subprocess
import sys
from pathlib import Path

log = logging.getLogger("corteia.fonts")

#: preferred → acceptable substitutes, in order.  Every name is a real family
#: that ships with the respective OS (or with the app's own bundle).
SUBSTITUTES: dict[str, list[str]] = {
    "Montserrat": ["Montserrat", "Inter", "Segoe UI", "Roboto", "Noto Sans",
                   "DejaVu Sans", "Arial"],
    "Inter": ["Inter", "Segoe UI", "Roboto", "Noto Sans", "DejaVu Sans", "Arial"],
    "Arial Black": ["Arial Black", "Arial", "Liberation Sans Narrow", "DejaVu Sans"],
    "Arial": ["Arial", "Liberation Sans", "Helvetica", "DejaVu Sans", "Segoe UI"],
    "Roboto": ["Roboto", "Noto Sans", "Segoe UI", "DejaVu Sans", "Arial"],
    "Impact": ["Impact", "Anton", "Bebas Neue", "Arial Black", "DejaVu Sans"],
    "Bebas Neue": ["Bebas Neue", "Impact", "Anton", "Arial Black", "DejaVu Sans"],
    "Poppins": ["Poppins", "Montserrat", "Inter", "Segoe UI", "DejaVu Sans"],
    "Segoe UI": ["Segoe UI", "Noto Sans", "DejaVu Sans", "Arial", "Helvetica Neue"],
}

#: fonts that read well as captions even on a bare system
SAFE_FALLBACKS = ["Segoe UI", "Arial", "Roboto", "Noto Sans", "DejaVu Sans",
                  "Liberation Sans", "Helvetica Neue", "Verdana", "Tahoma"]

_BOLD_HINT = re.compile(r"(black|bold|heavy|extra|semibold)", re.I)


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


@functools.lru_cache(maxsize=1)
def installed() -> list[str]:
    """Sorted list of installed font family names (may be empty in odd setups)."""
    names: set[str] = set()
    if sys.platform == "win32":
        names.update(_win_fonts())
    elif sys.platform == "darwin":
        names.update(_fc_list())
        if not names:
            names.update(_mac_fonts())
    else:
        names.update(_fc_list())
    if not names:
        names.update(_bundled_fonts())
    return sorted(names)


def _fc_list() -> set[str]:
    try:
        out = subprocess.run(["fc-list", ":", "family"], capture_output=True,
                             text=True, timeout=15).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug("fc-list indisponível: %s", exc)
        return set()
    fams: set[str] = set()
    for line in out.splitlines():
        for part in line.split(","):
            part = part.strip()
            if part and not part.startswith(":"):
                fams.update(p.strip() for p in part.split(";") if p.strip())
    return fams


def _win_fonts() -> set[str]:
    try:
        import winreg
    except ImportError:
        return set()
    fams: set[str] = set()
    roots = (
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts"),
        (winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Fonts"),
    )
    for hive, key in roots:
        try:
            with winreg.OpenKey(hive, key) as hk:
                for i in range(winreg.QueryInfoKey(hk)[1]):
                    label = winreg.EnumValue(hk, i)[0]
                    fams.add(re.sub(r"\s*\((Bold|Italic|Regular)\)\s*", "", label).strip())
        except OSError:
            continue
    return fams


def _mac_fonts() -> set[str]:
    try:
        out = subprocess.run(["system_profiler", "SPFontsDataType"], capture_output=True,
                             text=True, timeout=25).stdout
    except (OSError, subprocess.SubprocessError):
        return set()
    return {m.strip() for m in re.findall(r"^\s+Family:\s*(.+)$", out, re.M)}


def _bundled_fonts() -> set[str]:
    """Fonts shipped inside the app bundle (``resources/fonts``)."""
    from .. import paths
    out: set[str] = set()
    d = paths.resources_dir() / "fonts"
    if d.exists():
        for f in d.iterdir():
            if f.suffix.lower() in (".ttf", ".otf", ".woff2"):
                out.add(f.stem.replace("_", " "))
    return out


def has(name: str) -> bool:
    n = _norm(name)
    if not n:
        return False
    return any(_norm(x) == n or n in _norm(x) for x in installed())


def pick(preferred: str, *, extra: list[str] | None = None) -> tuple[str, bool]:
    """Return ``(usable_font, substituted)`` — never a name that does not exist.

    When nothing at all can be resolved, ``DejaVu Sans``/``Arial`` is returned
    with ``substituted=True`` and the UI tells the user the caption font fell
    back instead of pretending the chosen font was used.
    """
    pool = [preferred, *(extra or []), *SUBSTITUTES.get(preferred, []), *SAFE_FALLBACKS]
    for cand in pool:
        if cand and has(cand):
            return cand, _norm(cand) != _norm(preferred)
    fallback = "DejaVu Sans" if sys.platform != "win32" else "Arial"
    return fallback, True


def bold_variants(name: str) -> list[str]:
    return [f for f in installed() if _norm(name) in _norm(f) and _BOLD_HINT.search(f)]


def _font_dirs() -> list[Path]:
    """Directories that may contain font files on this OS."""
    from .. import paths
    dirs = [paths.resources_dir() / "fonts"]
    if sys.platform == "win32":
        windir = os.environ.get("WINDIR", r"C:\Windows")
        dirs.append(Path(windir) / "Fonts")
    else:
        dirs += [Path("/usr/share/fonts"), Path("/usr/local/share/fonts"),
                 Path.home() / ".fonts", Path.home() / ".local/share/fonts"]
    return [d for d in dirs if d.exists()]


def _fc_file(name: str) -> str:
    """Ask fontconfig for a file: it resolves families we cannot guess by path."""
    if not shutil.which("fc-match"):
        return ""
    args = ["fc-match", "%{file}"] + ([str(name)] if name else [])
    try:
        out = subprocess.run(args, capture_output=True, text=True, timeout=8).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""
    return out if out and Path(out).is_file() else ""


_FILE_INDEX: dict[str, list[str]] | None = None
_EXT = (".ttf", ".otf", ".ttc", ".woff")

# fc-list happily reports every face of a family under the same family name, so
# a naive lookup returns an italic or an extra-light file.  Faces are ranked
# instead: never italic/oblique, and the requested weight wins.
def _face_rank(path: str, *, bold: bool) -> int:
    stem = Path(path).stem.lower()
    score = 0
    if "italic" in stem or "oblique" in stem:
        score -= 20
    if bold:
        score += 20 if ("black" in stem or "heavy" in stem or "extra-bold" in stem) else \
            16 if "bold" in stem else 10 if "medium" in stem else 6 if "semibold" in stem else 0
    else:
        score += 16 if "regular" in stem else 13 if "medium" in stem else 8 if "bold" in stem else 4
    if "condensed" in stem or "narrow" in stem or "semiexpanded" in stem:
        score -= 6
    if "thin" in stem or "extralight" in stem or ("light" in stem and "black" not in stem):
        score -= 8
    if "variable" in stem or "vf-" in stem:
        score += 2
    return score


def file_index() -> dict[str, list[str]]:
    """``{normalised family: [font files]}`` built from the real system inventory.

    Guessing paths fails (``LiberationSans-Regular.ttf`` sits three directories
    deep) and a single-file map returns whatever face fc-list happened to print
    first, so every face of a family is kept and ranked at lookup time.
    """
    global _FILE_INDEX
    if _FILE_INDEX is not None:
        return _FILE_INDEX
    idx: dict[str, list[str]] = {}

    def add(fam: str, path: str) -> None:
        key = _norm(fam)
        if key:
            bucket = idx.setdefault(key, [])
            if path not in bucket:
                bucket.append(path)

    if shutil.which("fc-list"):
        try:
            out = subprocess.run(["fc-list", ":", "file", "family"],
                                 capture_output=True, text=True, timeout=30).stdout
        except (OSError, subprocess.SubprocessError):
            out = ""
        for line in out.splitlines():
            path, _, fams = line.partition(":")
            path = path.strip()
            if not path or not path.lower().endswith(_EXT) or not Path(path).is_file():
                continue
            add(Path(path).stem, path)
            for fam in fams.split(","):
                fam = fam.strip()
                if not fam:
                    continue
                add(fam, path)
                low = fam.lower()
                for style in (" bold", " black", " heavy", " italic", " regular", " medium"):
                    if style in low:
                        add(low.replace(style, ""), path)
    for d in _font_dirs():
        try:
            for f in d.rglob("*"):
                if f.suffix.lower() in _EXT and f.is_file():
                    add(f.stem.replace("_", " "), str(f))
        except OSError:
            continue
    _FILE_INDEX = idx
    return idx


def _lookup(name: str) -> list[str]:
    idx = file_index()
    key = _norm(name)
    if not key:
        return []
    if key in idx:
        return idx[key]
    if len(key) >= 3:
        hits = [v for k, v in idx.items() if key in k]
        flat = [f for group in hits for f in group]
        if flat:
            return flat
    return []


def file_for(name: str, *, bold: bool = False) -> str:
    """Absolute path of a font file usable by ``drawtext``; ``""`` when unknown."""
    chosen, _ = pick(name)
    for cand in (name, chosen):
        paths = _lookup(cand)
        if paths:
            return max(paths, key=lambda f: _face_rank(f, bold=bold))
    return _fc_file(chosen) or _fc_file("DejaVu Sans") or _fc_file("")


def invalidate() -> None:
    global _FILE_INDEX
    installed.cache_clear()
    _FILE_INDEX = None
