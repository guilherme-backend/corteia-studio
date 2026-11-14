"""Páginas navegáveis do CorteIA Studio.

Cada página é um ``QWidget`` com construtor ``__init__(ctx, parent=None)`` e um
método ``refresh()`` obrigatório. O reexporte é feito por ``__getattr__`` para
que uma página ainda inexistente não impeça as demais de carregar.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any

__all__ = [
    "DashboardPage",
    "ImportPage",
    "ClipsPage",
    "EditorPage",
    "ExportPage",
    "SettingsPage",
]

_EXPORTS = {
    "DashboardPage": ".dashboard",
    "ImportPage": ".import_page",
    "ClipsPage": ".clips_page",
    "EditorPage": ".editor_page",
    "ExportPage": ".export_page",
    "SettingsPage": ".settings_page",
}


def __getattr__(name: str) -> Any:
    module = _EXPORTS.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return getattr(import_module(module, __name__), name)


def __dir__() -> list[str]:
    return sorted(__all__)
