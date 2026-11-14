"""Bootstrap da interface: QApplication, tema, idioma, contexto e janela principal.

Chamado por ``run.py`` → ``corteia.main.gui()``.  Também roda direto::

    python -m corteia.ui.app            # abre a janela
    python -m corteia.ui.app --smoke    # abre, gira o event loop e sai com 0

``--smoke`` constrói a janela de verdade (layout real, páginas reais, jobs de
aquecimento reais) em ``QT_QPA_PLATFORM=offscreen``, imprime o resumo e fecha
sem pedir interação a ninguém — é assim que a UI é testada em CI e em máquina
sem display.

Nota de compatibilidade: esta camada usa ``corteia.i18n.set_language`` (o nome
real do módulo). A versão antiga importava ``set_lang``, que não existe, e por
isso o app não abria.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any

from .. import __app_name__, __version__, paths
from ..config import get_config
from ..context import AppContext
from ..i18n import set_language, t

log = logging.getLogger("corteia.ui")

ORG_DOMAIN = "corteia.app"


# --------------------------------------------------------------------------- #
# peças do bootstrap
# --------------------------------------------------------------------------- #
def ensure_app_dirs() -> None:
    """Garante dados/cache/projetos/logs/modelos/temp antes de qualquer escrita.

    ``corteia.paths`` já cria cada pasta quando chamada; aqui só reunimos as
    chamadas e traduzimos falha de permissão em aviso, não em exceção.
    """
    for getter in (paths.data_dir, paths.cache_dir, paths.projects_dir,
                   paths.logs_dir, paths.models_dir, paths.temp_root):
        try:
            getter()
        except OSError as exc:
            log.warning("pasta indisponível (%s): %s", getter.__name__, exc)


def _apply_fonts(app: Any) -> None:
    """Registra as fontes do pacote, para a UI bater com a legenda queimada."""
    from PySide6.QtGui import QFontDatabase

    fonts_dir = paths.resources_dir() / "fonts"
    if fonts_dir.is_dir():
        for f in sorted(fonts_dir.glob("*.[ot]tf")):
            QFontDatabase.addApplicationFont(str(f))


def _qt_available() -> bool:
    try:
        import PySide6.QtWidgets  # noqa: F401

        return True
    except ImportError:
        return False


def app_icon() -> Any:
    """Ícone real (``assets/icon.png``); se não existir, ícone vetorial do tema."""
    from PySide6.QtGui import QIcon

    candidates = [
        paths.bundle_root().parent / "assets" / "icon.png",
        paths.bundle_root() / "assets" / "icon.png",
        Path.cwd() / "assets" / "icon.png",
    ]
    for cand in candidates:
        try:
            if cand.is_file():
                icon = QIcon(str(cand))
                if not icon.isNull():
                    return icon
        except OSError:
            continue
    from . import theme

    return theme.icon("scissors", "accent", 32)


def build_application(argv: list[str] | None = None) -> Any:
    """Cria (ou reaproveita) o ``QApplication`` com identidade do produto."""
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication

    args = list(sys.argv[:1] + (argv or []))
    try:
        QApplication.setHighDpiScaleFactorRoundingPolicy(
            Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    except (AttributeError, RuntimeError):
        pass
    app = QApplication.instance() or QApplication(args)
    app.setApplicationName(__app_name__)
    app.setApplicationDisplayName(__app_name__)
    app.setApplicationVersion(__version__)
    app.setOrganizationName(paths.ORG)
    app.setOrganizationDomain(ORG_DOMAIN)
    app.setWindowIcon(app_icon())
    return app


def apply_theme(app: Any, mode: str | None = None, accent: str | None = None) -> str:
    """Aplica o tema do config (ou o pedido) e devolve o QSS realmente usado."""
    from . import theme

    cfg = get_config()
    mode = mode or cfg.str_("ui.theme", "dark") or "dark"
    accent = accent or cfg.str_("ui.accent", "") or None
    qss = theme.apply(app, mode=mode, accent=accent)
    _apply_fallback_palette(app)
    return qss


def _apply_fallback_palette(app: Any) -> None:
    """Paleta de segurança para o que o QSS não cobre (evita texto invisível)."""
    from PySide6.QtGui import QPalette

    from . import theme

    pal = app.palette()
    base = theme.qcolor("bg")
    text = theme.qcolor("text")
    pal.setColor(QPalette.ColorRole.Window, base)
    pal.setColor(QPalette.ColorRole.WindowText, text)
    pal.setColor(QPalette.ColorRole.Base, theme.qcolor("surface"))
    pal.setColor(QPalette.ColorRole.Text, text)
    pal.setColor(QPalette.ColorRole.PlaceholderText, theme.qcolor("faint"))
    pal.setColor(QPalette.ColorRole.Button, theme.qcolor("surface2"))
    pal.setColor(QPalette.ColorRole.ButtonText, text)
    pal.setColor(QPalette.ColorRole.Highlight, theme.qcolor("accent"))
    pal.setColor(QPalette.ColorRole.HighlightedText, theme.qcolor("on_accent"))
    pal.setColor(QPalette.ColorRole.ToolTipBase, theme.qcolor("surface3"))
    pal.setColor(QPalette.ColorRole.ToolTipText, text)
    app.setPalette(pal)


def build_context(argv: list[str] | None = None) -> AppContext:
    """Um AppContext por processo, com overrides de linha de comando."""
    cfg = get_config()
    args = list(argv or [])
    if "--min-size" in args:
        try:
            cfg.set("reframe.min_size", int(args[args.index("--min-size") + 1]))
        except (IndexError, ValueError):
            pass
    ctx = AppContext(cfg)
    for a in args:
        if a.endswith(".json") and Path(a).is_file():
            try:
                ctx.load_project(Path(a))
                break
            except Exception as exc:  # noqa: BLE001 - CLI não derruba a UI
                log.warning("projeto %s não abriu: %s", a, exc)
    if ctx.project is None or not getattr(ctx.project, "name", None):
        ctx.new_project(t("Sem título"))
    return ctx


# --------------------------------------------------------------------------- #
# entrada
# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    smoke = "--smoke" in args
    force_lang = _flag_value(args, "--lang")
    force_theme = _flag_value(args, "--theme")
    geometry = _flag_value(args, "--geometry")
    project = _flag_value(args, "--project")

    if not _qt_available():
        print("PySide6 não está instalado. Rode o instalador (install.sh / install.bat) ou:\n"
              "  python -m pip install -r requirements.txt", file=sys.stderr)
        return 5

    ensure_app_dirs()
    cfg = get_config()
    try:
        app = build_application(args)
    except Exception as exc:  # noqa: BLE001 - sem display real isto acontece
        print(f"[ERRO] Não foi possível iniciar o Qt: {type(exc).__name__}: {exc}",
              file=sys.stderr)
        return 6

    _apply_fonts(app)
    apply_theme(app, mode=force_theme)
    set_language(force_lang or cfg.str_("ui.language", "pt") or "pt")

    ctx = build_context(args)
    if project:
        _load_project_arg(ctx, project)

    try:
        from .windows.main_window import MainWindow
    except Exception as exc:  # noqa: BLE001 - a UI tem de dizer o que falta
        log.exception("janela principal não carregou")
        print(f"A interface não pôde ser iniciada: {exc}", file=sys.stderr)
        print("Rode `python run.py --check` para ver o diagnóstico do ambiente.",
              file=sys.stderr)
        return 3

    try:
        win = MainWindow(ctx)
    except Exception as exc:  # noqa: BLE001
        log.exception("MainWindow falhou")
        print(f"A janela principal falhou ao ser montada: {type(exc).__name__}: {exc}",
              file=sys.stderr)
        return 3

    if geometry:
        w, h = _parse_geometry(geometry)
        win.resize(w, h)

    win.show()

    if smoke:
        return _run_smoke(app, win, ctx)

    rc = app.exec()
    try:
        ctx.save()
    except Exception as exc:  # noqa: BLE001
        log.warning("salvamento final: %s", exc)
    return int(rc or 0)


def _run_smoke(app: Any, win: Any, ctx: AppContext) -> int:
    """Abre, espera os jobs de aquecimento, percorre as páginas e fecha."""
    from PySide6.QtWidgets import QWidget
    from PySide6.QtCore import QTimer

    state = {"code": 0}

    def _check() -> None:
        try:
            central = win.centralWidget()
            widgets = len(central.findChildren(QWidget)) if central else 0
            pages = win.page_ids() if hasattr(win, "page_ids") else []
            stack = getattr(win, "stack", None)
            sizes = []
            for page_id in pages:
                page = win.page(page_id) if hasattr(win, "page") else None
                if page is None:
                    continue
                page.refresh() if hasattr(page, "refresh") else None
                sizes.append(f"{page_id}={page.width()}x{page.height()}")
            print(f"SMOKE UI: janela {win.width()}x{win.height()} ({widgets} widgets), "
                  f"{len(pages)} páginas ({', '.join(pages)}), stack={stack.count() if stack else 0}")
            if sizes:
                print("SMOKE páginas: " + "  ".join(sizes))
            state["code"] = 0 if (win.isVisible() and widgets > 20 and len(pages) >= 6) else 1
        except Exception as exc:  # noqa: BLE001
            print(f"SMOKE UI FALHOU: {type(exc).__name__}: {exc}", file=sys.stderr)
            state["code"] = 1
        app.quit()

    QTimer.singleShot(2500, _check)
    app.exec()
    try:
        ctx.save()
    except Exception:  # noqa: BLE001
        pass
    return int(state["code"])


def _flag_value(args: list[str], flag: str) -> str | None:
    if flag in args:
        idx = args.index(flag)
        if idx + 1 < len(args):
            return args[idx + 1]
    return None


def _parse_geometry(text: str) -> tuple[int, int]:
    parts = text.split("+")[0].lower().replace("x", " ").split()
    try:
        return max(800, int(parts[0])), max(560, int(parts[1]))
    except (IndexError, ValueError):
        return 1360, 820


def _load_project_arg(ctx: AppContext, raw: str) -> None:
    p = Path(raw).expanduser()
    if not p.is_file():
        log.warning("projeto da linha de comando não existe: %s", p)
        return
    try:
        ctx.load_project(p)
    except Exception as exc:  # noqa: BLE001
        log.warning("falha ao abrir %s: %s", p, exc)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
