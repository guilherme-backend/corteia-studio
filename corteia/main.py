"""Inicialização e linha de comando do CorteIA Studio.

Uso normal: ``python run.py`` abre a interface.  Também há modos sem janela,
que servem para instalar, diagnosticar e automatizar:

    python run.py --check          FFmpeg, codecs, fontes, GPU, modelos
    python run.py --smoke          pipeline real num vídeo sintético (sem UI)
    python run.py --analyze A.mp4  gera cortes e o JSON ao lado do arquivo
    python run.py --export A.mp4 0 30   exporta um trecho direto
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import sys
from pathlib import Path

from . import __app_name__, __version__

BANNER = f"""
  ██████╗ ██████╗ ██████╗ ████████╗███████╗██╗  ██╗██╗
 ██╔════╝██╔═══██╗██╔══██╗╚══██╔══╝██╔════╝╚██╗██╔╝██║
 ██║     ██║   ██║██████╔╝   ██║   █████╗   ╚███╔╝ ██║
 ██║     ██║   ██║██╔═══╝    ██║   ██╔══╝   ██╔██╗ ██║
 ╚██████╗╚██████╔╝██║        ██║   ███████╗██╔╝ ██╗██║
  ╚═════╝ ╚═════╝ ╚═╝        ╚═╝   ╚══════╝╚═╝  ╚═╝╚═╝   {__app_name__} {__version__}
