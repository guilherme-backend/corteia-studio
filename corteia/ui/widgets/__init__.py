"""Widgets reutilizáveis da interface.

Os módulos são importados sob demanda por ``__getattr__``: um widget que ainda
não exista (ou que falhe ao importar porque depende de um módulo do núcleo em
construção) não impede o pacote de carregar — apenas levanta
``AttributeError``/``ImportError`` quando alguém tenta usá-lo de fato.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any

__all__ = [
    "Sidebar",
    "StatCard",
    "ProjectCard",
    "ClipCard",
    "HistoryRow",
    "EmptyState",
    "DropArea",
    "StageProgress",
    "MiniBar",
    "ToastStack",
    "SwitchButton",
    "SegmentedControl",
    "BusyOverlay",
    "run_job",
    "get_runner",
]

#: nome público -> módulo relativo dentro de ``corteia.ui.widgets``
_EXPORTS = {
    "Sidebar": ".sidebar",
    "StatCard": ".cards",
    "ProjectCard": ".cards",
    "ClipCard": ".cards",
    "HistoryRow": ".cards",
    "EmptyState": ".cards",
    "DropArea": ".droparea",
    "StageProgress": ".progress",
    "MiniBar": ".progress",
    "ToastStack": ".toast",
    "SwitchButton": ".switch",
    "SegmentedControl": ".segmented",
    "BusyOverlay": ".busy",
    "run_job": ".busy",
    "get_runner": ".busy",
}


def __getattr__(name: str) -> Any:
    module = _EXPORTS.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return getattr(import_module(module, __name__), name)


def __dir__() -> list[str]:
    return sorted(__all__)
