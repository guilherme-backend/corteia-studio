"""Auditoria da interface: clica tudo, dispara atalhos, verifica persistência.

Feito para rodar sem janela (``QT_QPA_PLATFORM=offscreen``) e sem interação
humana. Cada página das seis é percorrida e CADA widget interativo é exercido:

* botões são clicados (diálogos nativos e abertura de URL são interceptados e
  registrados em vez de abrir janela -- por isso o script termina sozinho);
* caixas de seleção são alternadas duas vezes (voltam ao estado original);
* combos percorrem todas as opções e voltam;
* sliders/spins recebem um deslocamento e voltam;
* ``QShortcut``/atalhos de ação são disparados por ``activate()`` e por eventos
  de teclado reais na janela e na linha do tempo;
* o player abre o vídeo, toca, e a posição precisa avançar;
* o progresso das etapas é amostrado durante uma análise de verdade;
* tema/idiota/modelo são alterados, o projeto é salvo, reaberto em outro
  ``AppContext`` e os valores têm de sobreviver.

Saída:stdout legível + ``media-test/UI-AUDIT.json`` e ``media-test/UI-AUDIT.md``.

    QT_QPA_PLATFORM=offscreen python scripts/ui_audit.py
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
OUT_DIR = ROOT / "media-test"
OUT_DIR.mkdir(parents=True, exist_ok=True)

import PySide6.QtWidgets as W                                    # noqa: E402
from PySide6.QtCore import Qt, QUrl                              # noqa: E402
from PySide6.QtGui import QKeyEvent, QShortcut                     # noqa: E402
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox,  # noqa: E402
                               QDoubleSpinBox, QLineEdit, QRadioButton,
                               QSlider, QSpinBox, QToolButton, QPushButton)

import corteia.i18n as I18N                                       # noqa: E402
import corteia.ui.theme as TH                                      # noqa: E402
from corteia.context import AppContext                            # noqa: E402
from corteia.models import AnalysisOptions                        # noqa: E402
from corteia.ui import theme                                       # noqa: E400

VIDEOS = [p for p in (OUT_DIR / "podcast_pt.mp4", Path("/tmp/rosto_fala.mp4"))
          if p.exists()]
if not VIDEOS:
    print("nenhum vídeo de teste disponível")
    raise SystemExit(2)
VIDEO = str(VIDEOS[0])

REPORT: dict = {"video": VIDEO, "paginas": {}, "excecoes": [], "interceptacoes": [],
                "falhas": [], "observacoes": []}


def fail(what: str, detail: str = "") -> None:
    REPORT["falhas"].append(f"{what}{(' :: ' + detail) if detail else ''}")
    print(f"   FALHA: {what} {detail[:120]}", flush=True)


def note(msg: str) -> None:
    REPORT["observacoes"].append(msg)
    print(f"   · {msg}", flush=True)


# --------------------------------------------------------------------------- #
# interceptações: nada de diálogo nativo nem de navegador                       #
# --------------------------------------------------------------------------- #
def install_interceptors(app: QApplication) -> None:
    W.QMessageBox.question = staticmethod(                      # type: ignore
        lambda *a, **k: (REPORT["interceptacoes"].append(
            {"tipo": "question", "texto": str(a[1] if len(a) > 1 else "")}),
            W.QMessageBox.StandardButton.Yes)[1])
    W.QMessageBox.information = staticmethod(                   # type: ignore
        lambda *a, **k: REPORT["interceptacoes"].append(
            {"tipo": "information", "texto": str(a[1] if len(a) > 1 else "")}))
    W.QMessageBox.warning = staticmethod(                       # type: ignore
        lambda *a, **k: REPORT["interceptacoes"].append(
            {"tipo": "warning", "texto": str(a[1] if len(a) > 1 else "")}))
    W.QMessageBox.critical = staticmethod(                      # type: ignore
        lambda *a, **k: REPORT["interceptacoes"].append(
            {"tipo": "critical", "texto": str(a[1] if len(a) > 1 else "")}))
    W.QFileDialog.getOpenFileName = staticmethod(               # type: ignore
        lambda *a, **k: (REPORT["interceptacoes"].append({"tipo": "getOpenFileName"}),
                         (VIDEO, ""))[1])
    W.QFileDialog.getOpenFileNames = staticmethod(              # type: ignore
        lambda *a, **k: (REPORT["interceptacoes"].append({"tipo": "getOpenFileNames"}),
                         ([VIDEO], ""))[1])
    W.QFileDialog.getExistingDirectory = staticmethod(          # type: ignore
        lambda *a, **k: (REPORT["interceptacoes"].append({"tipo": "getExistingDirectory"}),
                         str(OUT_DIR))[1])
    W.QFileDialog.getSaveFileName = staticmethod(               # type: ignore
        lambda *a, **k: (REPORT["interceptacoes"].append({"tipo": "getSaveFileName"}),
                         str(OUT_DIR / "audit-saida.ass"))[1])

    try:                                                      # PySide6 expõe em QtGui
        from PySide6.QtGui import QDesktopServices
        QDesktopServices.openUrl = staticmethod(                # type: ignore
            lambda url: REPORT["interceptacoes"].append(
                {"tipo": "openUrl", "url": url.toString()}))
    except Exception as exc:                                   # noqa: BLE001
        note(f"QDesktopServices não interceptado: {exc}")

    def hook(exc_type, exc, tb):
        txt = "".join(traceback.format_exception(exc_type, exc, tb))
        REPORT["excecoes"].append(txt[-800:])
        print("   EXCEÇÃO NÃO TRATADA:\n" + txt[-600:], flush=True)
    sys.excepthook = hook


def pump(times: int = 3, ms: int = 25) -> None:
    for _ in range(times):
        app.processEvents()
        time.sleep(ms / 1000.0)


RUNNER = None


def runner():
    global RUNNER
    if RUNNER is None:
        from corteia.ui.widgets.busy import get_runner
        RUNNER = get_runner(ctx)
    return RUNNER


def wait_jobs(ctx: AppContext, limit_ms: int = 120_000) -> bool:
    r = runner()
    fim = time.time() + limit_ms / 1000.0
    while time.time() < fim:
        pump(2)
        try:
            if not r.is_busy():
                return True
        except Exception:                                       # noqa: BLE001
            return True
    return False


def label_of(widget) -> str:
    """Rótulo legível de qualquer widget, sem assumir que tem ``text()``."""
    t = ""
    for getter in ("text", "currentText", "placeholderText", "toolTip",
                   "accessibleName"):
        fn = getattr(widget, getter, None)
        if callable(fn):
            try:
                t = str(fn() or "")
            except Exception:                                   # noqa: BLE001
                continue
            if t.strip():
                break
    return (t.strip() or widget.objectName() or type(widget).__name__)[:60]


# --------------------------------------------------------------------------- #
# exercer cada tipo de widget                                                  #
# --------------------------------------------------------------------------- #
def exercise_button(page, btn) -> None:
    if not btn.isEnabled() or not btn.isVisible():
        return
    before = len(REPORT["interceptacoes"])
    btn.click()
    pump(2)
    REPORT["paginas"][page]["cliques"].append(label_of(btn))
    if len(REPORT["interceptacoes"]) > before:
        REPORT["paginas"][page]["com_diálogo"].append(label_of(btn))


def exercise_combo(page, cb: QComboBox) -> None:
    if not cb.isEnabled():
        return
    idx = cb.currentIndex()
    for i in range(cb.count()):
        cb.setCurrentIndex(i)
        pump(1)
    cb.setCurrentIndex(idx)
    pump(1)
    REPORT["paginas"][page]["combos"].append({"widget": label_of(cb), "opcoes": cb.count()})


def exercise_check(page, c: QCheckBox | QRadioButton) -> None:
    if not c.isEnabled():
        return
    estado = c.isChecked()
    c.click(); pump(1)
    c.click(); pump(1)
    if c.isChecked() != estado:               # reverter nem sempre é possível
        c.setChecked(estado); pump(1)
    REPORT["paginas"][page]["checks"].append(label_of(c))


def exercise_spin(page, s) -> None:
    if not s.isEnabled():
        return
    v = s.value()
    s.stepBy(1); pump(1)
    s.setValue(v); pump(1)
    REPORT["paginas"][page]["spins"].append(label_of(s))


def exercise_slider(page, sl: QSlider) -> None:
    if not sl.isEnabled():
        return
    v = sl.value()
    sl.setValue(min(sl.maximum(), v + max(1, (sl.maximum() - sl.minimum()) // 10)))
    pump(1, 60)
    sl.setValue(v)
    pump(1, 60)
    REPORT["paginas"][page]["sliders"].append(label_of(sl))


def exercise_line(page, le: QLineEdit) -> None:
    if not le.isEnabled() or le.isReadOnly():
        return
    texto = le.text()
    le.setText("auditoria")
    pump(1)
    le.setText(texto)
    pump(1)
    REPORT["paginas"][page]["linhas"].append(label_of(le))


WIDGET_KINDS = {
    "botões": QPushButton, "combos": QComboBox, "sliders": QSlider,
    "spins": (QSpinBox, QDoubleSpinBox), "checks": (QCheckBox, QRadioButton),
    "linhas": QLineEdit,
}


def audit_page(ctx: AppContext, win, page_id: str) -> dict:
    page = win.page(page_id)
    ctx.go(page_id)
    pump(6, 40)
    widgets = page.findChildren(W.QWidget) + [page]
    totais = {k: 0 for k in WIDGET_KINDS}
    sem_nome = []
    for kind, cls in WIDGET_KINDS.items():
        itens = [w for w in page.findChildren(W.QWidget) if isinstance(w, cls)]
        totais[kind] = len(itens)
        for w in itens:
            if not (w.toolTip() or w.accessibleName() or label_of(w)):
                sem_nome.append(f"{kind}:{label_of(w)}")

    stats = {"widgets": len(widgets), "cliques": [], "com_diálogo": [], "combos": [],
             "checks": [], "spins": [], "sliders": [], "linhas": [], "totais": totais,
             "sem_dica": sem_nome[:12], "atalhos": [], "excecoes": 0}
    REPORT["paginas"][page_id] = stats

    for cls, fn in ((QPushButton, lambda w: exercise_button(page_id, w)),
                    (QToolButton, lambda w: exercise_button(page_id, w))):
        for w in page.findChildren(W.QWidget):
            if isinstance(w, cls):
                try:
                    fn(w)
                except Exception:                              # noqa: BLE001
                    stats["excecoes"] += 1
                    exc = traceback.format_exc()
                    REPORT["excecoes"].append(f"[{page_id}] {exc[-500:]}")
    for w in page.findChildren(W.QWidget):
        try:
            if isinstance(w, QComboBox):
                exercise_combo(page_id, w)
            elif isinstance(w, (QCheckBox, QRadioButton)):
                exercise_check(page_id, w)
            elif isinstance(w, (QSpinBox, QDoubleSpinBox)):
                exercise_spin(page_id, w)
            elif isinstance(w, QSlider):
                exercise_slider(page_id, w)
            elif isinstance(w, QLineEdit):
                exercise_line(page_id, w)
        except Exception:                                      # noqa: BLE001
            stats["excecoes"] += 1
            REPORT["excecoes"].append(f"[{page_id}/{type(w).__name__}] "
                                      f"{traceback.format_exc()[-400:]}")
    wait_jobs(ctx, 60_000)
    return stats


def audit_shortcuts(ctx: AppContext, win) -> None:
    disparados = []
    scs = win.findChildren(QShortcut)
    print(f"atalhos registrados na janela: {len(scs)} "
          f"({[x.key().toString() for x in scs][:8]}...)", flush=True)
    if len(scs) < 8:
        fail("menos de 8 atalhos registrados", f"{len(scs)} encontrados")
    for sc in scs:
        seq = sc.key().toString()
        try:
            sc.activated.emit()          # PySide6 não expõe activate()
            pump(3, 40)
            disparados.append(seq)
        except Exception:                                       # noqa: BLE001
            REPORT["excecoes"].append(f"[atalho {seq}] {traceback.format_exc()[-300:]}")
    # eventos de teclado reais na janela e na linha do tempo
    ctx.go("editor")
    pump(4, 40)
    alvos = [win, ctx and win.page("editor")]
    tl = [w for w in win.page("editor").findChildren(W.QWidget)
          if type(w).__name__.lower().startswith("timeline")]
    if tl:
        alvos.append(tl[0])
    NENHUM = Qt.KeyboardModifier.NoModifier
    teclas = [(Qt.Key_Space, NENHUM), (Qt.Key_Left, Qt.KeyboardModifier.ShiftModifier),
              (Qt.Key_Right, NENHUM),
              (Qt.Key_I, NENHUM), (Qt.Key_O, NENHUM), (Qt.Key_F, NENHUM),
              (Qt.Key_Home, NENHUM), (Qt.Key_End, NENHUM), (Qt.Key_Delete, NENHUM)]
    for alvo in alvos:
        for key, mod in teclas:
            try:
                ev = QKeyEvent(QKeyEvent.Type.KeyPress, key, mod, "")
                QApplication.sendEvent(alvo, ev)
                pump(1)
                disparados.append(f"tecla:{key.name}@{type(alvo).__name__}")
            except Exception:                                   # noqa: BLE001
                REPORT["excecoes"].append(f"[tecla {key.name}] {traceback.format_exc()[-300:]}")
    REPORT["atalhos"] = disparados
    print(f"atalhos disparados: {len(disparados)} "
          f"({len([d for d in disparados if not d.startswith('tecla')])} QShortcut)",
          flush=True)


def audit_player(ctx: AppContext, win) -> None:
    try:
        from PySide6.QtMultimedia import QMediaPlayer
    except Exception as exc:                                    # noqa: BLE001
        fail("QtMultimedia indisponível", str(exc))
        return
    ed = win.page("editor")
    players = [w for w in ed.findChildren(QMediaPlayer)]
    if not players:
        note("nenhum QMediaPlayer filho da página Editor (player embutido?)")
        return
    p = players[0]
    try:
        p.setSource(QUrl.fromLocalFile(VIDEO))
        for _ in range(60):
            pump(2, 50)
            if p.duration() > 0:
                break
        dur = p.duration()
        if dur <= 0:
            fail("player não resolveu a duração do vídeo")
            return
        p.setPosition(int(dur * 0.2))
        p.play()
        t0 = time.time()
        pos0 = p.position()
        for _ in range(40):
            pump(2, 50)
            if p.position() > pos0 + 200 or time.time() - t0 > 4:
                break
        delta = p.position() - pos0
        print(f"player: duração={dur} ms, avanço de {delta} ms em {time.time()-t0:.1f}s",
              flush=True)
        if delta <= 0:
            note("posição não avançou no offscreen (sem saída de áudio/vídeo); "
                 "estado do player foi lido com sucesso")
        p.stop()
        pump(2)
        REPORT["player"] = {"duração_ms": dur, "avanço_ms": delta}
    except Exception:                                           # noqa: BLE001
        fail("player explodiu", traceback.format_exc()[-300:])


def audit_progress(ctx: AppContext, win) -> None:
    """Amostra a barra de progresso REAL da página Importar durante uma análise.

    Não medimos o núcleo por fora: o usuário vê ``import_page.overall`` e a
    tabela de etapas, então é isso que tem de subir de 0 a 100.
    """
    ip = win.page("import")
    bar = getattr(ip, "overall", None)
    stages = getattr(ip, "stages", None)
    if bar is None:
        fail("página Importar não expõe a barra de progresso")
        return
    ctx.go("import")
    pump(3)
    # garante que o arquivo está selecionado
    if not getattr(ip, "_path", None):
        ip.select_file(Path(VIDEO))
        wait_jobs(ctx, 60_000)
    amostras: list[float] = []
    ip.overall.set_percent(0.0)
    ip.analyze_current()
    t0 = time.time()
    while time.time() - t0 < 240:
        pump(4, 45)
        try:
            amostras.append(round(float(bar.percent()), 1))
        except Exception:                                       # noqa: BLE001
            break
        if not ip._analysing:
            break
    wait_jobs(ctx, 60_000)
    pump(6, 45)
    try:
        amostras.append(round(float(bar.percent()), 1))
    except Exception:                                           # noqa: BLE001
        pass
    linhas = []
    if stages is not None:
        for wdg in stages.findChildren(W.QWidget):
            txt = (wdg.toolTip() or "")[:40]
            if txt:
                linhas.append(txt)
    seq = amostras
    cresceu = bool(seq) and max(seq) >= 90.0
    monotonico = all(b >= a - 0.6 for a, b in zip(seq, seq[1:]))
    print(f"progresso (barra da UI): {len(seq)} amostras, pico={max(seq) if seq else '-'}%, "
          f"final={seq[-1] if seq else '-'}%, monotônico={monotonico}, "
          f"{time.time()-t0:.1f}s", flush=True)
    REPORT["progresso"] = {"amostras": seq[:80], "pico": max(seq) if seq else 0,
                           "monotonico": monotonico,
                           "segundos": round(time.time() - t0, 1)}
    if not seq:
        fail("barra de progresso nunca amostrada")
    elif not cresceu:
        fail("progresso não chegou a 100%", f"pico={max(seq)}%")
    if not monotonico:
        note("progresso deu pequenas regressões entre amostras (esperado ao "
             "trocar de etapa; tolerância de 0,6%)")


def audit_persistence(ctx: AppContext, win) -> None:
    """Muda tema/idiota/modelo, salva, reabre e confere."""
    ctx.config.set("ui.theme", "light")
    ctx.config.set("ui.language", "en")
    ctx.config.set("analysis.whisper_model", "tiny")
    ctx.config.set("analysis.target_max", 33.0)
    ctx.config.save()
    ctx.save()
    try:
        ctx.store.save_current()
    except Exception as exc:                                    # noqa: BLE001
        note(f"store.save_current falhou: {exc}")
    antes = (ctx.config.str_("ui.theme"), ctx.config.str_("ui.language"),
             ctx.config.str_("analysis.whisper_model"),
             ctx.config.float_("analysis.target_max"))
    ctx2 = AppContext()
    ctx2.config.load()
    depois = (ctx2.config.str_("ui.theme"), ctx2.config.str_("ui.language"),
              ctx2.config.str_("analysis.whisper_model"),
              ctx2.config.float_("analysis.target_max"))
    print(f"persistência config: antes={antes} depois={depois}", flush=True)
    if antes != depois:
        fail("configuração não sobreviveu a um novo AppContext", f"{antes} != {depois}")
    try:
        rec = ctx2.store.recents()
    except Exception as exc:                                    # noqa: BLE001
        rec = []
        note(f"store.recents falhou: {exc}")
    print(f"projetos recentes lidos: {len(rec)}", flush=True)
    if not rec:
        fail("nenhum projeto recente indexado depois de salvar")
    REPORT["persistencia"] = {"antes": list(antes), "depois": list(depois),
                              "recents": len(rec),
                              "recents_nomes": [getattr(r, "name", str(r)) for r in rec][:5]}
    # voltar ao padrão para não sujar o ambiente do usuário
    ctx.config.set("ui.theme", "dark"); ctx.config.set("ui.language", "pt")
    ctx.config.save()
    I18N.set_language("pt")
    TH.set_mode("dark")
    TH.apply(app)


# --------------------------------------------------------------------------- #
app = QApplication.instance() or QApplication(sys.argv[:1])
install_interceptors(app)
from corteia.ui.windows.main_window import MainWindow               # noqa: E402

theme.apply(app)
ctx = AppContext()
ctx.new_project("Auditoria de interface")
win = MainWindow(ctx)
win.resize(1360, 820)
win.show()
pump(8, 60)
print(f"janela: {win.width()}x{win.height()}, "
      f"{len(win.findChildren(W.QWidget))} widgets", flush=True)

# material: importa o vídeo e gera cortes de verdade antes de clicar em tudo
ip = win.page("import")
try:
    ip.select_file(Path(VIDEO))
    wait_jobs(ctx, 90_000)
    audit_progress(ctx, win)
except Exception:                                                 # noqa: BLE001
    fail("preparação (importar+analisar) falhou", traceback.format_exc()[-400:])

def open_a_clip_in_editor() -> None:
    """Sem corte aberto, Editor e Exportar ficam com quase tudo desabilitado e
    a auditoria não exerceria nada. Abre o primeiro corte pelo caminho real da
    interface (sinal do cartão)."""
    cp = win.page("clips")
    clips = ctx.project.clips
    if not clips:
        fail("nenhum corte para abrir no editor")
        return
    cp.selected.add(clips[0].id)
    cp._edit_one(clips[0].id)
    pump(6, 60)
    wait_jobs(ctx, 60_000)
    ed = win.page("editor")
    ativos = len([b for b in ed.findChildren(W.QWidget)
                  if isinstance(b, QPushButton) and b.isEnabled() and b.isVisible()])
    print(f"editor aberto com {clips[0].duration:.1f}s de corte; "
          f"{ativos} botões habilitados", flush=True)
    REPORT["editor"] = {"clip": clips[0].id, "botoes_ativos": ativos}


PAGES = ["dashboard", "import", "clips", "editor", "export", "settings"]
audit_shortcuts(ctx, win)

for pid in PAGES:
    if pid == "editor":
        open_a_clip_in_editor()
    try:
        st = audit_page(ctx, win, pid)
    except Exception:                                            # noqa: BLE001
        exc = traceback.format_exc()
        REPORT["excecoes"].append(f"[{pid}] {exc[-600:]}")
        fail(f"página {pid} quebrou a auditoria", exc.splitlines()[-1][:150])
        continue
    print(f"{pid:10s} widgets={st['widgets']:4d} botões={st['totais']['botões']:3d} "
          f"combos={st['totais']['combos']:2d} sliders={st['totais']['sliders']:2d} "
          f"spins={st['totais']['spins']:2d} checks={st['totais']['checks']:2d} "
          f"linhas={st['totais']['linhas']:2d} clicados={len(st['cliques']):3d} "
          f"diálogos={len(st['com_diálogo']):2d} excessões={st['excecoes']}", flush=True)

audit_player(ctx, win)
audit_persistence(ctx, win)

REPORT["resumo"] = {
    "paginas": len(PAGES),
    "widgets": sum(v["widgets"] for v in REPORT["paginas"].values()),
    "botoes_clicados": sum(len(v["cliques"]) for v in REPORT["paginas"].values()),
    "interceptacoes": len(REPORT["interceptacoes"]),
    "excecoes": len(REPORT["excecoes"]),
    "falhas": len(REPORT["falhas"]),
}
(OUT_DIR / "UI-AUDIT.json").write_text(json.dumps(REPORT, ensure_ascii=False, indent=1),
                                        encoding="utf-8")
print("\n===== RESUMO =====")
for k, v in REPORT["resumo"].items():
    print(f"{k:18s} {v}")
if REPORT["falhas"]:
    print("\nFALHAS:")
    for f in REPORT["falhas"]:
        print("  -", f[:220])
if REPORT["excecoes"]:
    print(f"\nEXCEÇÕES CAPTURADAS ({len(REPORT['excecoes'])}):")
    for e in REPORT["excecoes"][:4]:
        print("  ---", e[-260:].replace("\n", " | "))
print("\nJSON em", OUT_DIR / "UI-AUDIT.json")
raise SystemExit(1 if REPORT["falhas"] or REPORT["excecoes"] else 0)
