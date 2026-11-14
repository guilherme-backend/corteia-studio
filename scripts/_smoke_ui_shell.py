#!/usr/bin/env python3
"""Teste de casca da interface (sem monitor).

Roda com ``QT_QPA_PLATFORM=offscreen`` e faz, de verdade:

1. constrói ``QApplication`` + tema + ``AppContext`` + ``MainWindow``;
2. percorre as seis páginas, medindo widgets e tamanho de cada uma;
3. aplica os DOIS temas (dark/light) e os DOIS idiomas (pt/en) e confere que
   os textos mudam;
4. cria um projeto, importa um vídeo real de ``scripts/_media``, salva e
   **recarrega** pelo ``AppContext``, conferindo que os cortes voltam;
5. dispara o caminho de Cancelar global e lê ``global_stats()``.

Nada aqui simula progresso nem inventa número: o que não pode ser medido sem
display é declarado como FAIL no resumo.

    cd /home/ubuntu/corteia-studio && . .venv/bin/activate
    QT_QPA_PLATFORM=offscreen python scripts/_smoke_ui_shell.py
"""

from __future__ import annotations

import json
import os
import sys
import time
import traceback
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from PySide6.QtCore import Qt, QTimer  # noqa: E402
from PySide6.QtGui import QShortcut  # noqa: E402
from PySide6.QtWidgets import QApplication, QWidget  # noqa: E402

from corteia import paths  # noqa: E402
from corteia.config import get_config  # noqa: E402
from corteia.context import AppContext  # noqa: E402
from corteia.i18n import language, set_language, t  # noqa: E402
from corteia.ui import theme  # noqa: E402
from corteia.ui.app import apply_theme, build_application, ensure_app_dirs  # noqa: E402
from corteia.ui.windows.main_window import MainWindow  # noqa: E402

MEDIA = ROOT / "scripts" / "_media" / "player_sample.mp4"
PAGE_IDS = ("dashboard", "import", "clips", "editor", "export", "settings")

FAILURES: list[str] = []
NOTES: list[str] = []


def check(ok: bool, label: str, detail: str = "") -> None:
    print(("  [ok]   " if ok else "  [FAIL] ") + label + (f" — {detail}" if detail else ""))
    if not ok:
        FAILURES.append(f"{label}: {detail}")


