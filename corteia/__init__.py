"""CorteIA Studio — estúdio de cortes automáticos com IA.

Pacote do aplicativo desktop (PySide6).  Camadas, de baixo para cima:

corteia.core
    FFmpeg/ffprobe, Whisper, detecção facial e reenquadramento, legendas ASS,
    análise de engajamento, renderização e fila de exportação.  NAO importa
    PySide6 — pode ser usado por scripts e testes sem interface.
corteia.workers
    Ponte para as threads (JobRunner), o único caminho aceito entre a UI e o
    núcleo.
corteia.ui
    Tema, widgets, páginas e a janela principal.

corteia.context (AppContext) é o único ponto de contato: guarda configuração,
projeto, pipeline e exportador.  Todo texto visível ao usuário é português do
Brasil; ver corteia.i18n.
"""

from __future__ import annotations

__app_name__ = "CorteIA Studio"
__product__ = "corteia-studio"
__version__ = "1.0.0"
__version_info__ = (1, 0, 0)

__all__ = ["__app_name__", "__product__", "__version__", "__version_info__", "main"]


def main(argv=None) -> int:
    """Entry point for run.py, the ``corteia`` console script and PyInstaller."""
    from .main import main as _main
    return _main(argv)
