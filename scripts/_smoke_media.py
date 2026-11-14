#!/usr/bin/env python3
"""Smoke test for the CorteIA media core (no network, real FFmpeg).

Run from the project root:

    . .venv/bin/activate && python scripts/_smoke_media.py

It generates real synthetic media with FFmpeg, then exercises probe, thumbnail,
audio_energy_curve, detect_silences, detect_scenes, ffmpeg_cli.run/job/cancel and
the downloader guards (consent/URL validation + option building only).
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import numpy as np                                    # noqa: E402

from corteia.core import ffmpeg_cli, media, scenes, silence  # noqa: E402
from corteia.core import downloader                    # noqa: E402
from corteia.core.errors import (                       # noqa: E402
    CancelledError, MediaError, ProcessingError,
)
from corteia import paths                          # noqa: E402
from corteia.config import get_config                   # noqa: E402
from corteia.models import Silence                      # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="corteia-smoke-", dir="/tmp"))
FAILURES: list[str] = []
RESULTS: list[str] = []


def check(label: str, ok: bool, extra: str = "") -> None:
    line = f"[{'PASS' if ok else 'FAIL'}] {label}{(' — ' + extra) if extra else ''}"
    RESULTS.append(line)
    print(line, flush=True)
    if not ok:
        FAILURES.append(label)


def info(label: str, value: object) -> None:
    print(f"       {label}: {value}", flush=True)


def sh(cmd: list[str]) -> None:
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit(f"fixture generation failed: {' '.join(cmd)}\n{r.stderr[-800:]}")


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #
def make_fixtures() -> dict[str, Path]:
    print("== gerando mídia sintética real com FFmpeg ==", flush=True)
    ff = ffmpeg_cli.ffmpeg_bin()
    t0 = time.time()
    files: dict[str, Path] = {}

    sh([ff, "-hide_banner", "-v", "error", "-y",
        "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30000/1001:duration=12",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=12",
        "-shortest", "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
        "-c:a", "aac", str(TMP / "t1.mp4")])
    files["video"] = TMP / "t1.mp4"

    # tones only in [0,2] [4,7] [9,12] -> silences expected at [2,4] and [7,9]
    sh([ff, "-hide_banner", "-v", "error", "-y",
        "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30:duration=12",
        "-f", "lavfi", "-i", "aevalsrc=0.5*sin(2*PI*440*t)*(between(t\\,0\\,2)+"
        "between(t\\,4\\,7)+between(t\\,9\\,12)):d=12",
        "-shortest", "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
        "-c:a", "aac", str(TMP / "speech.mp4")])
    files["speech"] = TMP / "speech.mp4"

    # 4 hard cuts of 3 s each
    sh([ff, "-hide_banner", "-v", "error", "-y",
        "-f", "lavfi", "-i", "color=c=red:s=640x360:r=25:d=3",
        "-f", "lavfi", "-i", "color=c=green:s=640x360:r=25:d=3",
        "-f", "lavfi", "-i", "color=c=blue:s=640x360:r=25:d=3",
        "-f", "lavfi", "-i", "color=c=yellow:s=640x360:r=25:d=3",
        "-filter_complex", "[0:v][1:v][2:v][3:v]concat=n=4:v=1",
        "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
        str(TMP / "scenes.mp4")])
    files["scenes"] = TMP / "scenes.mp4"

    sh([ff, "-hide_banner", "-v", "error", "-y",
        "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30:duration=6",
        "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(TMP / "noaudio.mp4")])
    files["noaudio"] = TMP / "noaudio.mp4"

    # rotated (display matrix) + one-hour audio track for the memory test
    sh([ff, "-hide_banner", "-v", "error", "-y",
        "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30:duration=4",
        "-metadata:s:v:0", "rotate=90", "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p",
        str(TMP / "rot.mp4")])
    files["rot"] = TMP / "rot.mp4"

    sh([ff, "-hide_banner", "-v", "error", "-y",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=3600",
        "-c:a", "aac", "-b:a", "64k", str(TMP / "long.mp4")])
    files["long"] = TMP / "long.mp4"

    sh([ff, "-hide_banner", "-v", "error", "-y",
        "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=25:duration=45",
        "-f", "lavfi", "-i", "sine=frequency=300:duration=45",
        "-shortest", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
        "-c:a", "aac", str(TMP / "src45.mp4")])
    files["src45"] = TMP / "src45.mp4"

    (TMP / "text.txt").write_text("não sou vídeo", encoding="utf-8")
    files["text"] = TMP / "text.txt"
    info("tempo de geração", f"{time.time() - t0:.1f}s em {TMP}")
    return files


# --------------------------------------------------------------------------- #
# tests
# --------------------------------------------------------------------------- #
def test_probe(files: dict[str, Path]) -> None:
    print("\n== media.probe ==", flush=True)
    info("ffmpeg", ffmpeg_cli.ffmpeg_bin())
    i = media.probe_or_raise(files["video"])
    info("VideoInfo", i)
    check("fps 30000/1001 lido como 29.97", abs(i.fps - 29.97003) < 0.001, f"{i.fps:.5f}")
    check("duração ~12s", abs(i.duration - 12.0) < 0.15, f"{i.duration:.3f}s")
    check("resolução 1280x720", (i.width, i.height) == (1280, 720), i.resolution_display)
    check("has_audio + codecs", i.has_audio and i.codec_video == "h264" and i.codec_audio == "aac",
          f"{i.codec_video}/{i.codec_audio}")
    check("bitrate > 0", i.bitrate > 0, f"{i.bitrate/1_000_000:.2f} Mbps")
    check("todos os campos preenchidos",
          all([i.path, i.duration, i.width, i.height, i.fps, i.codec_video, i.bitrate]),
          str(sorted(i.to_dict())))
    ok, msg = media.is_supported(files["text"])
    check("is_supported(.txt) rejeita", not ok, msg)
    check("is_supported(.mp4) aceita", media.is_supported(files["video"])[0])
    check("container suporta stream copy (h264/mp4)", media.container_supports_stream_copy(files["video"]))
    info("estimate_export_size 1080p crf20 30s", media.estimate_export_size(i, 30, 20, "1080p"))
    info("estimate_export_size 720p crf28 30s ", media.estimate_export_size(i, 30, 28, "720p"))

    # FFmpeg 6.1 here transposes instead of writing a display matrix, so no rotated
    # fixture can be produced locally: the ffprobe payload below is the real JSON
    # shape a phone recording yields, fed at the process boundary.
    for payload, want_rot, want_dims in (
        ({"format": {"duration": "8.400000", "bit_rate": "9100000"}, "streams": [
            {"index": 0, "codec_name": "hevc", "codec_type": "video", "width": 1920, "height": 1080,
             "avg_frame_rate": "30000/1001", "r_frame_rate": "30000/1001", "nb_frames": "252",
             "side_data_list": [{"side_data_type": "Display Matrix", "rotation": -90}],
             "disposition": {"attached_pic": 0}},
            {"index": 1, "codec_name": "aac", "codec_type": "audio"}]}, 270, (1080, 1920)),
        ({"format": {"duration": "6.000000"}, "streams": [
            {"codec_name": "h264", "codec_type": "video", "width": 1280, "height": 720,
             "avg_frame_rate": "25/1", "tags": {"rotate": "270"},
             "disposition": {"attached_pic": 0}}]}, 270, (720, 1280)),
    ):
        media.invalidate_cache()
        real = ffmpeg_cli.run_json
        ffmpeg_cli.run_json = lambda a, _d=payload: _d
        try:
            rot = media.probe(Path("phone.mp4"))
        finally:
            ffmpeg_cli.run_json = real
            media.invalidate_cache()
        info("ffprobe com rotação", f"rotation={rot.rotation} exibição={rot.width}x{rot.height} "
                                    f"fps={rot.fps:.5f} áudio={rot.has_audio}")
        check(f"rotation {want_rot} lida do payload ffprobe", rot.rotation == want_rot, str(rot.rotation))
        check("dimensões de exibição trocadas quando 90/270",
              (rot.width, rot.height) == want_dims, f"{rot.width}x{rot.height}")
    rot_file = media.probe(files["rot"])
    info("arquivo gerado localmente", f"rotation={rot_file.rotation} (build transpõe, não grava matriz)")

    na = media.probe(files["noaudio"])
    check("sem áudio: has_audio False", na.has_audio is False and na.codec_audio == "")
    check("curve vazia sem áudio", media.audio_energy_curve(files["noaudio"])[0].size == 0)

    for bad, why in ((TMP / "missing.mp4", "inexistente"), (files["text"], "sem vídeo")):
        try:
            media.probe_or_raise(Path(bad))
            check(f"probe_or_raise rejeita {why}", False, "nenhuma exceção")
        except MediaError as exc:
            check(f"probe_or_raise rejeita {why}", True, exc.message)

    # probe caching (warm the entry first, the loop above resets it)
    media.probe(files["video"])
    t0 = time.perf_counter()
    media.probe(files["video"])
    cached_ms = (time.perf_counter() - t0) * 1000
    media.invalidate_cache(files["video"])
    t0 = time.perf_counter()
    media.probe(files["video"])
    fresh_ms = (time.perf_counter() - t0) * 1000
    info("probe cache vs fresh", f"{cached_ms:.1f}ms vs {fresh_ms:.1f}ms")
    check("cache de probe acelera a leitura", cached_ms < fresh_ms)


def test_thumbnail(files: dict[str, Path]) -> None:
    print("\n== media.thumbnail / frame_at ==", flush=True)
    t0 = time.perf_counter()
    p1 = media.thumbnail(files["video"], at=6.0, size=480)
    cold_ms = (time.perf_counter() - t0) * 1000
    t0 = time.perf_counter()
    p2 = media.thumbnail(files["video"], at=6.0, size=480)
    warm_ms = (time.perf_counter() - t0) * 1000
    info("arquivo", f"{p1}  ({p1.stat().st_size} bytes, frio {cold_ms:.0f}ms, cache {warm_ms:.0f}ms)")
    check("thumbnail existe e é JPEG", p1.exists() and p1.suffix == ".jpg" and p1.stat().st_size > 1000)
    digest, _, size_tag = p1.stem.rpartition("_")
    check("nome do cache = sha1(path+mtime+at)_{size}.jpg",
          len(digest) == 40 and all(c in "0123456789abcdef" for c in digest)
          and size_tag == "480" and p1.name.endswith(".jpg"), p1.name)
    check("reuso pelo cache", p1 == p2 and warm_ms < cold_ms)
    check("gravada em cache_dir/thumbs", p1.parent.name == "thumbs" and "thumbs" in str(p1.parent),
          str(p1.parent))
    auto = media.thumbnail(files["video"])       # at < 0 -> 10%
    check("at<0 usa 10% do vídeo", auto.exists(), auto.name)
    dest = TMP / "out" / "custom.jpg"
    media.thumbnail(files["video"], at=1.0, size=200, dest=dest)
    check("dest custom funciona", dest.exists() and dest.stat().st_size > 500)

    fr = media.frame_at(files["video"], 3.0, width=320)
    check("frame_at retorna BGR", fr is not None and fr.ndim == 3 and fr.shape[2] == 3
          and fr.shape[1] == 320, None if fr is None else str(fr.shape))
    try:
        media.thumbnail(TMP / "missing.mp4")
        check("thumbnail de arquivo inexistente levanta MediaError", False)
    except MediaError as exc:
        check("thumbnail de arquivo inexistente levanta MediaError", True, exc.message)


def test_curve(files: dict[str, Path]) -> None:
    print("\n== media.audio_energy_curve (streaming) ==", flush=True)
    times, db = media.audio_energy_curve(files["video"], hop=0.1)
    info("12s de vídeo", f"{times.size} quadros, hop={times[1]-times[0]:.3f}s, "
                         f"dB médio={db.mean():.1f}, máx={db.max():.1f}, mín={db.min():.1f}")
    check("curva preenchida (≈120 quadros)", 100 <= times.size <= 130, str(times.size))
    check("tempos cobrem o vídeo", abs(times[0] - 0.05) < 1e-6 and times[-1] <= 12.0,
          f"{times[0]:.3f}..{times[-1]:.3f}")
    ref = astats_rms_db(files["video"])
    info("referência astats", f"RMS {ref:.2f} dBFS x curva {db.mean():.2f} dBFS")
    check("curva confere com o RMS do FFmpeg (astats)", abs(db.mean() - ref) < 0.5,
          f"delta {abs(db.mean() - ref):.3f} dB")
    check("dB em escala dBFS (< 0)", float(db.max()) < 0.0, f"máx {db.max():.1f} dBFS")

    t_sp, db_sp = media.audio_energy_curve(files["speech"], hop=0.1)
    loud = db_sp[t_sp < 1.0]                     # tone playing
    quiet = db_sp[(t_sp > 2.6) & (t_sp < 3.6)]   # silence window
    info("speech.mp4", f"fala {loud.mean():.1f} dBFS × silêncio {quiet.mean():.1f} dBFS")
    check("detecta queda de energia no silêncio", loud.mean() - quiet.mean() > 20.0,
          f"delta {loud.mean()-quiet.mean():.1f} dB")

    import psutil
    proc = psutil.Process()
    before = proc.memory_info().rss
    t0 = time.perf_counter()
    tl, dl = media.audio_energy_curve(files["long"], hop=0.1)
    dt = time.perf_counter() - t0
    peak = proc.memory_info().rss - before
    info("1 hora de áudio", f"{tl.size} quadros em {dt:.1f}s; RSS delta {peak/1e6:.1f} MB; "
                            f"bytes por leitura incremental {int(0.1*8000*4*16)/1024:.0f} KB")
    check("curva de 1 h coberta", 35000 <= tl.size <= 36100, str(tl.size))
    check("memória limitada (< 64 MB)", peak < 64 * 1024 * 1024, f"{peak/1e6:.1f} MB")
    # max_frames widens the hop instead of growing the array unbounded
    th, dh = media.audio_energy_curve(files["long"], hop=0.1, max_frames=1000)
    info("max_frames=1000", f"{th.size} quadros, hop efetivo {th[1]-th[0]:.3f}s, "
                            f"último t={th[-1]:.1f}s")
    check("max_frames protege a curva", th.size <= 1001 and (th[1] - th[0]) > 3.5,
          f"{th.size} quadros @ {th[1]-th[0]:.2f}s")


def test_silence(files: dict[str, Path]) -> None:
    print("\n== silence.detect_silences / cut_points ==", flush=True)
    seen: list[float] = []
    sil = silence.detect_silences(files["speech"], db=-35.0, min_ms=350,
                                  progress=lambda p, m: seen.append(p))
    found = [(round(s.start, 3), round(s.end, 3)) for s in sil]
    expected = [(2.0, 4.0), (7.0, 9.0)]
    info("silêncios", found)
    info("progresso", f"{len(seen)} atualizações, {min(seen):.2f} → {max(seen):.2f}")
    check("2 janelas encontradas", len(sil) == 2, str(found))
    deltas: list[float] = []
    for (ea, eb), s in zip(expected, sil):
        deltas += [abs(s.start - ea), abs(s.end - eb)]
    info("deltas vs. esperado", f"máx {max(deltas)*1000:.0f} ms")
    check("janelas batem com os tempos sintéticos (<80ms)", max(deltas) < 0.08,
          f"{max(deltas)*1000:.0f}ms")
    check("progresso do silencedetect reportado", bool(seen) and max(seen) >= 0.9,
          f"{max(seen):.2f}" if seen else "nenhum")
    check("total_speech = 8s", abs(silence.total_speech(sil, 12.0) - 8.0) < 0.05,
          f"{silence.total_speech(sil, 12.0):.3f}s")
    check("speech_ratio(0,12) = 0.667", abs(silence.speech_ratio(sil, 0, 12) - 2 / 3) < 0.01,
          f"{silence.speech_ratio(sil, 0, 12):.3f}")
    check("speech_ratio dentro do silêncio = 0", silence.speech_ratio(sil, 2.2, 3.8) == 0.0)
    check("inside_silence", silence.inside_silence(3.0, sil) and not silence.inside_silence(0.5, sil))
    cp = silence.cut_points(sil, 0, 12)
    info("cut_points(0,12)", cp)
    check("cut_points devolve FALA", cp == [(0.0, 2.0), (4.0, 7.0), (9.0, 12.0)], str(cp))
    check("cut_points recorta a janela", silence.cut_points(sil, 1, 10) ==
          [(1.0, 2.0), (4.0, 7.0), (9.0, 10.0)], str(silence.cut_points(sil, 1, 10)))
    check("cut_points vazio se a janela é muda", silence.cut_points(sil, 2.5, 3.5) == [])
    many = [Silence(i + 0.5, i + 1.0) for i in range(0, 30)]
    capped = silence.cut_points(many, 0, 30, min_keep=0.35, max_cut=5)
    info("cut_points(max_cut=5)", capped)
    check("max_cut respeitado", len(capped) == 5, str(len(capped)))
    pair = [Silence(0.5, 1.0), Silence(1.1, 2.0)]
    fused = silence.cut_points(pair, 0, 10, min_keep=0.35)
    info("união de silêncios próximos", fused)
    check("silêncios separados por fala curta são unidos",
          fused == [(0.0, 0.5), (2.0, 10.0)], str(fused))
    tiny = [Silence(1.0, 2.0), Silence(3.0, 4.0)]
    kept = silence.cut_points(tiny, 0, 10, min_keep=2.5)
    info("falas < min_keep", kept)
    check("falas < min_keep descartadas", kept == [(4.0, 10.0)], str(kept))
    gp = silence.gaps(sil, 12.0)
    check("gaps() complementa silêncios",
          [(round(a, 1), round(b, 1)) for a, b in gp] == [(0.0, 2.0), (4.0, 7.0), (9.0, 12.0)],
          str(gp))
    check("gaps() descarta fala menor que min_gap",
          silence.gaps([Silence(0.0, 1.0), Silence(1.02, 12.0)], 12.0, min_gap=0.12) == [],
          str(silence.gaps([Silence(0.0, 1.0), Silence(1.02, 12.0)], 12.0, min_gap=0.12)))
    check("sem áudio -> sem silêncios", silence.detect_silences(files["noaudio"]) == [])

    # robustness of the parser itself
    sample = ("[silencedetect @ 0x1] silence_start: 3.5 bitrate=N/A speed=23.8x\n"
              "[silencedetect @ 0x1] silence_end: 5.12 | silence_duration: 1.62\n"
              "[silencedetect @ 0x1] silence_start: 9.8\n")
    parsed = [(s.start, s.end) for s in silence.parse_silencedetect(sample, 10.0)]
    check("parser tolera status grudado e silêncio final aberto",
          parsed == [(3.5, 5.12), (9.8, 10.0)], str(parsed))


def test_scenes(files: dict[str, Path]) -> None:
    print("\n== scenes.detect_scenes ==", flush=True)
    prog: list[float] = []
    sc = scenes.detect_scenes(files["scenes"], threshold=27.0, min_scene_len=15,
                              progress=lambda p, m: prog.append(p))
    got = [(round(s.start, 2), round(s.end, 2)) for s in sc]
    info("4 cortes sintéticos", got)
    info("progresso", f"{len(prog)} updates → {max(prog):.2f}")
    check("4 cenas detectadas", len(sc) == 4, str(got))
    check("limites em 0/3/6/9/12 (±0.15s)",
          all(abs(g[0] - e) < 0.15 and abs(g[1] - e - 3.0) < 0.15
              for g, e in zip(got, (0.0, 3.0, 6.0, 9.0))), str(got))
    check("cobertura contínua", abs(sc[0].start) < 1e-6 and abs(sc[-1].end - 12.0) < 1e-6
          and all(abs(sc[i].end - sc[i + 1].start) < 1e-6 for i in range(len(sc) - 1)))
    one = scenes.detect_scenes(files["video"])
    info("sem cortes", [(round(s.start, 2), round(s.end, 2)) for s in one])
    check("vídeo sem cortes -> 1 cena cobrindo tudo",
          len(one) == 1 and one[0].start == 0.0 and abs(one[0].end - 12.0) < 0.1, str(one))
    check("scenes_in/count", len(scenes.scenes_in(sc, 2.9, 6.1)) == 3
          and scenes.scene_count_in(sc, 2.9, 6.1) == 3)
    check("boundaries()", scenes.boundaries(sc) == [0.0, 3.0, 6.0, 9.0], str(scenes.boundaries(sc)))
    cancel = threading.Event()
    cancel.set()
    try:
        scenes.detect_scenes(files["scenes"], cancel=cancel)
        check("scenes respeita cancel pré-setado", False, "não cancelou")
    except CancelledError:
        check("scenes respeita cancel pré-setado", True)


def test_ffmpeg(files: dict[str, Path]) -> None:
    print("\n== ffmpeg_cli.run / progresso / erro / cancelamento ==", flush=True)
    marks: list[tuple[float, str]] = []
    out = TMP / "encoded.mp4"
    src = media.probe(files["src45"])
    cmd = ffmpeg_cli.encode_cmd(
        [str(files["src45"])], ["-nostdin"], "scale=640:360,fps=30", "aresample=48000", out,
        codec="libx264", crf=26, preset="veryfast", fps=30, audio_bitrate="128k",
        extra=["-movflags", "+faststart"], threads=0,
    )
    info("encode_cmd", " ".join(cmd[:6]) + " ... " + " ".join(cmd[-8:]))
    t0 = time.perf_counter()
    ffmpeg_cli.run(cmd, total_duration=src.duration,
                   on_progress=lambda p, m: marks.append((p, m)), cancel=None)
    dt = time.perf_counter() - t0
    pct = [round(p * 100, 1) for p, _ in marks]
    info("progresso", f"{len(marks)} eventos: {pct[:6]} ... {pct[-3:]}")
    info("mensagens", [m for _, m in marks[:2]] + ["..."] + [marks[-1][1]] if marks else "-")
    check("progresso reportado", len(marks) >= 5, f"{len(marks)} eventos")
    check("progresso avança de 0 a ~100", bool(marks) and marks[0][0] < 0.35 and pct[-1] >= 99.9,
          f"primeiro {pct[0] if pct else None}% → último {pct[-1] if pct else None}%")
    check("progresso monótono", all(b - a >= -1e-9 for a, b in zip(pct, pct[1:])))
    enc = media.probe_or_raise(out)
    info("saída", f"{enc.duration:.3f}s {enc.width}x{enc.height} @{enc.fps:.2f}fps "
                  f"{enc.codec_video}/{enc.codec_audio} em {dt:.2f}s")
    check("arquivo de saída com duração correta", abs(enc.duration - 45.0) < 0.35, f"{enc.duration:.3f}s")
    check("reencode aplicado (640x360@30 + aac 48k)",
          (enc.width, enc.height) == (640, 360) and abs(enc.fps - 30.0) < 0.1
          and enc.codec_audio == "aac")

    # stream copy cut
    cut = TMP / "cut.mp4"
    ffmpeg_cli.run(ffmpeg_cli.copy_cmd([str(files["src45"])], cut, start=5.0, end=11.0),
                   total_duration=6.0)
    c = media.probe(cut)
    info("copy_cmd 5→11s", f"{c.duration:.2f}s, codec={c.codec_video} (sem reencode)")
    check("corte por stream copy com duração exata", abs(c.duration - 6.0) < 0.25, f"{c.duration:.2f}s")
    fastcut = TMP / "cutfast.mp4"
    ffmpeg_cli.run(ffmpeg_cli.copy_cmd([str(files["src45"])], fastcut, start=5.0, end=11.0, fast=True))
    fc_ = media.probe(fastcut)
    info("copy_cmd fast=True 5→11s", f"{fc_.duration:.2f}s (alinhado ao keyframe anterior)")
    check("modo fast corta a partir do keyframe", fc_.duration >= 6.0, f"{fc_.duration:.2f}s")

    # failure path: last stderr lines kept, PT message
    try:
        ffmpeg_cli.run(["ffmpeg", "-hide_banner", "-nostdin", "-i", str(TMP / "nope.mp4"),
                        "-f", "null", "-"])
        check("falha vira MediaError", False, "nenhuma exceção")
    except MediaError as exc:
        n_lines = len(exc.detail.splitlines())
        check("falha vira MediaError com stderr", n_lines >= 2 and "FFmpeg" in exc.message,
              f"{exc.message} | {n_lines} linhas de detalhe")
        check("detalhe limitado a ~15 linhas", n_lines <= 16, f"{n_lines} linhas")
        print("       stderr capturado (primeiras linhas):", flush=True)
        for line in exc.detail.splitlines()[:3]:
            print(f"         | {line}", flush=True)

    # invalid filter -> guaranteed non-zero exit
    try:
        ffmpeg_cli.run(["ffmpeg", "-hide_banner", "-nostdin", "-i", str(files["video"]),
                        "-vf", "this_filter_does_not_exist", "-f", "null", "-"])
        check("filtro inválido levanta erro", False)
    except MediaError as exc:
        check("filtro inválido levanta erro", "Unrecognized option" in exc.detail
              or "does not exist" in exc.detail or "No such" in exc.detail,
              exc.detail.splitlines()[-1][:90] if exc.detail else "")

    # run_json returns a dict
    data = ffmpeg_cli.run_json(["ffprobe", "-v", "error", "-print_format", "json",
                                "-show_format", str(files["video"])])
    check("run_json devolve dict", isinstance(data, dict) and "format" in data,
          f"duration={data['format']['duration']}")

    # parse_progress_line unit checks
    vals = {
        "out_time_ms=6000000 (6s/12s)": ffmpeg_cli.parse_progress_line("out_time_ms=6000000", 12.0),
        "out_time_us=3000000 (3s/12s)": ffmpeg_cli.parse_progress_line("out_time_us=3000000", 12.0),
        "time=00:00:03.00": ffmpeg_cli.parse_progress_line(
            "frame=  100 fps=297 time=00:00:03.00 speed=9.9x", 12.0),
        "frame= + taxa real do stream (100/30fps/10s)": ffmpeg_cli.parse_progress_line(
            "frame=  100 Stream #0:0: Video: h264, 1280x720, 30 fps, 30 tbr", 10.0),
        "frame= isolado (fps= ali é velocidade de encode, não taxa)":
            ffmpeg_cli.parse_progress_line("frame=  300 fps=29.97 size=1KiB", 12.0),
        "sem progresso": ffmpeg_cli.parse_progress_line("progress=continue", 12.0),
    }
    info("parse_progress_line", {k: (None if v is None else round(v, 4)) for k, v in vals.items()})
    check("parse out_time_ms/us/time=/frame=",
          abs(vals["out_time_ms=6000000 (6s/12s)"] - 0.5) < 1e-6
          and abs(vals["out_time_us=3000000 (3s/12s)"] - 0.25) < 1e-6
          and abs(vals["time=00:00:03.00"] - 0.25) < 1e-6
          and abs(vals["frame= + taxa real do stream (100/30fps/10s)"] - 100 / 30 / 10) < 1e-6
          and vals["frame= isolado (fps= ali é velocidade de encode, não taxa)"] is None
          and vals["sem progresso"] is None,
          "frame= isolado não inventa % (fps= da barra é velocidade de codificação)")

    # cancellation of a real encode
    stop = threading.Event()
    err: list[BaseException] = []
    slow = TMP / "slow.mp4"

    def _long_encode() -> None:
        try:
            ffmpeg_cli.run(ffmpeg_cli.encode_cmd(
                [str(files["src45"])], [], None, None, slow, codec="libx264", crf=18,
                preset="veryfast", fps=30, audio_bitrate="192k"),
                total_duration=45.0, cancel=stop, on_progress=lambda p, m: None)
        except BaseException as exc:                          # noqa: BLE001
            err.append(exc)

    th = threading.Thread(target=_long_encode, daemon=True)
    th.start()
    time.sleep(0.7)
    stop.set()
    joined = time.perf_counter()
    th.join(10.0)
    alive_after = subprocess.run(["pgrep", "-f", "slow.mp4"], capture_output=True, text=True)
    n_alive = len([x for x in alive_after.stdout.split() if x.strip()])
    info("cancelamento", f"{type(err[0]).__name__ if err else 'nenhuma exceção'} em "
                         f"{time.perf_counter()-joined:.2f}s após stop.set(); "
                         f"processos ffmpeg ainda vivos para o arquivo: {n_alive}")
    check("cancel levanta CancelledError", bool(err) and isinstance(err[0], CancelledError),
          str(err[:1]))
    check("processo filho morre no cancelamento", n_alive == 0, f"{n_alive} vivos")
    check("threads de stderr não vazam", threading.active_count() <= 6, str(threading.active_count()))

    # FFmpegJob
    job_out = TMP / "job.mp4"
    seen: list[float] = []
    job = ffmpeg_cli.FFmpegJob().start(
        ffmpeg_cli.encode_cmd([str(files["src45"])], [], "scale=320:180", None, job_out,
                              codec="libx264", crf=30, preset="veryfast", fps=25,
                              audio_bitrate="96k"),
        total_duration=45.0, on_progress=lambda p, m: seen.append(p))
    poll = 0
    while job.alive and poll < 600:
        time.sleep(0.05)
        poll += 1
    rc = job.wait(timeout=60)
    info("FFmpegJob", f"rc={rc} alive={job.alive} percent={job.percent:.3f} "
                      f"updates={len(seen)} máx={max(seen):.3f}")
    check("FFmpegJob termina com rc 0 e 100%", rc == 0 and not job.alive and job.percent >= 0.999
          and job_out.exists())
    job2 = ffmpeg_cli.FFmpegJob().start(
        ffmpeg_cli.encode_cmd([str(files["src45"])], [], None, None, TMP / "job2.mp4",
                              codec="libx264", crf=20, preset="veryfast", fps=30,
                              audio_bitrate="192k"), total_duration=45.0)
    time.sleep(0.6)
    job2.cancel()
    try:
        job2.wait(timeout=10)
        check("FFmpegJob.cancel() levanta CancelledError", False, "não cancelou")
    except CancelledError:
        check("FFmpegJob.cancel() levanta CancelledError", True)

    # stderr line streaming through capture
    _, tail = ffmpeg_cli.capture(["ffmpeg", "-hide_banner", "-nostdin", "-i", str(files["video"]),
                                  "-f", "null", "-"])
    check("capture devolve stderr", "Duration:" in tail, tail.splitlines()[0][:70])

    # extract_wav
    wav = media.extract_wav(files["video"], 16000)
    w = media.probe(wav)
    info("extract_wav", f"{wav.name} {w.duration:.2f}s {w.codec_audio} "
                        f"{w.width}x{w.height}")
    check("wav mono 16k pcm_s16le", wav.exists() and w.codec_audio == "pcm_s16le"
          and abs(w.duration - 12.0) < 0.2)
    check("extract_wav reutiliza o cache", media.extract_wav(files["video"], 16000) == wav)

    check("video_url_or_path escapa para filtergraph",
          media.video_url_or_path("clip:name.mp4").endswith(r"clip\:name.mp4") and
          media.video_url_or_path("https://x/y.mp4") == "https://x/y.mp4",
          media.video_url_or_path("a/clip:name.mp4"))


def test_downloader() -> None:
    print("\n== downloader (sem rede) ==", flush=True)
    cfg = get_config()
    original = cfg.get("net.download_consent", False)
    try:
        cfg.set("net.download_consent", False)
        ok, msg = downloader.can_download("https://example.com/v.mp4")
        check("sem consentimento bloqueia", not ok, msg)
        cfg.set("net.download_consent", True)
        ok, msg = downloader.can_download("https://example.com/v.mp4")
        check("com consentimento e link https libera", ok, msg)
        for bad, why in (
            ("file:///etc/passwd", "scheme file"),
            ("ftp://example.com/x", "scheme ftp"),
            ("http://127.0.0.1:8000/a.mp4", "loopback"),
            ("http://localhost/a.mp4", "localhost"),
            ("http://192.168.0.23/a.mp4", "LAN privada"),
            ("http://[::1]/a.mp4", "ipv6 loopback"),
            ("http://nas.local/a.mp4", ".local"),
            ("/home/u/video.mp4", "não é link"),
            ("", "vazio"),
        ):
            ok, msg = downloader.can_download(bad)
            check(f"recusa {why}", not ok, msg)
        opts = downloader.build_opts(paths.temp_sub("downloads"))
        check("opts: formato mp4 <=1080p", opts["format"] ==
              "bv*[height<=1080][ext=mp4]+ba/b[height<=1080]", opts["format"])
        check("opts: merge em mp4, sem legendas",
              opts["merge_output_format"] == "mp4" and opts["writesubtitles"] is False
              and opts["writeautomaticsub"] is False)
        check("opts: destino temp downloads",
              str(paths.temp_root() / "downloads") in opts["outtmpl"], opts["outtmpl"])
        check("opts: progress hook real", len(opts["progress_hooks"]) == 1
              and callable(opts["progress_hooks"][0]))
        # the hook itself, driven with synthetic yt-dlp payloads
        got: list[tuple[float, str]] = []
        opts2 = downloader.build_opts(paths.temp_sub("downloads"),
                                      on_progress=lambda p, m: got.append((p, m)))
        hook = opts2["progress_hooks"][0]
        hook({"status": "downloading", "downloaded_bytes": 250, "total_bytes": 1000,
              "speed": 3_500_000, "eta": 4})
        hook({"status": "downloading", "downloaded_bytes": 1000, "total_bytes_estimate": 1000})
        hook({"status": "finished"})
        info("progress hook", got)
        check("hook converte bytes em 0..1 (com teto antes do merge)",
              abs(got[0][0] - 0.25) < 1e-6 and abs(got[1][0] - 0.94) < 1e-6 and got[-1][0] == 0.95,
              str([round(g[0], 3) for g in got]))
        cancel = threading.Event()
        cancel.set()
        opts3 = downloader.build_opts(paths.temp_sub("downloads"), cancel=cancel)
        try:
            opts3["progress_hooks"][0]({"status": "downloading", "downloaded_bytes": 1,
                                        "total_bytes": 2})
            check("hook cancela quando o evento é setado", False, "nenhuma exceção")
        except Exception as exc:                              # noqa: BLE001
            check("hook cancela quando o evento é setado",
                  type(exc).__name__ == "DownloadCancelled", type(exc).__name__)
        try:
            downloader.download("https://example.com", paths.temp_sub("downloads"))
            check("download exige consentimento antes de qualquer rede", False)
        except ProcessingError as exc:
            cfg.set("net.download_consent", False)
            check("download exige consentimento antes de qualquer rede", True, exc.message)
        check("is_url", downloader.is_url("https://a.b/c") and not downloader.is_url("/tmp/a.mp4"))
    finally:
        cfg.set("net.download_consent", original)


def test_errors() -> None:
    print("\n== errors ==", flush=True)
    e = ProcessingError("render", "Falha ao renderizar", "x264 aborted")
    check("ProcessingError(stage,message,detail)",
          (e.stage, e.message, e.detail) == ("render", "Falha ao renderizar", "x264 aborted"))
    m = MediaError("O arquivo não contém vídeo")
    check("MediaError com mensagem única cai em .message",
          m.message == "O arquivo não contém vídeo" and m.stage == "media", f"{m.stage}/{m.message}")
    check("hierarquia", isinstance(m, ProcessingError) and issubclass(MediaError, ProcessingError)
          and issubclass(CancelledError, Exception))
    check("str(exc) é texto português", "vídeo" in str(m), str(m))


def astats_rms_db(path: Path) -> float:
    """Reference RMS measured by FFmpeg's own astats filter."""
    _, tail = ffmpeg_cli.capture(["ffmpeg", "-hide_banner", "-nostdin", "-i", str(path),
                                  "-af", "astats=metadata=0:reset=0", "-vn", "-f", "null", "-"])
    found = [float(x) for x in re.findall(r"RMS level dB:\s*(-?[\d.]+)", tail)]
    return max(found) if found else 0.0


def main() -> int:
    print(f"python {sys.version.split()[0]} | ffmpeg {ffmpeg_cli.ffmpeg_bin()}", flush=True)
    files = make_fixtures()
    try:
        test_probe(files)
        test_thumbnail(files)
        test_curve(files)
        test_silence(files)
        test_scenes(files)
        test_ffmpeg(files)
        test_downloader()
        test_errors()
    finally:
        if shutilwhich := shutil.which("pgrep"):
            left = subprocess.run([shutilwhich, "-af", "ffmpeg"], capture_output=True, text=True)
            info("processos ffmpeg restantes", len([l for l in left.stdout.splitlines() if l.strip()]))
    print("\n================ RESULTADO ================", flush=True)
    total = len(RESULTS)
    print(f"{total - len(FAILURES)}/{total} verificações passaram", flush=True)
    for f in FAILURES:
        print(f"  FALHOU: {f}", flush=True)
    if not FAILURES:
        shutil.rmtree(TMP, ignore_errors=True)
        print(f"(fixtures removidas: {TMP})", flush=True)
    else:
        print(f"(fixtures mantidas em {TMP})", flush=True)
    return 1 if FAILURES else 0


if __name__ == "__main__":
    raise SystemExit(main())