"""


def _setup_logging(verbose: bool = False) -> Path:
    from . import paths
    log_dir = paths.logs_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    level = logging.DEBUG if verbose else logging.INFO
    handlers: list[logging.Handler] = [
        logging.FileHandler(log_dir / "corteia.log", encoding="utf-8")]
    if sys.stderr and sys.stderr.isatty():
        handlers.append(logging.StreamHandler())
    logging.basicConfig(level=level, handlers=handlers,
                        format="%(asctime)s %(levelname)-7s %(name)-22s %(message)s",
                        datefmt="%H:%M:%S")
    for noisy in ("shiboken6", "urllib3.connectionpool", "huggingface_hub", "faster_whisper"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    return log_dir / "corteia.log"


def cmd_check(argv_out: list[str]) -> int:
    """Tell the truth about the environment: what works and what will not."""
    from . import hardware, paths
    from .config import get_config
    from .core import fonts, transcribe
    hw = hardware.probe()
    ff = hardware.find_ffmpeg()
    print(f"{'App':22s} {__app_name__} {__version__}")
    print(f"{'Python':22s} {sys.version.split()[0]} em {sys.executable}")
    print(f"{'CPU / RAM':22s} {hw.cpu_count} núcleos · {hw.ram_gb:.1f} GB "
          f"({hw.ram_available_gb:.1f} GB livres)")
    print(f"{'GPU':22s} {hw.gpu_name or 'não detectada'}"
          + (f" · {hw.gpu_vram_gb:.1f} GB" if hw.gpu_vram_gb else "")
          + (" · CUDA ativo" if hw.cuda_available else ""))
    print(f"{'FFmpeg':22s} {ff or 'NÃO ENCONTRADO'}  ({hw.ffmpeg_version})")
    if ff:
        for name in ("libx264", "libx265", "aac"):
            ok = name in hw.video_encoders or name in _audio_encoders()
            print(f"  codec {name:12s}  {'ok' if ok else 'AUSENTE'}")
        print(f"  legendas (libass)   {'ok' if hw.subtitles_filter else 'AUSENTE — sem isso nada é queimado'}")
        print(f"  aceleração GPU      "
              + (", ".join(e for e in hw.video_encoders if "nvenc" in e or "qsv" in e)
                 or "somente CPU"))
    print(f"{'Whisper':22s} " + " · ".join(
        f"{m} {'instalado' if transcribe.is_downloaded(m) else 'não baixado'}"
        for m in ("tiny", "base", "small")))
    print(f"{'Fontes instaladas':22s} {len(fonts.installed())}")
    print(f"{'Config':22s} {get_config().path}")
    print(f"{'Cache':22s} {paths.cache_dir()}")
    print(f"{'Temp':22s} {paths.temp_root()} ({paths.human_bytes(paths.temp_usage_bytes())})")
    print(f"{'Projetos':22s} {paths.projects_dir()}")
    for note in hw.notes:
        print("  aviso: " + " ".join(note.split())[:150])
    problems = []
    if not ff:
        problems.append("FFmpeg não encontrado: instale e adicione ao PATH "
                        "(Windows: winget install Gyan.FFmpeg).")
    elif "libx264" not in hw.video_encoders:
        problems.append("Este FFmpeg não tem libx264; a exportação H.264 vai falhar.")
    if not hw.subtitles_filter:
        problems.append("O FFmpeg local não tem o filtro de legendas (libass): as "
                        "legendas não serão queimadas no vídeo final.")
    if not fonts.file_for("Arial"):
        problems.append("Nenhuma fonte de título foi localizada; os títulos podem "
                        "sair com fonte genérica.")
    if problems:
        print("\nPROBLEMAS:")
        for text in problems:
            print("  - " + text)
        return 1
    try:
        from .hardware import ctranslate2_device as _cd

        _dev, _ct = _cd()
        for _nome in ("tiny", "base", "small", "medium", "large-v3", "distil-large-v3"):
            _ok, _why = transcribe.check_fit(_nome, _dev, _ct)
            print(f"  modelo {_nome:16s} {'ok' if _ok else 'NÃO CABE':8s} {_why}")
        print(f"  recomendado         {transcribe.largest_fitting(_dev, _ct)!r} em {_dev}/{_ct}")
    except Exception as _exc:                               # noqa: BLE001
        print(f"  aviso: orçamento de memória indisponível ({_exc})")

    print("\nTudo pronto para uso.")
    return 0


def _audio_encoders() -> list[str]:
    """Audio codecs are not in video_encoders; ask FFmpeg once."""
    from .core import ffmpeg_cli
    try:
        out, _ = ffmpeg_cli.capture([ffmpeg_cli.ffmpeg_bin(), "-hide_banner", "-encoders"])
    except Exception:                                       # noqa: BLE001
        return []
    names = []
    for line in out.splitlines():
        # ffmpeg lists codecs as " A....D aac                AAC (…)": the flag
        # column is one token, the codec name is the next.
        parts = line.split()
        # split() eats the leading space, so the flag column is "A....D"
        if len(parts) >= 2 and len(parts[0]) == 6 and parts[0][0] == "A":
            names.append(parts[1])
    return names


def cmd_smoke() -> int:
    """Run the real product pipeline on a generated video — no GUI, no mocks."""
    from .context import AppContext
    from .models import AnalysisOptions, ClipStatus, ExportSettings
    import subprocess
    import tempfile

    work = Path(tempfile.mkdtemp(prefix="corteia-smoke-"))
    src = work / "amostra.mp4"
    lines = [
        "A verdade é que ninguém te conta isso sobre cortar vídeo, mas eu vou contar.",
        "Eu testei quarenta e sete ferramentas diferentes até entender o que importa.",
        "O segredo não é a legenda bonita, é o gancho nos três primeiros segundos.",
        "Então se você quer cortar melhor hoje, anota essa: meça a retenção, não o like.",
    ]
    # Real Portuguese speech (espeak-ng) — a sine tone has no words, and a
    # product smoke test that produces zero clips proves nothing about quality.
    wav = work / "fala.wav"
    text = " ".join(lines)
    spoken = False
    espeak = shutil.which("espeak-ng") or shutil.which("espeak")
    if espeak:
        subprocess.run([espeak, "-v", "pt-br", "-s", "165", "-w", str(wav), text],
                       check=True, capture_output=True)
        spoken = True
    if not spoken:
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                        "-f", "lavfi", "-i", "sine=frequency=320:duration=24",
                        "-c:a", "pcm_s16le", str(wav)], check=True)
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
           "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30:duration=22",
           "-i", str(wav),
           "-filter_complex",
           "[1:a]atrim=0:22,asetpts=PTS-STARTPTS,"
           "volume='if(between(t,9,11),0,1)':eval=frame[a]",
           "-map", "0:v", "-map", "[a]", "-t", "22",
           "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-shortest", str(src)]
    subprocess.run(cmd, check=True)
    print(f"SMOKE: vídeo de teste criado em {src} "
          f"(22 s, 1280x720, fala PT-BR {'real' if spoken else 'sintética'}, "
          f"silêncio 9–11 s)")

    os.environ.setdefault("CORTEIA_PROJECTS_DIR", str(work / "projects"))
    ctx = AppContext()
    ctx.new_project("Smoke")
    opts = AnalysisOptions(target_min=6.0, target_max=18.0, max_clips=4, min_score=5.0,
                           whisper_model="tiny", use_llm=False, remove_silences=True)
    events: list[tuple[float, str]] = []
    clips = ctx.pipeline.analyze(src, opts,
                                 on_progress=lambda f, m: events.append((f, m)))
    for st in ctx.pipeline.stages():
        print(f"  estágio {st['key']:11s} {st['state']:9s} {st['progress'] * 100:5.1f}%  "
              f"{st['detail'][:60]}")
    if not clips:
        print("FALHA: o pipeline não gerou nenhum corte")
        return 1
    for c in clips:
        c.status = ClipStatus.APPROVED
        print(f"  corte {c.start:5.1f}-{c.end:5.1f} ({c.duration:4.1f}s) "
              f"score={c.score.total:5.1f}  {c.title[:50]}")
    st = ctx.project.export_settings
    st.out_dir = str(work / "out")
    st.resolution = st.resolution.__class__.R_720
    st.quality_crf = 30
    st.preset = "ultrafast"
    res = ctx.exporter.export_all()
    for out in res["outputs"]:
        info = json.loads(subprocess.run(["ffprobe", "-v", "error", "-print_format", "json",
                                          "-show_format", "-show_streams", out],
                                         capture_output=True, text=True).stdout)
        v = next(s for s in info["streams"] if s["codec_type"] == "video")
        print(f"  exportado: {Path(out).name} {v['width']}x{v['height']} "
              f"{float(info['format']['duration']):.2f}s "
              f"{int(info['format']['size']) // 1024} KB")
    ok = res["ok"] >= 1 and res["failed"] == 0
    print(f"\nSMOKE {'OK' if ok else 'FALHOU'}: {res['ok']} exportado(s), "
          f"{res['failed']} falha(s), {len(events)} eventos de progresso.")
    print(f"(trabalho em {work}; nada foi apagado para você conferir)")
    return 0 if ok else 1


def cmd_analyze(path: str, as_json: str = "") -> int:
    """Headless analysis: writes the suggested clips as JSON next to the video."""
    from .context import AppContext
    from .models import AnalysisOptions
    src = Path(path)
    if not src.exists():
        print(f"não existe: {src}", file=sys.stderr)
        return 2
    ctx = AppContext()
    ctx.new_project(f"CLI {src.stem}")
    clips = ctx.pipeline.analyze(src, AnalysisOptions())
    payload = {"source": str(src), "clips": [
        {"start": c.start, "end": c.end, "duration": round(c.duration, 2),
         "title": c.title, "description": c.description, "score": c.score.total,
         "reason": c.reason, "keywords": c.keywords} for c in clips]}
    dest = Path(as_json) if as_json else src.with_suffix(".cortes.json")
    dest.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"{len(clips)} cortes em {dest}")
    for c in payload["clips"]:
        print(f"  {c['start']:7.1f}-{c['end']:7.1f} {c['score']:5.1f}  {c['title'][:60]}")
    return 0


def cmd_export(path: str, start: str, dur: str) -> int:
    """Cut and render one stretch without the interface (batch friendly)."""
    from .context import AppContext
    from .models import Clip, ClipStatus
    src = Path(path)
    t0, length = float(start), float(dur)
    ctx = AppContext()
    ctx.new_project(f"CLI {src.stem}")
    clip = Clip(source_path=str(src), start=t0, end=t0 + length,
                title=f"Corte {int(t0)}s", status=ClipStatus.APPROVED)
    ctx.project.clips = [clip]
    ctx.add_source(src)
    out = ctx.exporter.export_one(clip.id)
    print(f"exportado: {out}")
    return 0 if Path(out).exists() else 1


def gui(argv: list[str]) -> int:
    try:
        from .ui.app import main as ui_main
    except ImportError as exc:                       # the UI package is missing
        print(f"Interface indisponível: {exc}\n"
              "Rode `python run.py --check` para ver o diagnóstico.", file=sys.stderr)
        return 3
    return ui_main(argv)


def cmd_smoke_ui(argv: list[str] | None = None) -> int:
    """Build the real window, click through every page, then close.

    This is the check that a packaging mistake (missing .qss, missing icon
    theme, a page that crashes on an empty project) is caught before a human
    opens the app.  On a machine with no display we ask Qt for its offscreen
    platform, so the same command works in CI.
    """
    # Ter DISPLAY não quer dizer que o plugin xcb consiga abrir: em container
    # sem libxcb-cursor o Qt aborta o processo inteiro.  Um smoke de CI nunca
    # deve depender de janela, então offscreen é o padrão aqui.
    if not os.environ.get("QT_QPA_PLATFORM"):
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
        print("usando a plataforma Qt 'offscreen' (smoke sem janela real)")
    try:
        from .ui.app import main as ui_main_smoke
    except Exception as exc:                          # noqa: BLE001
        print(f"interface indisponível: {exc}", file=sys.stderr)
        return 3
    return ui_main_smoke(["--smoke"])


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="corteia", description=__app_name__)
    p.add_argument("--version", action="version", version=f"{__app_name__} {__version__}")
    p.add_argument("-v", "--verbose", action="store_true", help="log debug na tela")
    p.add_argument("--check", action="store_true",
                   help="diagnóstico de FFmpeg, codecs, fontes, GPU e modelos")
    p.add_argument("--smoke", action="store_true",
                   help="roda o pipeline real num vídeo sintético e exporta 1 corte")
    p.add_argument("--smoke-ui", action="store_true",
                   help="abre a janela de verdade, navega pelas páginas e fecha (CI)")
    p.add_argument("--analyze", metavar="VIDEO", help="analisa sem abrir a interface")
    p.add_argument("--json", metavar="SAIDA", default="", help="arquivo JSON do --analyze")
    p.add_argument("--export", nargs=3, metavar=("VIDEO", "INÍCIO", "SEGUNDOS"),
                   help="recorta e renderiza um trecho sem a interface")
    p.add_argument("--min-size", type=int, default=600, metavar="PX",
                   help="tamanho mínimo do recorte (passa para a UI)")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    log_file = _setup_logging(args.verbose)
    if args.min_size:                       # forwarded to the UI when it starts
        os.environ["CORTEIA_MIN_SIZE"] = str(args.min_size)
    try:
        if args.check:
            return cmd_check(sys.argv[1:])
        if args.smoke:
            return cmd_smoke()
        if args.smoke_ui:
            return cmd_smoke_ui(sys.argv[1:])
        if args.analyze:
            return cmd_analyze(args.analyze, args.json)
        if args.export:
            if len(args.export) != 3:
                print("uso: --export VIDEO INÍCIO SEGUNDOS", file=sys.stderr)
                return 2
            return cmd_export(*args.export)
        return gui(sys.argv[1:])
    except KeyboardInterrupt:
        print("\ninterrompido.", file=sys.stderr)
        return 130
    finally:
        logging.shutdown()
        _ = log_file


if __name__ == "__main__":                       # pragma: no cover
    raise SystemExit(main())
