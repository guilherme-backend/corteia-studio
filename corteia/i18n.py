"""Minimal i18n: Portuguese is the source language, English is a lookup table.

``tr("Gerar cortes com IA")`` returns the string unchanged in pt and the mapped
value in en. Untranslated strings fall back to Portuguese instead of showing a
key, so the UI can never render ``[[missing]]`` boxes.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from . import paths
from .config import get_config

_current = "pt"
_table: dict[str, str] = {}
_loaded = False

# shipped override file, editable without rebuilding: <data dir>/i18n_en.json
_CACHE_KEY = re.compile(r"\{[0-9a-zA-Z_]+\}")


def _load_table() -> None:
    global _table, _loaded
    if _loaded:
        return
    _loaded = True
    for cand in (paths.data_dir() / "i18n_en.json",
                 paths.resources_dir() / "i18n" / "en.json"):
        if Path(cand).exists():
            try:
                data = json.loads(Path(cand).read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    _table.update({str(k): str(v) for k, v in data.items()})
            except (OSError, ValueError):
                pass
            break


def set_language(lang: str) -> None:
    global _current
    _current = "en" if str(lang).lower().startswith("en") else "pt"
    _load_table()


def language() -> str:
    return _current


def tr(text: str, **kwargs) -> str:
    if not text:
        return ""
    _load_table()
    out = _table.get(text, text) if _current == "en" else text
    if kwargs:
        try:
            out = out.format(**kwargs)
        except (KeyError, IndexError, ValueError):
            pass
    return out


def t(text: str, **kwargs) -> str:
    """Shorthand used across the UI."""
    return tr(text, **kwargs)


def from_config() -> None:
    set_language(get_config().str_("ui.language") or "pt")


# convenience for the few places that need plural awareness
def plural(count: int, singular: str, plural_form: str) -> str:
    return singular if count == 1 else plural_form
