#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Smoke de produto do CorteIA Studio.

Este e o teste que ``run.py --smoke`` (e portanto ``./run.sh --smoke`` /
``run.bat --smoke``) deve chamar.  Ele nao abre janela e nao usa mocks:

  1. importa todos os modulos do pacote ``corteia`` (import quebrado = falha);
  2. imprime versao e diagnostico real de ambiente (Python, FFmpeg, hardware,
     modelos Whisper baixados, detector de rostos, fontes, disco);
  3. se ``/tmp/asr_test.mp4`` existir, roda a **pipeline real** de ponta a
     ponta nesse arquivo (probe -> transcricao -> silencio -> cenas ->
     planejamento de cortes) e **exporta um corte** de verdade pelo
     ``Exporter``; o MP4 gerado e re-medido com ffprobe (duracao, resolucao,
     tamanho) -- nada e dado como sucesso por confianca;
  4. sai com 0 (tudo certo) ou 1 (falha real).  Sem o video de exemplo o
     resultado e "parcial": as etapas 1 e 2 continuam obrigatorias.

Uso:
    . .venv/bin/activate            # .venv\\Scripts\\activate no Windows
    python scripts/smoke.py
    python scripts/smoke.py --model tiny --keep
    python scripts/smoke.py --quick          # pula video/exportacao
    python scripts/smoke.py --json           # relatorio final em JSON

