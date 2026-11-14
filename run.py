#!/usr/bin/env python3
"""CorteIA Studio — lançador.

    python run.py              abre a interface
    python run.py --check      diagnostica FFmpeg, codecs, fontes, GPU, modelos
    python run.py --smoke      roda o pipeline real e exporta um corte (sem GUI)
    python run.py --analyze video.mp4
    python run.py --export video.mp4 12 30

Este arquivo existe para que o programa rode direto da pasta clonada, sem
instalação.  O trabalho real está em corteia/main.py.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# TLS/HTTP local: nenhum tráfego além do que o usuário pedir (API e downloads).
MIN_PYTHON = (3, 11)


def _guard_python() -> int | None:
    if sys.version_info < MIN_PYTHON:
        need = ".".join(map(str, MIN_PYTHON))
        print(f"CorteIA Studio precisa de Python {need} ou mais novo.\n"
              f"Você está usando {sys.version.split()[0]} ({sys.executable}).",
              file=sys.stderr)
        return 4
    return None


def _guard_qt() -> int | None:
    """Say what to install, in Portuguese, instead of an ImportError stack."""
    try:
        import PySide6  # noqa: F401
    except ImportError:
        print("Faltam as dependências. Instale com:\n"
              "  Windows:  install.bat\n"
              "  Linux/macOS:  bash install.sh\n"
              "ou:  python -m pip install -r requirements.txt", file=sys.stderr)
        return 5
    return None


def main() -> int:
    for guard in (_guard_python, _guard_qt):
        code = guard()
        if code is not None:
            return code
    quiet = ("--smoke", "--smoke-ui", "--check", "--analyze", "--export",
             "--version", "-h", "--help")
    if not any(a == q or a.startswith(f"{q}=") or a in q
               for a in sys.argv for q in quiet if a.startswith("-")):
        print(__doc__)
    from corteia.main import main as real_main
    return real_main()


if __name__ == "__main__":
    raise SystemExit(main())