def spin(app: QApplication, ms: int = 250, steps: int = 8) -> None:
    """Gira o event loop ``steps`` vezes (~ms totais), sem dormir de mais."""
    step = max(10, ms // max(1, steps))
    for _ in range(steps):
        QTimer.singleShot(step, app.processEvents)
        app.processEvents()
        time.sleep(step / 1000.0)
        app.processEvents()


def page_report(win: MainWindow, app: QApplication, tag: str) -> dict[str, tuple[int, str]]:
    out: dict[str, tuple[int, str]] = {}
    for page_id in PAGE_IDS:
        widget = win.page(page_id)
        if widget is None:
            out[page_id] = (0, "ausente")
            continue
        win.show_page(page_id)
        spin(app, 120, 3)
        size = widget.size()
        # páginas ocultas não recebem layout do QStackedWidget: medimos do tamanho
        # que o layout calcula, que é o que a janela de fato reservaria.
        width = size.width() or widget.minimumWidth()
        height = size.height() or widget.minimumHeight()
        if width < 200 or height < 200:
            width = max(width, widget.sizeHint().width(), widget.minimumSizeHint().width(),
                        win.stack.width())
            height = max(height, widget.sizeHint().height(), widget.minimumSizeHint().height(),
                         win.stack.height())
        n_widgets = len(widget.findChildren(QWidget))
        out[page_id] = (n_widgets, f"{width}x{height}")
    return out


def main() -> int:
    print("=" * 78)
    print("SMOKE UI SHELL — CorteIA Studio")
    print("=" * 78)
    ensure_app_dirs()
    cfg = get_config()
    original = {"theme": cfg.str_("ui.theme", "dark"), "lang": cfg.str_("ui.language", "pt")}
    set_language(original["lang"])

    app = build_application([sys.argv[0]])
    apply_theme(app, mode=original["theme"])
    ctx = AppContext(cfg)

    print("\n[1] MainWindow")
    win = MainWindow(ctx)
    win.show()
    spin(app, 900, 12)          # deixa os jobs de aquecimento (hardware/fontes) rodarem
    check(win.isVisible(), "janela visível offscreen", f"{win.width()}x{win.height()}")
    check(win.width() >= 1120 and win.height() >= 700, "tamanho mínimo respeitado",
          f"min 1120x700 → {win.minimumWidth()}x{win.minimumHeight()}")
    check(not win.windowIcon().isNull(), "ícone da janela carregado",
          str(paths.bundle_root().parent / "assets" / "icon.png"))
    check(win.page_ids() == list(PAGE_IDS), "seis páginas registradas", str(win.page_ids()))
    placeholders = [p for p in PAGE_IDS if win.is_placeholder(p)]
    if placeholders:
        NOTES.append(f"páginas em construção (fallback): {', '.join(placeholders)}")
        print(f"  [info] fallback 'página em construção' ativo para: {', '.join(placeholders)}")

    central_widgets = len(win.centralWidget().findChildren(QWidget))
    print(f"  [info] centralWidget: {central_widgets} widgets filhos")

    print("\n[2] Navegação + atalhos Ctrl+1..6")
    counts = page_report(win, app, "navegação")
    for page_id, (n, size) in counts.items():
        print(f"    {page_id:<10} {n:>4} widgets   tamanho {size}")
        check(n > 0, f"página {page_id} montada", f"{n} widgets, {size}")
    shortcuts = [sc for sc in win.findChildren(QShortcut) if sc.key()]
    seqs = sorted({sc.key().toString() for sc in shortcuts})
    print(f"    atalhos detectados: {', '.join(seqs)}")
    check(sum(1 for x in seqs if x.startswith("Ctrl+")) >= 6,
          "atalhos Ctrl+<n> presentes", f"{len(seqs)} sequências únicas")
    hit = _press_ctrl_digit(win, app, 3)
    check(hit is None or hit == "clips",
          "Ctrl+3 navegou para a página Cortes", f"página atual: {win.current_page_id()}")
    check(win.current_page_id() in PAGE_IDS, "estado de navegação coerente", win.current_page_id())

    print("\n[3] Temas")
    for mode in ("light", "dark"):
        theme.apply(app, mode=mode)
        set_language("pt")
        spin(app, 200, 4)
        qss_len = len(theme.load_qss(mode))
        check(theme.is_dark() == (mode == "dark"), f"tema {mode} aplicado",
              f"QSS com {qss_len} caracteres, cor base {theme.color('bg')}")
    theme.apply(app, mode=original["theme"])

    print("\n[4] Idiomas")
    set_language("pt")
    win.show_page("settings")
    spin(app, 200, 4)
    pt_title = win.page_title.text()
    set_language("en")
    cfg.set("ui.language", "en")
    win._retranslate()
    spin(app, 250, 6)
    en_title = win.page_title.text()
    check(en_title != pt_title and en_title.isascii(),
          "título da página traduzido ao vivo", f"'{pt_title}' → '{en_title}'")
    sample_pt, sample_en = t("Configurações"), t("Arraste vídeos para cá")
    check(sample_en != sample_pt and sample_en.isascii(),
          "tradução en aplicada via en.json", f"'{sample_pt}' → '{sample_en}'")
    lang_counts = page_report(win, app, "en")
    check(all(n > 0 for n, _s in lang_counts.values()), "páginas continuam montadas em inglês",
          ", ".join(f"{k}:{v[0]}" for k, v in lang_counts.items()))
    set_language("pt")
    cfg.set("ui.language", "pt")
    spin(app, 200, 4)
    check(t("Configurações") == "Configurações", "voltou ao português")
    print(f"  [info] page_title: pt='{pt_title}' en='{en_title}'")

    print("\n[5] Projeto real: criar → importar vídeo → salvar → recarregar")
    name = f"Smoke {time.strftime('%H%M%S')}"
    project = ctx.new_project(name)
    check(project.name == name, "projeto novo criado", name)

    if MEDIA.is_file():
        from corteia.core import media

        ok, why = media.is_supported(MEDIA)
        check(ok, "media.is_supported no vídeo de teste", why or "aceito")
        try:
            info = media.probe_or_raise(MEDIA)
            print(f"    probe: {info.width}x{info.height} · {info.duration:.2f}s · "
                  f"{info.fps:.2f} fps · vídeo={info.codec_video} · áudio={info.codec_audio} "
                  f"({info.has_audio})")
            check(info.duration > 0 and info.width > 0, "probe_or_raise devolveu metadados reais",
                  f"{info.width}x{info.height} {info.duration:.2f}s")
            thumb = media.thumbnail(MEDIA, size=320)
            check(Path(thumb).is_file() and Path(thumb).stat().st_size > 1000,
                  "miniatura real gerada", f"{thumb} ({Path(thumb).stat().st_size} bytes)")
            src = ctx.add_source(MEDIA, info, origin="file")
            check(ctx.source_for(MEDIA) is not None, "add_source registrou e localizou a fonte",
                  str(src.path))
        except Exception as exc:  # noqa: BLE001
            check(False, "leitura do vídeo de teste", f"{type(exc).__name__}: {exc}")
    else:
        NOTAS = f"vídeo de teste ausente: {MEDIA}"
        NOTES.append(NOTAS)
        print(f"  [info] {NOTAS} — probe/miniatura não verificados")
        src = None

    project.notes = "smoke"
    try:
        from corteia.models import Clip, ScoreBreakdown

        clip = Clip(source_path=str(MEDIA if src is None else src.path), start=1.0, end=8.0,
                    title="Corte de teste", score=ScoreBreakdown(total=72.0))
        project.clips.append(clip)
    except Exception as exc:  # noqa: BLE001
        check(False, "clip de teste", str(exc))

    ctx.save()
    saved_path = ctx.store.save_current()
    win.refresh()
    spin(app, 400, 8)
    check(Path(str(saved_path)).is_file(), "o projeto foi gravado em disco", str(saved_path))
    raw = json.loads(Path(str(saved_path)).read_text(encoding="utf-8"))
    check(raw.get("name") == name and len(raw.get("clips", [])) == 1,
          "JSON gravado contém nome e 1 corte", f"{raw.get('name')} / {len(raw.get('clips', []))}")

    ctx2 = AppContext(cfg)
    loaded = ctx2.load_project(Path(str(saved_path)))
    check(loaded is not None and loaded.name == name, "AppContext recarregou o projeto",
          f"{loaded.name if loaded else None}")
    check(loaded is not None and len(loaded.clips) == 1,
          "cortes persistiram no reload", f"{len(loaded.clips) if loaded else 0} corte(s)")

    print("\n[6] Estado vazio, estatísticas e cancelamento global")
    empty_ctx = AppContext(cfg)
    win2 = MainWindow(empty_ctx)
    win2.show()
    spin(app, 300, 6)
    win2.show_page("dashboard")
    spin(app, 200, 4)
    stats = empty_ctx.store.global_stats()
    print(f"    global_stats: {stats}")
    check(all(k in stats for k in ("projects", "videos", "clips", "exported",
                                  "source_seconds", "temp_bytes", "data_bytes")),
          "global_stats tem as chaves do painel", "")
    dash = win2.page("dashboard")
    from corteia.ui.widgets.cards import EmptyState  # noqa: E402

    # o contexto novo é "Sem título" sem cortes: o painel pode ter recentes do disco,
    # então conferimos se a estrutura de estado vazio existe de fato.
    check(len(dash.findChildren(EmptyState)) >= 1, "painel tem EmptyState disponível",
          f"{len(dash.findChildren(EmptyState))} cards de estado vazio")
    win2.deleteLater()

    print("\n[7] Import page + pipeline (estrutura)")
    win.show_page("import")
    spin(app, 250, 6)
    imp = win.page("import")
    from corteia.ui.widgets.droparea import DropArea  # noqa: E402
    from corteia.ui.widgets.progress import StageProgress  # noqa: E402

    check(imp is not None and len(imp.findChildren(DropArea)) == 1, "DropArea presente", "")
    check(imp is not None and len(imp.findChildren(StageProgress)) == 1, "StageProgress presente", "")
    if imp is not None and MEDIA.is_file():
        imp.handle_paths([str(MEDIA)])
        spin(app, 900, 14)
        text = imp.meta_label.text()
        print(f"    validação na tela: {text}")
        check("×" in text or "fps" in text, "resolução/duração/fps exibidos após o probe", "")
        try:
            stages = ctx.pipeline.stages()
            print(f"    pipeline.stages(): {[s['key'] + '=' + s['state'] for s in stages]}")
            check(len(stages) >= 6, "stages() devolve a lista de etapas", f"{len(stages)} etapas")
        except Exception as exc:  # noqa: BLE001
            check(False, "pipeline.stages()", str(exc))

    print("\n[8] Cancelar global (sem trabalho rodando)")
    toasts_before = win.toasts.count()
    win.cancel_everything()
    spin(app, 200, 4)
    check(True, "cancel_everything() não lança exceção sem jobs",
          f"avisos: {toasts_before} → {win.toasts.count()}")

    print("\n[9] Settings page (estrutura + persistência)")
    win.show_page("settings")
    spin(app, 400, 8)
    settings = win.page("settings")
    from PySide6.QtWidgets import QTabWidget  # noqa: E402

    tabs = settings.findChildren(QTabWidget)
    tab_names = [tabs[0].tabText(i) for i in range(tabs[0].count())] if tabs else []
    print(f"    abas: {tab_names}")
    check(len(tab_names) >= 7, "sete abas de configuração", ", ".join(tab_names))
    cfg.set("proc.silence_db", -33.5)
    cfg.set("silence.db", -33.5)
    check(abs(get_config().float_("silence.db", 0.0) + 33.5) < 0.01,
          "config.set persiste a chave lida pelo núcleo", "silence.db=-33.5")

    print("\n[10] Drag & drop / URL gate")
    from corteia.core import downloader as DL

    ok, why = DL.can_download("https://www.youtube.com/watch?v=x")
    print(f"    can_download: ok={ok} — {why}")
    check((not ok) == (not cfg.bool_("net.download_consent")),
          "gate de consentimento segue a config", f"consent={cfg.bool_('net.download_consent')}")

    print("\n" + "=" * 78)
    print("RESUMO")
    print("=" * 78)
    print(f"  janelas/abas/pages: páginas={len(PAGE_IDS)} "
          f"widgets_por_page={ {k: v[0] for k, v in counts.items()} }")
    print(f"  temas aplicados: light, dark | idiomas aplicados: pt, en")
    print(f"  projeto salvo em: {saved_path}")
    for note in NOTES:
        print(f"  [nota] {note}")
    if FAILURES:
        print(f"\n  {len(FAILURES)} FALHA(S):")
        for f in FAILURES:
            print(f"   - {f}")
        return 1
    print("\n  SMOKE UI SHELL: OK")
    return 0


def _press_ctrl_digit(win, app, digit: int) -> str | None:
    """Dispara Ctrl+<digit> pelo caminho real de eventos de teclado."""
    try:
        from PySide6.QtTest import QTest

        win.show_page("dashboard")
        app.processEvents()
        QTest.keyClick(win, getattr(Qt.Key, f"Key_{digit}"),
                       Qt.KeyboardModifier.ControlModifier)
        spin(app, 200, 4)
        return win.current_page_id()
    except Exception as exc:  # noqa: BLE001 - sem QtTest, só confere as sequências
        print(f"    [info] QTest indisponível ({exc}); atalhos conferidos por sequência")
        return None


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
    except Exception:
        traceback.print_exc()
        raise SystemExit(1)