O ponto de entrada para o app e ``main(argv) -> int``; ``run.py --smoke`` deve
apenas repassar os argumentos e propagar o codigo de saida.
"""
from __future__ import annotations

import argparse
import importlib
import json
import os
import platform
import resource
import subprocess
import sys
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Sem isso, em maquina sem display o PySide6 aborta o processo.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

SAMPLE_DEFAULT = Path(os.environ.get("CORTEIA_SMOKE_MEDIA", "/tmp/asr_test.mp4"))

#: Modulos que o produto EXIGE. Se algum sumir do pacote, o smoke falha com
#: nome, em vez de passar silencioso porque "importou o que existia".
REQUIRED_MODULES = [
    "corteia", "corteia.paths", "corteia.config", "corteia.models",
    "corteia.hardware", "corteia.i18n", "corteia.project", "corteia.context",
    "corteia.core", "corteia.core.errors", "corteia.core.ffmpeg_cli",
    "corteia.core.media", "corteia.core.silence", "corteia.core.scenes",
    "corteia.core.transcribe", "corteia.core.faces", "corteia.core.reframe",
    "corteia.core.lexicon", "corteia.core.analyze", "corteia.core.timeline",
    "corteia.core.subtitles", "corteia.core.llm", "corteia.core.fonts",
    "corteia.core.render", "corteia.core.exporter", "corteia.core.pipeline",
    "corteia.core.downloader", "corteia.workers.jobs",
]


def discover_modules() -> list[str]:
    """Todos os modulos importaveis de ``corteia`` (UI inclusa de proposito:
    um import quebrado em ``corteia/ui/`` derruba o app inteiro)."""
    import corteia

    found = ["corteia"]
    for mod in pkgutil.walk_packages(corteia.__path__, prefix="corteia."):
        name = mod.name
        if ".__pycache__" in name or name.endswith(".resources"):
            continue
        found.append(name)
    return sorted(set(found))

GREEN, RED, YEL, DIM, BOLD, RST = ("\033[32m", "\033[31m", "\033[33m",
                                   "\033[2m", "\033[1m", "\033[0m")
if not sys.stdout.isatty():
    GREEN = RED = YEL = DIM = BOLD = RST = ""


class Report:
    """Contador de checagens + saida legivel em portugues."""

    def __init__(self) -> None:
        # contadores com prefixo n_ para nao colidir com os metodos
        self.n_ok = 0
        self.n_warn = 0
        self.n_fail = 0
        self.n_skip = 0
        self.rows: list[dict] = []
        self.t0 = time.time()

    def _row(self, kind: str, label: str, detail: str = "") -> None:
        self.rows.append({"kind": kind, "label": label, "detail": detail})

    def ok_(self, label: str, detail: str = "") -> None:
        self.n_ok += 1
        self._row("ok", label, detail)
        print(f"  {GREEN}[ok]{RST}   {label}" + (f"  {DIM}{detail}{RST}" if detail else ""))

    def warn(self, label: str, detail: str = "") -> None:
        self.n_warn += 1
        self._row("warn", label, detail)
        print(f"  {YEL}[aviso]{RST} {label}" + (f"  {DIM}{detail}{RST}" if detail else ""))

    def fail_(self, label: str, detail: str = "") -> None:
        self.n_fail += 1
        self._row("fail", label, detail)
        print(f"  {RED}[falha]{RST} {label}" + (f"  {DIM}{detail}{RST}" if detail else ""))

    def skip_(self, label: str, detail: str = "") -> None:
        self.n_skip += 1
        self._row("skip", label, detail)
        print(f"  {DIM}[pulo]  {label} {detail}{RST}")

    def step(self, title: str) -> None:
        print(f"\n{BOLD}==> {title}{RST}")


def _rss_peak_mb() -> float:
    """Pico de RSS do processo em MB (medido, nao estimado)."""
    peak_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return peak_kb / 1024.0 if peak_kb > 0 else 0.0


# --------------------------------------------------------------------------- #
# 1. imports
# --------------------------------------------------------------------------- #
def check_imports(rep: Report) -> dict:
    rep.step("1/4  Importando todos os modulos do pacote")
    missing = [m for m in REQUIRED_MODULES if importlib.util.find_spec(m) is None]
    for m in missing:
        rep.fail_(f"modulo obrigatorio ausente: {m}")
    names = discover_modules()
    broken: list[str] = []
    for name in names:
        try:
            importlib.import_module(name)
        except Exception as exc:                                # noqa: BLE001
            broken.append(name)
            rep.fail_(f"import {name}", f"{type(exc).__name__}: {exc}")
    if not broken and not missing:
        rep.ok_(f"{len(names)} modulos descobertos, todos importaram sem erro",
                f"{len(REQUIRED_MODULES)} obrigatorios conferidos")
    return {"modules": len(names), "required": len(REQUIRED_MODULES),
            "missing_required": missing, "broken": broken}


# --------------------------------------------------------------------------- #
# 2. diagnosticos
# --------------------------------------------------------------------------- #
def check_environment(rep: Report) -> dict:
    rep.step("2/4  Versao e diagnostico do ambiente")
    from corteia import hardware, paths

    rep.ok_(f"CorteIA Studio {paths.VERSION}",
            f"{'pacote congelado' if paths.is_frozen() else 'arquivos-fonte'}")
    rep.ok_(f"Python {platform.python_version()}",
            f"{platform.system()} {platform.release()} / {platform.machine()}")

    # FFmpeg
    if hardware.find_ffmpeg() and hardware.find_ffprobe():
        try:
            out = subprocess.run([hardware.find_ffmpeg(), "-version"],
                                 capture_output=True, text=True, timeout=20).stdout
            first = (out.splitlines() or [""])[0]
            rep.ok_("FFmpeg encontrado", first[:70])
        except Exception as exc:                                # noqa: BLE001
            rep.fail_("FFmpeg nao respondeu", str(exc)[:120])
    else:
        rep.fail_("FFmpeg/FFprobe ausentes", "o app nao roda a pipeline sem eles")

    report = hardware.probe()
    for line in report.summary_lines():
        rep.ok_(line)
    if not report.subtitles_filter:
        rep.warn("FFmpeg sem filtro subtitles (libass)",
                 "legenda queimada fica indisponivel")
    if "libx264" not in report.video_encoders:
        rep.fail("FFmpeg sem libx264", "exportacao H.264 e o padrao do app")
    # `report.video_encoders` so lista encoder de VIDEO: checar aac ali daria
    # falso negativo. A lista completa vem do proprio ffmpeg.
    listing = _ffmpeg_listing()
    for enc, critical in (("libx264", True), ("aac", True), ("libx265", False)):
        present = _listing_has(listing, enc)
        if present:
            rep.ok_(f"encoder {enc} disponivel")
        elif critical:
            rep.fail(f"FFmpeg sem {enc}", "exportacao de video/audio pode falhar")
        else:
            rep.warn(f"FFmpeg sem {enc}", "exportacao em H.265 indisponivel (use H.264)")

    # modelos Whisper
    from corteia.core import transcribe as TR
    catalog = TR.model_catalog()
    for row in catalog:
        if row.get("downloaded"):
            rep.ok_(f"modelo Whisper '{row.get('name')}' no disco",
                    f"{float(row.get('on_disk') or 0) / 1e6:.0f} MB baixados")
    if not any(row.get("downloaded") for row in catalog):
        rep.warn("nenhum modelo Whisper baixado",
                 "a primeira transcricao vai baixar (precisa de internet)")

    # detector de rostos
    from corteia.core import faces as FA
    if FA.detector_available():
        rep.ok_(f"detector de rostos: {FA.detector_name()}")
    else:
        rep.warn("sem detector de rostos", FA.detector_note() or
                 "reframe vai cair em centralizar/saliencia")
    yunet = paths.bundled_model(FA.YUNET_FILE)
    (rep.ok_ if yunet.exists() else rep.warn)(
        f"modelo YuNet ({FA.YUNET_FILE})", f"{yunet} / {yunet.stat().st_size/1e6:.1f} MB"
        if yunet.exists() else f"ausente em {yunet}")

    # fontes
    from corteia.core import fonts as FO
    names = FO.installed()
    (rep.ok_ if names else rep.warn)(f"{len(names)} fontes instaladas visiveis ao libass",
                                     ", ".join(names[:4]) + ("..." if len(names) > 4 else ""))

    # disco / pastas
    free = paths.free_bytes_temp()
    (rep.ok_ if free > 5 * 1024**3 else rep.warn)(
        f"{free / 1024**3:.1f} GB livres em {paths.temp_root()}")
    rep.ok_("dados do app", str(paths.data_dir()))
    rep.ok_("cache do app", str(paths.cache_dir()))

    cfg = None
    try:
        from corteia.config import get_config
        cfg = get_config()
        rep.ok_("configuracao carregada", str(cfg.path()))
    except Exception as exc:                                    # noqa: BLE001
        rep.fail_("config nao carregou", str(exc)[:120])

    llm_provider = cfg.str_("api.provider") if cfg else "off"
    rep.ok_(f"IA externa: provedor '{llm_provider or 'off'}'",
            "sem API = pontuacao 100% local (documentado no README)")

    return {"ffmpeg": report.ffmpeg_version, "ram_gb": round(report.ram_gb, 1),
            "cpu": report.cpu_count, "gpu": report.gpu_name or "nenhuma"}


# --------------------------------------------------------------------------- #
# 3+4. pipeline real + exportacao
# --------------------------------------------------------------------------- #
def run_pipeline(rep: Report, sample: Path, model: str, keep: bool,
                 timeout_s: float) -> dict:
    rep.step(f"3/4  Pipeline real em {sample}")
    from PySide6.QtCore import QCoreApplication

    app = QCoreApplication.instance() or QCoreApplication([])

    from corteia.context import AppContext
    from corteia.core import media
    from corteia.models import (AnalysisOptions, ClipStatus, ExportSettings,
                                Resolution)
    from corteia import paths

    out_dir = Path("/tmp") / f"corteia-smoke-{os.getpid()}"
    out_dir.mkdir(parents=True, exist_ok=True)
    result: dict = {"sample": str(sample), "ran": False}

    t_start = time.time()
    ctx = AppContext()
    project = ctx.new_project("Smoke")
    try:
        info = media.probe_or_raise(sample)
        rep.ok_("probe", f"{info.width}x{info.height} @ {info.fps:.2f} fps, "
                         f"{info.duration:.1f}s, {info.codec_video}/{info.codec_audio}")
        result.update(width=info.width, height=info.height,
                      duration=round(info.duration, 2))

        opts = AnalysisOptions(
            target_min=8.0, target_max=min(25.0, max(9.0, info.duration * 0.6)),
            max_clips=4, min_score=10.0, whisper_model=model, language="pt",
            remove_silences=True, smart_boundaries=True, follow_speaker=True,
            use_llm=False,
        )
        last = {"pct": 0.0}

        def progress(frac: float, msg: str = "") -> None:
            # Núcleo reporta fracao 0..1 (convencao do core).
            pct = float(frac) * 100.0
            if pct - last["pct"] >= 10.0 or pct >= 99.9:
                last["pct"] = pct
                print(f"      {DIM}{pct:5.1f}%  {msg}{RST}")
            if time.time() - t_start > timeout_s:
                ctx.pipeline.cancel()

        clips = ctx.pipeline.analyze(sample, opts, on_progress=progress)
        ran_s = time.time() - t_start
        result["ran"] = True
        result["analyze_seconds"] = round(ran_s, 1)
        result["clips"] = len(clips)

        src = ctx.source_for(sample)
        segs = len(src.transcript.segments) if src and src.transcript else 0
        words = sum(len(s.words) for s in src.transcript.segments) if src and src.transcript else 0
        (rep.ok_ if segs else rep.fail_)(
            f"transcricao: {segs} trechos / {words} palavras",
            f"{ran_s:.1f}s de CPU, pico de RAM {_rss_peak_mb():.0f} MB")
        if src and src.transcript and src.transcript.segments:
            exemplo = src.transcript.segments[0].text.strip()
            rep.ok_("primeiro trecho reconhecido", exemplo[:78])
        (rep.ok_ if src and src.silences else rep.warn)(
            f"mapa de silencio: {len(src.silences) if src else 0} pausas")
        (rep.ok_ if src and src.scenes else rep.warn)(
            f"mapa de planos: {len(src.scenes) if src else 0} cenas")

        (rep.ok_ if clips else rep.fail_)(f"{len(clips)} cortes planejados",
                                          "; ".join(f"{c.duration:.0f}s/{c.score.total:.0f}"
                                                    for c in clips[:4]))
        if not clips:
            rep.fail_("pipeline nao gerou nenhum corte",
                      "em um video com fala isso e um defeito de produto")
            return result

        best = max(clips, key=lambda c: c.score.total)
        rep.ok_("melhor corte", f"'{best.title[:44]}' "
                                f"{best.start:.1f}-{best.end:.1f}s "
                                f"(nota {best.score.total:.0f}) - {best.reason[:60]}")

        # ---------------------------------------------------------- exportar
        rep.step("4/4  Exportando 1 corte (FFmpeg real)")
        best.status = ClipStatus.APPROVED
        settings = ExportSettings()
        settings.resolution = Resolution.R_720          # smoke rapido, caminho real
        settings.out_dir = str(out_dir)
        settings.quality_crf = 23
        settings.preset = "veryfast"
        settings.max_parallel = 1
        settings.burn_subtitles = True                  # exercita libass
        project.export_settings = settings
        ctx.save()

        est = None
        try:
            from corteia.core.exporter import estimate
            est = estimate(ctx, [best])
            rep.ok_("estimativa de exportacao",
                    f"{est.get('mb')} MB / {est.get('minutes')} min "
                    f"({'medido no historico' if est.get('measured') else 'heuristica'})")
        except Exception as exc:                        # noqa: BLE001
            rep.warn("estimativa indisponivel", str(exc)[:100])

        t_exp = time.time()
        out_path = ctx.exporter.export_one(best.id, settings)
        exp_s = time.time() - t_exp
        out = Path(out_path)
        if not out.exists():
            rep.fail_("arquivo exportado nao existe", str(out))
            return result

        size = out.stat().st_size
        check = media.probe(out)
        expected = max(1.0, best.duration)
        ok_dur = abs(check.duration - expected) <= max(1.2, expected * 0.25)
        ok_res = (check.width, check.height) == settings.target_size
        (rep.ok_ if ok_dur else rep.fail_)(
            "duracao do corte confere", f"{check.duration:.2f}s (esperado ~{expected:.2f}s)")
        (rep.ok_ if ok_res else rep.fail_)(
            "resolucao do corte confere",
            f"{check.width}x{check.height} (esperado "
            f"{settings.target_size[0]}x{settings.target_size[1]})")
        (rep.ok_ if size > 20_000 else rep.fail_)(
            f"arquivo gerado: {out.name}", f"{size/1e6:.2f} MB em {exp_s:.1f}s "
            f"({check.codec_video}/{check.codec_audio})")

        result.update(export=str(out), export_seconds=round(exp_s, 1),
                      export_mb=round(size / 1e6, 2),
                      out_w=check.width, out_h=check.height,
                      out_duration=round(check.duration, 2),
                      est=est if isinstance(est, dict) else None)

        if not keep:
            try:
                out.unlink()
                for extra in out_dir.glob("*"):
                    extra.unlink(missing_ok=True)
                out_dir.rmdir()
            except OSError as exc:
                rep.warn("nao consegui limpar a saida", str(exc)[:80])
        else:
            rep.ok_("saida preservada", str(out))
    except Exception as exc:                            # noqa: BLE001
        rep.fail_("pipeline/export lancou excecao", f"{type(exc).__name__}: {exc}")
        traceback.print_exc(limit=6)
        result["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        # Nao deixa o projeto do smoke poluir os recentes do usuario.
        try:
            from corteia import paths as _p
            ctx.store.delete(_p.projects_dir() / f"{project.id}.json")
        except Exception:                               # noqa: BLE001
            pass
        del app
    return result


# --------------------------------------------------------------------------- #
def build_sample_if_missing(rep: Report) -> Path | None:
    """Tenta gerar /tmp/asr_test.mp4 com espeak-ng (mesmo caminho do
    ``scripts/_smoke_asr.py``); se nao der, so avisa."""
    import shutil
    if not shutil.which("espeak-ng"):
        return None
    script = ROOT / "scripts" / "_smoke_asr.py"
    if not script.exists():
        return None
    try:
        mod = importlib.util.spec_from_file_location("_smoke_asr_build", script)
        if mod is None or mod.loader is None:
            return None
        ns: dict = {"__name__": "_smoke_asr_build"}
        mod.loader.exec_module(ns)
        fn = ns.get("build_media")
        if callable(fn):
            fn(force=False)
            return SAMPLE_DEFAULT if SAMPLE_DEFAULT.exists() else None
    except Exception as exc:                            # noqa: BLE001
        rep.warn("nao consegui gerar a midia de exemplo", str(exc)[:100])
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="smoke.py", description="Smoke de produto do CorteIA Studio")
    parser.add_argument("--media", default=str(SAMPLE_DEFAULT),
                        help="video real para a pipeline (padrao /tmp/asr_test.mp4)")
    parser.add_argument("--model", default="base",
                        help="modelo Whisper da transcricao (padrao: base)")
    parser.add_argument("--quick", action="store_true",
                        help="so imports + diagnostico (pula pipeline e export)")
    parser.add_argument("--keep", action="store_true",
                        help="nao apaga o MP4 exportado")
    parser.add_argument("--timeout", type=float, default=420.0,
                        help="limite de segundos da analise antes de cancelar")
    parser.add_argument("--json", action="store_true", dest="as_json",
                        help="imprime o relatorio final em JSON")
    args = parser.parse_args(argv)

    rep = Report()
    print(f"{BOLD}CorteIA Studio - smoke de produto{RST}")
    data: dict = {"started": time.strftime("%Y-%m-%d %H:%M:%S")}

    imports = check_imports(rep)
    data["imports"] = imports
    data["environment"] = check_environment(rep)

    sample = Path(args.media)
    if not sample.exists() and not args.quick:
        built = build_sample_if_missing(rep)
        if built:
            rep.warn("midia de exemplo gerada por espeak-ng", str(built))

    if args.quick:
        rep.skip_("pipeline e exportacao", "pedido com --quick")
        data["pipeline"] = {"skipped": "quick"}
    elif not sample.exists():
        rep.skip_("pipeline e exportacao",
                  f"{sample} nao existe (crie-o com scripts/_smoke_asr.py)")
        data["pipeline"] = {"skipped": f"{sample} ausente"}
    else:
        data["pipeline"] = run_pipeline(rep, sample, args.model, args.keep,
                                       args.timeout)

    secs = time.time() - rep.t0
    partial = bool(data.get("pipeline", {}).get("skipped"))
    status = "FALHOU" if rep.n_fail else ("PARCIAL" if partial else "OK")
    print(f"\n{BOLD}==> Resultado{RST}")
    print(f"  {rep.n_ok} ok | {rep.n_warn} avisos | {rep.n_fail} falhas | "
          f"{rep.n_skip} pulos"
          f"  ({secs:.1f}s, pico de RAM {_rss_peak_mb():.0f} MB)")
    if rep.n_fail:
        print(f"  {RED}SMOKE {status}{RST}")
    elif partial:
        print(f"  {YEL}SMOKE {status}{RST} - ambiente e imports certos, "
              f"mas a pipeline nao rodou")
    else:
        print(f"  {GREEN}SMOKE {status}{RST}")

    data["summary"] = {"ok": rep.n_ok, "warnings": rep.n_warn,
                       "failures": rep.n_fail, "skipped": rep.n_skip,
                       "seconds": round(secs, 1),
                       "peak_rss_mb": round(_rss_peak_mb(), 1), "status": status,
                       "checks": rep.rows}
    if args.as_json:
        print(json.dumps(data, ensure_ascii=False, indent=2, default=str))

    # PARCIAL nao e falha: sem midia de exemplo nao ha o que processar.
    return 1 if rep.n_fail else 0


if __name__ == "__main__":
    sys.exit(main())
