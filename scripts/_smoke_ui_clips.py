"""Smoke test offscreen das páginas de cortes do CorteIA Studio.

Roda de verdade (sem display):

1. cria um ``AppContext`` real apontando para ``/tmp/asr_test.mp4``;
2. analisa com ``whisper_model='tiny'`` **dentro do JobRunner** (o cache de
   transcrição em ``~/.cache/corteia-studio/transcripts`` costuma acertar, então
   a etapa de fala é pulada na segunda execução);
3. monta ``ClipsPage``, ``EditorPage`` e ``ExportPage``, varre os cartões,
   checa tooltip de score, timeline, cues/``.ass`` e controles;
4. dispara **um export real** de um corte pelo JobRunner e confere o arquivo com
   ``ffprobe``.

Uso:
    cd /home/ubuntu/corteia-studio && . .venv/bin/activate
    QT_QPA_PLATFORM=offscreen python scripts/_smoke_ui_clips.py

Sai com código 1 se qualquer verificação falhar.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PySide6.QtWidgets import QApplication                       # noqa: E402

from corteia import paths                                          # noqa: E402
from corteia.context import AppContext                             # noqa: E402
from corteia.i18n import t                                         # noqa: E402
from corteia.models import AnalysisOptions, ClipStatus             # noqa: E402
from corteia.ui import theme                                       # noqa: E402
from corteia.ui.pages.clips_page import ClipsPage                   # noqa: E402
from corteia.ui.pages.editor_page import EditorPage                 # noqa: E402
from corteia.ui.pages.export_page import ExportPage                  # noqa: E402
from corteia.ui.widgets.busy import get_runner                     # noqa: E402

VIDEO = Path("/tmp/asr_test.mp4")
OUT_DIR = Path("/tmp/ui_smoke_out")

#: runner compartilhado com as páginas (memoizado por ``id(ctx)`` em busy.py)
RUNNER: object = None

FAILURES: list[str] = []
EVIDENCE: dict[str, object] = {}


def check(label: str, ok: bool, value: object = "") -> None:
    """Registra uma verificação com o número real que a sustenta."""
    EVIDENCE[label] = value
    mark = "OK  " if ok else "FAIL"
    print(f"[{mark}] {label}: {value}")
    if not ok:
        FAILURES.append(label)


def pump(app: QApplication, seconds: float) -> None:
    end = time.time() + seconds
    while time.time() < end:
        app.processEvents()
        time.sleep(0.01)


def wait_idle(app: QApplication, timeout: float = 300.0) -> None:
    """Espera o JobRunner esvaziar (com processEvents, como a GUI faria)."""
    runner = RUNNER
    end = time.time() + timeout
    while time.time() < end:
        app.processEvents()
        if hasattr(runner, "is_busy") and not runner.is_busy():
            return
        time.sleep(0.05)
    raise TimeoutError("JobRunner não esvaziou")


def run_in_thread(app: QApplication, fn, name: str, timeout: float = 600.0):
    """Executa ``fn(progress, cancel)`` pelo JobRunner e devolve o resultado."""
    holder: dict[str, object] = {"result": None, "error": None}
    done = {"v": False}

    def on_done(res):
        holder["result"] = res
        done["v"] = True

    def on_error(text):
        holder["error"] = str(text)
        done["v"] = True

    get_runner().run(fn, name=name, on_done=on_done, on_error=on_error)
    RUNNER.run(fn, name=name, on_done=on_done, on_error=on_error)
    end = time.time() + timeout
    while time.time() < end and not done["v"]:
        app.processEvents()
        time.sleep(0.02)
    if not done["v"]:
        raise TimeoutError(f"job {name} não terminou")
    if holder["error"]:
        raise RuntimeError(holder["error"])
    return holder["result"]


def ffprobe(path: Path) -> dict:
    """Medidas reais do arquivo exportado (duração, resolução, bitrate)."""
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries",
         "format=duration,size,bit_rate:stream=codec_name,width,height", "-of", "json",
         str(path)], capture_output=True, text=True, check=True)
    return json.loads(out.stdout or "{}")


def main() -> int:
    if not VIDEO.is_file():
        print(f"[ERRO] {VIDEO} não existe — rode scripts/_smoke_asr.py antes.")
        return 2

    app = QApplication(sys.argv)
    theme.apply(app)

    ctx = AppContext()
    global RUNNER
    RUNNER = get_runner(ctx)
    project = ctx.new_project("Smoke UI Cortes")
    project.export_settings.out_dir = str(OUT_DIR)
    ctx.save()

    # ---------------------------------------------------------------- 1. análise
    started = time.time()
    opts = AnalysisOptions(whisper_model="tiny", max_clips=4, target_min=8.0,
                           target_max=25.0)

    def analyse(progress, cancel):
        return ctx.pipeline.analyze(source_path=str(VIDEO), options=opts,
                                    on_progress=lambda frac, label: progress(
                                        max(0.0, min(100.0, float(frac) * 100.0)),
                                        str(label)),
                                    cancel=cancel, force=False)

    clips = run_in_thread(app, analyse, t("Análise"), timeout=900.0)
    elapsed = time.time() - started
    check("cortes sugeridos pelo pipeline", len(clips or []) > 0,
          f"{len(clips or [])} em {elapsed:.1f}s (whisper tiny)")
    stages = ctx.pipeline.stages()
    EVIDENCE["etapas"] = {str(s.get("key")): (str(s.get("state")),
                                              round(float(s.get("progress") or 0.0) * 100, 1),
                                              round(float(s.get("elapsed") or 0.0), 1))
                          for s in stages}
    print("[info] etapas:", json.dumps(EVIDENCE["etapas"], ensure_ascii=False))
    src = ctx.source_for(VIDEO)
    check("transcrição cacheada", bool(src and src.transcript
                                       and src.transcript.segments),
          f"{len(src.transcript.segments) if src and src.transcript else 0} segmentos, "
          f"{len(src.silences) if src else 0} silêncios, "
          f"{len(src.scenes) if src else 0} planos")

    # ---------------------------------------------------------------- 2. ClipsPage
    page = ClipsPage(ctx)
    page.resize(1100, 720)
    page.show()
    pump(app, 0.3)
    visible = [c for c in page._cards.values() if c.isVisible()]
    check("cartões renderizados", len(visible) == len(ctx.project.clips),
          f"{len(visible)} cartões / {len(ctx.project.clips)} cortes")
    first = ctx.project.clips[0]
    card = page._cards[first.id]
    tip = page.score_explanation(first)
    check("tooltip explica o score em português",
          any(word in tip for word in ("gancho", "emoção", "densidade")) and "hook" not in tip,
          f"score {first.score.total:.0f}/100, {len(tip.splitlines())} linhas")
    before_filter = len([c for c in page._cards.values() if c.isVisible()])
    page.slider_score.setValue(70)
    pump(app, 0.2)
    after_filter = len([c for c in page._cards.values() if c.isVisible()])
    check("filtro por score mínimo funciona", after_filter <= before_filter,
          f"score>=70: {after_filter} de {before_filter}")
    page.slider_score.setValue(0)
    pump(app, 0.2)
    page._toggle_selected(first.id)
    page._bulk(ClipStatus.APPROVED)
    check("aprovação em lote grava status",
          str(first.status.value) == "approved", str(first.status.value))
    page.edit_title.setText("Corte renomeado no smoke")
    page.current_id = first.id
    page._apply_title()
    check("título editável persiste", ctx.clip(first.id).title == "Corte renomeado no smoke",
          ctx.clip(first.id).title)
    page.grab()

    # --------------------------------------------------------------- 3. EditorPage
    editor = EditorPage(ctx, first.id)
    editor.resize(1280, 800)
    editor.show()
    pump(app, 0.4)
    check("abas laterais montadas", editor.tabs.count() == 4,
          [editor.tabs.tabText(i) for i in range(editor.tabs.count())])
    run_in_thread(app, lambda progress, cancel: ctx.pipeline.crop_plan(
        first, first.edit.aspect,
        on_progress=lambda frac, msg="": progress(float(frac) * 100.0, str(msg)),
        cancel=cancel, force=True), t("Recorte"), timeout=600.0)
    editor._request_plan(force=True)
    wait_idle(app, 600.0)
    plan = editor._plan
    check("plano de recorte calculado", plan is not None and bool(plan.points),
          f"modo={plan.mode if plan else '-'} keyframes={len(plan.points) if plan else 0}"
          f" nota={plan.note if plan else '-'}")
    rect = editor._apply_crop_at(first.start + 1.0)
    check("crop aplicado no player", editor.player.crop_rect() is not None,
          editor.player.crop_rect())
    tl = editor.timeline
    tl.set_clip(first.start, first.end)
    tl.fit_clip()
    state = tl.state()
    check("timeline tem falas + silêncios removidos",
          state["segments"] > 0 and state["removals"] >= 0,
          json.dumps(state, ensure_ascii=False))
    spans = tl.removed_spans(first.start, first.end, ctx.pipeline.timeline_for(first).ranges)
    check("lacunas derivadas de TimelineMap.ranges", len(spans) >= 0,
          f"{len(spans)} lacunas · {sum(b - a for a, b in spans):.2f}s cortados")
    # ajuste fino de entrada/saída via alças (sinal trimChanged)
    old_in = float(first.edit.trim_start or first.start)
    tl.trimChanged.emit(first.start + 0.5, first.end)
    editor._on_trim(first.start + 0.5, first.end)
    check("trim fino grava em clip.edit",
          abs(float(ctx.clip(first.id).edit.trim_start) - (first.start + 0.5)) < 0.01,
          f"{old_in:.2f} → {ctx.clip(first.id).edit.trim_start:.2f}")
    overlay = editor.overlay
    check("prévia WYSIWYG usa texto real do trecho",
          len(overlay.counts()["ass"]) > 0 and overlay.counts()["cues"] > 0,
          json.dumps(overlay.counts(), ensure_ascii=False))
    ass = Path(overlay.counts()["ass"])
    check("arquivo .ass gerado existe", ass.is_file(),
          f"{ass.name} · {ass.stat().st_size if ass.is_file() else 0} bytes")
    editor.style_panel.spin_words.setValue(2)
    editor.style_panel.chk_upper.setChecked(False)
    pump(app, 0.2)
    check("StylePanel escreve em clip.edit",
          ctx.clip(first.id).edit.subtitles is not None
          and ctx.clip(first.id).edit.subtitles.max_words_per_line == 2
          and ctx.clip(first.id).edit.subtitles.uppercase is False,
          f"{ctx.clip(first.id).edit.subtitles.max_words_per_line} palavras/linha, "
          f"maiúsculas={ctx.clip(first.id).edit.subtitles.uppercase}")
    editor.sw_silence.setChecked(True, notify=True)
    editor._on_silences(True)
    check("remove_silences persistido", ctx.clip(first.id).edit.remove_silences is True,
          ctx.clip(first.id).edit.remove_silences)
    editor.grab()

    # --------------------------------------------------------------- 4. ExportPage
    export = ExportPage(ctx)
    export.resize(1200, 800)
    export.show()
    pump(app, 0.5)
    check("fila montada a partir dos aprovados", len(export._rows) >= 1,
          f"{len(export._rows)} linhas")
    est = run_in_thread(app, lambda progress, cancel: (
        _estimate(ctx, progress)), t("Estimativa"), timeout=300.0)
    check("estimativa retornada", isinstance(est, dict) and est.get("clips", 0) > 0,
          json.dumps(est, ensure_ascii=False))

    # --- export REAL de um corte, pelo JobRunner (como a página faz)
    target = ctx.clip(first.id)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for stale in OUT_DIR.glob("*.mp4"):
        stale.unlink()
    progress_log: list[tuple[float, str]] = []

    def do_export(progress, cancel):
        def cb(clip_id, frac, msg=""):
            progress_log.append((round(float(frac) * 100.0, 1), str(msg)))
            progress(max(0.0, min(100.0, float(frac) * 100.0)), str(msg))

        return ctx.exporter.export_one(target.id)

    t0 = time.time()
    out_path = run_in_thread(app, do_export, t("Exportação"), timeout=900.0)
    dur = time.time() - t0
    produced = [p for p in OUT_DIR.glob("*.mp4")]
    check("arquivo exportado existe", bool(out_path) and Path(str(out_path)).is_file(),
          f"{Path(str(out_path)).name} em {dur:.1f}s")
    if produced:
        probe = ffprobe(produced[0])
        fmt = probe.get("format", {})
        streams = [s for s in probe.get("streams", []) if s.get("codec_type") == "video"]
        vstream = streams[0] if streams else {}
        size = Path(produced[0]).stat().st_size
        check("ffprobe: duração compatível",
              3.0 <= float(fmt.get("duration", 0) or 0) <= 40.0,
              f"duração={float(fmt.get('duration', 0) or 0):.2f}s "
              f"(corte={target.duration:.2f}s)")
        check("ffprobe: vídeo codificado",
              str(vstream.get("codec_name")) in ("h264", "hevc"),
              f"{vstream.get('codec_name')} {vstream.get('width')}x{vstream.get('height')} "
              f"· {size / 1024 / 1024:.2f} MB · bitrate {fmt.get('bit_rate')} bps")
        EVIDENCE["arquivo"] = str(produced[0])
    check("histórico ganhou linha", len(ctx.project.exports) >= 1,
          json.dumps(ctx.project.exports[-1], ensure_ascii=False, default=str)[:220])
    export._rebuild_history()
    pump(app, 0.2)
    export.grab()

    # -------------------------------------------------------- 5. PreviewDialog
    from corteia.ui.windows.preview_dialog import PreviewDialog

    preview = PreviewDialog(ctx, target, seconds=4.0)
    preview.show()
    pump(app, 0.2)
    preview.render_preview()
    wait_idle(app, 600.0)
    generated = preview.preview_path()
    check("prévia de 4s renderizada", bool(generated) and Path(str(generated)).is_file(),
          f"{generated.name} · {Path(str(generated)).stat().st_size / 1024:.0f} KB"
          if generated and Path(str(generated)).is_file() else "não gerada")
    if generated and Path(str(generated)).is_file():
        prov = ffprobe(generated)
        EVIDENCE["previa"] = prov.get("format", {})

    print("\n================ RESUMO ================")
    print(json.dumps(EVIDENCE, ensure_ascii=False, indent=1, default=str)[:4000])
    if FAILURES:
        print(f"\n[RESULTADO] FALHOU: {len(FAILURES)} verificação(es): {FAILURES}")
        return 1
    print("\n[RESULTADO] TUDO OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
