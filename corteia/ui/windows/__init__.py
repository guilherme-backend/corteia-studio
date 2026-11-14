"""Janelas do CorteIA Studio.

``MainWindow`` é importada sob demanda para que a camada de janelas não puxe
todo o núcleo (e as páginas em construção) na inicialização do Qt.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any

__all__ = ["MainWindow"]

_EXPORTS = {"MainWindow": ".main_window"}


def __getattr__(name: str) -> Any:
    module = _EXPORTS.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return getattr(import_module(module, __name__), name)


def __dir__() -> list[str]:
    return sorted(__all__)
