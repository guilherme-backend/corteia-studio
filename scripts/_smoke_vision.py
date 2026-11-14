"""Vision-layer smoke tests for CorteIA Studio (``faces.py`` + ``reframe.py``).

Real evidence only: procedural videos are generated with OpenCV, remuxed with
FFmpeg, analysed with the bundled YuNet model, and the produced filtergraphs
are actually executed by FFmpeg. Every check prints real numbers.

Run
---
    python scripts/_smoke_vision.py              # all tests
    python scripts/_smoke_vision.py --skip-perf  # drop the 60 s benchmark
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from corteia.core import faces, reframe                       # noqa: E402
from corteia.models import (AspectRatio, CropPlan, FaceTrackPoint,  # noqa: E402
                             VideoInfo)

TEST_VIDEO = Path("/tmp/faces_test.mp4")
PERF_VIDEO = Path("/tmp/faces_perf.mp4")
NOFACE_VIDEO = Path("/tmp/noface_testsrc2.mp4")

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(f"{name}: {ok} {detail}")
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def ffprobe_json(path: Path) -> dict:
    fp = shutil.which("ffprobe") or "/usr/bin/ffprobe"
    out = subprocess.run([fp, "-v", "error", "-show_format", "-show_streams",
                          "-of", "json", str(path)],
                         capture_output=True, text=True, check=True).stdout
    return json.loads(out)


def video_info(path: Path) -> VideoInfo:
    j = ffprobe_json(path)
    v = next((s for s in j["streams"] if s["codec_type"] == "video"), {})
    num, den = (v.get("r_frame_rate") or "30/1").split("/")
    return VideoInfo(path=str(path), duration=float(j["format"].get("duration", 0) or 0),
                     width=int(v.get("width", 0) or 0), height=int(v.get("height", 0) or 0),
                     fps=float(num) / float(den or 1), codec_video=v.get("codec_name", ""))


def ensure_videos(build_perf: bool = True) -> None:
    import _make_faces_video as gen

    if not TEST_VIDEO.is_file():
        print(f"gerando {TEST_VIDEO} ...")
        print("  " + gen.build(TEST_VIDEO, size=(1280, 720), fps=30.0,
                               duration=12.0)["ffprobe"].replace("\n", " "))
    if build_perf and not PERF_VIDEO.is_file():
        print(f"gerando {PERF_VIDEO} (60 s 1080p) ...")
        print("  " + gen.build(PERF_VIDEO, size=(1920, 1080), fps=30.0,
                               duration=60.0)["ffprobe"].replace("\n", " "))
    if not NOFACE_VIDEO.is_file():
        ff = shutil.which("ffmpeg") or "/usr/bin/ffmpeg"
        subprocess.run([ff, "-y", "-loglevel", "error", "-f", "lavfi",
                        "-i", "testsrc2=size=1280x720:rate=30:duration=10",
                        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                        "-pix_fmt", "yuv420p", "-g", "30", str(NOFACE_VIDEO)],
                       check=True, capture_output=True)


# --------------------------------------------------------------------------- #
# 1 — detection on the real synthetic video
# --------------------------------------------------------------------------- #
def test_1_detection() -> None:
    info = video_info(TEST_VIDEO)
    check("1a vídeo sintético real h264 1280x720 30fps 12s",
          info.width == 1280 and info.height == 720 and info.codec_video == "h264"
          and 11.5 < info.duration <= 12.1,
          f"{info.width}x{info.height} {info.codec_video} {info.fps:.0f}fps "
          f"{info.duration:.2f}s {TEST_VIDEO.stat().st_size / 1e6:.2f} MB")

    cap = cv2.VideoCapture(str(TEST_VIDEO))
    per_frame: list[tuple[float, int, float]] = []
    for t in (0.0, 2.0, 4.0, 6.0, 8.0, 10.0, 11.5):
        cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000)
        ok, frame = cap.read()
        if not ok:
            continue
        found = faces.detect_faces(frame)
        per_frame.append((t, len(found), max((f.score for f in found), default=0.0)))
    cap.release()
    check("1b detect_faces encontra >=1 rosto em cada frame amostrado",
          all(n >= 1 for _, n, _ in per_frame),
          " ".join(f"t={t:.1f}:n={n},sc={s:.2f}" for t, n, s in per_frame))
    both = sum(1 for _, n, _ in per_frame if n == 2)
    check("1c dois rostos simultâneos na maioria dos frames", both >= (len(per_frame) + 1) // 2,
          f"{both}/{len(per_frame)} frames com 2 rostos")

    cap = cv2.VideoCapture(str(TEST_VIDEO))
    cap.set(cv2.CAP_PROP_POS_MSEC, 6200)
    ok, frame = cap.read()
    cap.release()
    found = faces.detect_faces(frame)
    f0 = found[0]
    check("1d Face: landmarks 5x2 + cx/cy/area/aspect",
          f0.landmarks.shape == (5, 2) and f0.area > 0 and 0.5 < f0.aspect < 2.5
          and abs(f0.cx - (f0.x + f0.w / 2)) < 1e-6 and abs(f0.cy - (f0.y + f0.h / 2)) < 1e-6,
          f"box=({f0.x:.0f},{f0.y:.0f},{f0.w:.0f},{f0.h:.0f}) score={f0.score:.3f} "
          f"cx={f0.cx:.1f} cy={f0.cy:.1f} area={f0.area:.0f} aspect={f0.aspect:.3f} "
          f"mouth_lm={f0.landmarks[3].round(1).tolist()}")

    noise = np.random.default_rng(3).integers(0, 255, (720, 1280, 3)).astype(np.uint8)
    check("1e frame sem rosto -> [] sem exceção", faces.detect_faces(noise) == [],
          f"detector={faces.detector_name()!r} available={faces.detector_available()} "
          f"note={faces.detector_note()!r}")

    # the Haar fallback is a guarded code path: OpenCV 5 has no CascadeClassifier
    has_cascade = hasattr(cv2, "CascadeClassifier")
    haar = faces._get_haar()
    check("1f guarda do fallback Haar (cv2.CascadeClassifier)",
          (has_cascade and haar is not None) or (not has_cascade and haar is None),
          f"hasattr(cv2,'CascadeClassifier')={has_cascade} cascade={haar is not None} "
          f"cascata_path={faces._haar_cascade_path()}")
    check("1g min_size filtra rostos minúsculos",
          len(faces.detect_faces(cv2.resize(frame, (320, 180)), min_size=48)) <=
          len(faces.detect_faces(cv2.resize(frame, (320, 180)), min_size=8)),
          "min_size=48 vs min_size=8 em 320x180")

# --------------------------------------------------------------------------- #
# 2 — tracking, visual speech, hysteresis and the 9:16 plan
# --------------------------------------------------------------------------- #
def _assert_plan(plan: CropPlan, info: VideoInfo, aspect: AspectRatio,
                 t0: float, t1: float, tag: str) -> None:
    ar = aspect.value_w_h
    W, H = info.width, info.height
    outside = [p for p in plan.points
               if not (0 <= p[1] and 0 <= p[2] and p[1] + p[3] <= W and p[2] + p[4] <= H)]
    ar_err = max(abs(p[3] / p[4] - ar) for p in plan.points)
    check(f"{tag}1 todo keyframe dentro da fonte (0<=x, x+w<=W) e AR exato",
          not outside and ar_err <= 0.01,
          f"{len(plan.points)} keyframes, AR alvo={ar:.4f}, erro máximo={ar_err:.5f}"
          + (f", FORA={outside[:2]}" if outside else ""))
    ts = [p[0] for p in plan.points]
    gaps = [b - a for a, b in zip(ts, ts[1:])]
    check(f"{tag}2 tempos crescentes cobrindo [t0,t1] sem buraco nem sobreposição",
          ts[0] <= t0 + 1e-6 and ts[-1] >= t1 - 1e-6 and all(g > 0 for g in gaps),
          f"t {ts[0]:.3f}..{ts[-1]:.3f} (janela {t0}..{t1}), vão máximo={max(gaps):.3f}s")
    check(f"{tag}3 no máximo {reframe.MAX_KEYFRAMES} keyframes",
          len(plan.points) <= reframe.MAX_KEYFRAMES, f"{len(plan.points)} keyframes")


def test_2_track_and_plan():
    info = video_info(TEST_VIDEO)
    prog: list[float] = []
    msgs: list[str] = []

    def on_progress(frac: float, msg: str) -> None:
        prog.append(frac)
        if msg not in msgs:
            msgs.append(msg)

    t0 = time.perf_counter()
    pts = faces.analyze_clip_track(TEST_VIDEO, info, 2.0, 10.0, sample_fps=6.0,
                                   on_progress=on_progress)
    dt = time.perf_counter() - t0
    tracks = sorted({p.track for p in pts})
    cx_all = [p.cx for p in pts]
    sp_all = [p.speaking for p in pts]
    by_track = {tr: [p for p in pts if p.track == tr] for tr in tracks}
    print(f"  analyze_clip_track: {len(pts)} pontos em {dt:.2f}s | tracks={tracks} | "
          f"cx [{min(cx_all):.3f} .. {max(cx_all):.3f}] | "
          f"speaking [{min(sp_all):.3f} .. {max(sp_all):.3f}]")
    for tr, group in by_track.items():
        print(f"    track {tr}: n={len(group)} "
              f"cx[{min(g.cx for g in group):.3f}..{max(g.cx for g in group):.3f}] "
              f"w={statistics_median(g.w for g in group):.3f} "
              f"score med={statistics_median(g.score for g in group):.3f} "
              f"speaking med={statistics_median(g.speaking for g in group):.3f} "
              f"max={max(g.speaking for g in group):.3f}")
    check("2a analyze_clip_track produziu >=2 tracks no vídeo de 2 pessoas",
          len(pts) >= 20 and len(tracks) >= 2, f"{len(pts)} pontos, {len(tracks)} tracks")
    check("2b progresso 0->1 monotônico com texto em português",
          bool(prog) and prog[0] > 0 and abs(prog[-1] - 1.0) < 1e-6
          and all(b >= a - 1e-9 for a, b in zip(prog, prog[1:]))
          and any("rostos" in m for m in msgs),
          f"{len(prog)} callbacks, mensagens={msgs}")

    early = {tr: statistics_median(g.speaking for g in by_track[tr] if 2.0 <= g.t < 4.8)
             for tr in tracks if any(2.0 <= g.t < 4.8 for g in by_track[tr])}
    late = {tr: statistics_median(g.speaking for g in by_track[tr] if 6.0 < g.t <= 10.0)
            for tr in tracks if any(6.0 < g.t <= 10.0 for g in by_track[tr])}
    print(f"  speaking mediano por track: 2–4,8s={ {k: round(v, 3) for k, v in early.items()} } "
          f"6–10s={ {k: round(v, 3) for k, v in late.items()} }")
    speaker_early = max(early, key=lambda k: early[k])
    speaker_late = max(late, key=lambda k: late[k])
    check("2c speaking mede fala real: o falante ativo troca de track",
          speaker_early != speaker_late and late[speaker_late] >= 0.5
          and early[speaker_early] >= 0.5
          and late.get(speaker_early, 1.0) < early[speaker_early],
          f"fala 2–4,8s = track {speaker_early} ({early[speaker_early]:.3f}) vs "
          f"6–10s = track {speaker_late} ({late[speaker_late]:.3f}); "
          f"o track {speaker_early} cai para {late.get(speaker_early, float('nan')):.3f}")

    pts2 = faces.analyze_clip_track(TEST_VIDEO, info, 2.0, 10.0, sample_fps=6.0)
    same = len(pts) == len(pts2) and all(
        a.track == b.track and abs(a.cx - b.cx) < 1e-9 and abs(a.speaking - b.speaking) < 1e-9
        for a, b in zip(pts, pts2))
    check("2d determinístico: duas execuções idênticas", same,
          f"{len(pts)} vs {len(pts2)} pontos, igual={same}")

    ev = threading.Event()
    hits = {"n": 0}

    def cancel_at_3(frac: float, _msg: str) -> None:
        hits["n"] += 1
        if hits["n"] >= 3:
            ev.set()

    try:
        faces.analyze_clip_track(TEST_VIDEO, info, 2.0, 10.0, sample_fps=6.0,
                                 on_progress=cancel_at_3, cancel=ev)
        check("2e cancel() interrompe a análise com CancelledError", False, "não levantou exceção")
    except faces.CancelledError:
        check("2e cancel() interrompe a análise com CancelledError", True,
              f"parou após {hits['n']} amostras de {len(_times(2.0, 10.0, 6.0))}")

    merged = faces.merge_tracks(pts)
    steps = [abs(b.cx - a.cx) for a, b in zip(merged, merged[1:]) if a.track == b.track]
    jitter = [abs(b.cx - a.cx) for a, b in zip(pts, pts[1:]) if a.track == b.track]
    check("2f merge_tracks limita o passo por amostra (anti-jitter)",
          len(merged) == len(pts) and (not steps or max(steps) <= faces.MAX_STEP_NORM + 1e-9),
          f"passo máx após merge={max(steps) if steps else 0:.4f} "
          f"(limite {faces.MAX_STEP_NORM}, bruto={max(jitter) if jitter else 0:.4f})")

    sel = reframe._select_track(sorted(pts, key=lambda p: (p.t, p.track)), True, [])
    seq = [s.track for s in sel]
    switches = sum(1 for a, b in zip(seq, seq[1:]) if a != b)
    first_switch = next((s.t for a, b, s in zip(seq, seq[1:], sel[1:]) if a != b), None)
    check("2g histerência: exatamente 1 troca de falante, nunca antes de 0,6 s de vantagem",
          switches == 1 and first_switch is not None and first_switch > 5.0,
          f"trocas={switches} em t={first_switch}; sequência={seq[:6]}...{seq[-4:]}")

    ghost = faces.analyze_clip_track(Path("/tmp/does_not_exist.mp4"), info, 0.0, 3.0)
    check("2h arquivo ilegível -> [] (sem exceção)", ghost == [], f"retornou {len(ghost)} pontos")

    jittery = []
    for i in range(20):
        cx = 0.30 + 0.01 * i + (0.02 if i % 3 == 1 else -0.015 if i % 3 == 2 else 0.0)
        jittery.append(FaceTrackPoint(t=i / 6.0, cx=cx, cy=0.5, w=0.1, score=0.7,
                                      speaking=0.5, track=0))
    smooth = faces.merge_tracks(jittery)
    raw_steps = [abs(b.cx - a.cx) for a, b in zip(jittery, jittery[1:])]
    out_steps = [abs(b.cx - a.cx) for a, b in zip(smooth, smooth[1:])]
    check("2f2 merge_tracks remove o jitter sintético", max(out_steps) <= faces.MAX_STEP_NORM
          and max(out_steps) < max(raw_steps),
          f"passo bruto máx={max(raw_steps):.4f} -> suavizado máx={max(out_steps):.4f}")

    plan = reframe.plan_crop(pts, info, AspectRatio.VERTICAL_9_16, 2.0, 10.0)
    print(f"  plan_crop 9:16: mode={plan.mode} faces_used={plan.faces_used} "
          f"keyframes={len(plan.points)}")
    print(f"  note={plan.note!r}")
    for kf in plan.points:
        print(f"    t={kf[0]:6.2f}  x={kf[1]:4d}  y={kf[2]:4d}  w={kf[3]}  h={kf[4]}  "
              f"ar={kf[3] / kf[4]:.4f}")
    _assert_plan(plan, info, AspectRatio.VERTICAL_9_16, 2.0, 10.0, "2h3")
    check("2i plano registra a troca de falante na nota", "mudou para a trilha" in plan.note,
          f"note={plan.note!r}")
    segs = reframe.segments_of(plan)
    check("2j segments_of cobre [t0,t1] contíguo com geometria única",
          bool(segs) and abs(segs[0][0] - 2.0) < 1e-6 and abs(segs[-1][1] - 10.0) < 1e-6
          and all(abs(a[1] - b[0]) < 1e-9 for a, b in zip(segs, segs[1:]))
          and len({(s[4], s[5]) for s in segs}) == 1,
          f"{len(segs)} segmentos {segs[0][4]}x{segs[0][5]}, "
          f"primeiro=({segs[0][0]:.2f},{segs[0][1]:.2f}) último=({segs[-1][0]:.2f},{segs[-1][1]:.2f})")
    return pts, info, plan


def statistics_median(values) -> float:
    import statistics

    vals = [float(v) for v in values]
    return statistics.median(vals) if vals else 0.0


def _times(t0: float, t1: float, fps: float, duration: float = 12.0) -> list[float]:
    return faces._sample_times(t0, t1, fps, duration)


# --------------------------------------------------------------------------- #
# 3 — no face at all
# --------------------------------------------------------------------------- #
def test_3_noface() -> None:
    info = video_info(NOFACE_VIDEO)
    pts = faces.analyze_clip_track(NOFACE_VIDEO, info, 1.0, 9.0, sample_fps=4.0)
    plan = reframe.plan_crop(pts, info, AspectRatio.VERTICAL_9_16, 1.0, 9.0)
    check("3a testsrc2 sem rosto -> 0 pontos e modo saliency/center",
          pts == [] and plan.mode in ("saliency", "center"),
          f"pontos={len(pts)} mode={plan.mode} faces_used={plan.faces_used} note={plan.note!r}")
    _assert_plan(plan, info, AspectRatio.VERTICAL_9_16, 1.0, 9.0, "3b")
    boxes = {(p[1], p[2], p[3], p[4]) for p in plan.points}
    check("3c nenhum movimento inventado: plano estático", len(boxes) == 1,
          f"boxes distintos={len(boxes)} -> {sorted(boxes)}")

    r = reframe.saliency_center(NOFACE_VIDEO, info, 1.0, 9.0, AspectRatio.VERTICAL_9_16)
    t0 = time.perf_counter()
    r2 = reframe.saliency_center(NOFACE_VIDEO, info, 1.0, 9.0, AspectRatio.VERTICAL_9_16)
    cached = time.perf_counter() - t0
    check("3d saliency real (Sobel) com cache por janela",
          r is not None and r == r2 and cached < 0.01,
          f"centro={r} 2ª chamada={cached * 1000:.3f} ms cache={len(reframe._SALIENCY_CACHE)}")

    pm = reframe.plan_crop(pts, info, AspectRatio.SQUARE_1_1, 1.0, 9.0, manual="300,100,500,500")
    check("3e manual -> modo manual com AR 1:1 e dentro da fonte",
          pm.mode == "manual" and abs(pm.points[0][3] / pm.points[0][4] - 1.0) <= 0.01
          and pm.points[0][1] + pm.points[0][3] <= info.width,
          f"{pm.points[0]} note={pm.note!r}")
    bad = reframe.plan_crop(pts, info, AspectRatio.SQUARE_1_1, 1.0, 9.0, manual="x,y")
    check("3f manual inválido cai no fallback e avisa", bad.mode in ("saliency", "center")
          and "inválido" in bad.note, f"mode={bad.mode} note={bad.note!r}")
    pz = reframe.plan_crop(pts, info, AspectRatio.PORTRAIT_4_5, 1.0, 9.0, zoom=2.0)
    _assert_plan(pz, info, AspectRatio.PORTRAIT_4_5, 1.0, 9.0, "3g")


# --------------------------------------------------------------------------- #
# 4 — compilation
# --------------------------------------------------------------------------- #
def test_4_compile() -> None:
    for rel in ("corteia/core/faces.py", "corteia/core/reframe.py"):
        p = subprocess.run([sys.executable, "-m", "py_compile", str(ROOT / rel)],
                           capture_output=True, text=True)
        check(f"4 python -m py_compile {rel}", p.returncode == 0,
              (p.stderr or "sem erros").strip()[:220])


# --------------------------------------------------------------------------- #
# 5 — performance on 60 s of 1080p
# --------------------------------------------------------------------------- #
def test_5_perf() -> None:
    info = video_info(PERF_VIDEO)
    cap = cv2.VideoCapture(str(PERF_VIDEO))
    cap.set(cv2.CAP_PROP_POS_MSEC, 12000)
    ok, frame = cap.read()
    n = len(faces.detect_faces(frame)) if ok else 0
    cap.release()
    print(f"  vídeo: {info.width}x{info.height} {info.duration:.1f}s "
          f"({PERF_VIDEO.stat().st_size / 1e6:.1f} MB), rostos em t=12s: {n}")
    t0 = time.perf_counter()
    pts = faces.analyze_clip_track(PERF_VIDEO, info, 0.0, 60.0, sample_fps=6.0)
    dt = time.perf_counter() - t0
    samples = len(_times(0.0, 60.0, 6.0, info.duration))
    check("5 analyze_clip_track de 60 s a sample_fps=6 termina em < 60 s", dt < 60.0,
          f"{dt:.2f}s wall para {samples} amostras -> {dt / samples * 1000:.0f} ms/amostra; "
          f"{len(pts)} pontos, {len({p.track for p in pts})} tracks; "
          f"detector limitado a {faces._max_side()} px")
    plan = reframe.plan_crop(pts, info, AspectRatio.VERTICAL_9_16, 10.0, 50.0)
    _assert_plan(plan, info, AspectRatio.VERTICAL_9_16, 10.0, 50.0, "5b")
    print(f"  plano 40 s: mode={plan.mode} keyframes={len(plan.points)} "
          f"tamanho={plan.points[0][3]}x{plan.points[0][4]}")


# --------------------------------------------------------------------------- #
# 6 — the generated filter strings really run in FFmpeg
# --------------------------------------------------------------------------- #
def test_6_ffmpeg(plan: CropPlan) -> None:
    ff = shutil.which("ffmpeg") or "/usr/bin/ffmpeg"
    out_w, out_h, fps = 1080, 1920, 30.0

    def run(filt: str, out: Path, *, complex_: bool, ss: float, dur: float) -> tuple[int, Path]:
        cmd = [ff, "-y", "-loglevel", "error", "-ss", f"{ss:.3f}", "-t", f"{dur:.3f}",
               "-i", str(TEST_VIDEO)]
        cmd += (["-filter_complex", filt, "-map", "[outv]"] if complex_ else ["-vf", filt])
        cmd += ["-an", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "32", str(out)]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            print("    ffmpeg:", (r.stderr or "").strip()[-700:])
        return r.returncode, out

    def size(path: Path) -> tuple[int, int, float]:
        j = ffprobe_json(path)
        v = next((s for s in j["streams"] if s["codec_type"] == "video"), {})
        return int(v.get("width", 0)), int(v.get("height", 0)), float(j["format"]["duration"])

    dur = plan.points[-1][0] - plan.points[0][0]
    graph = reframe.ffmpeg_reframe_filter(plan, out_w, out_h, fps, mode="filtergraph")
    rc, out = run(graph, Path("/tmp/vision_segments.mp4"), complex_=True,
                  ss=plan.points[0][0], dur=dur)
    w, h, d = (0, 0, 0.0)
    if rc == 0:
        w, h, d = size(out)
    segs = reframe.segments_of(plan)
    check("6a filtergraph de segmentos (+concat) executado por FFmpeg",
          rc == 0 and (w, h) == (out_w, out_h) and abs(d - dur) < 0.4,
          f"{len(segs)} segmentos -> {w}x{h} {d:.2f}s (esperado {dur:.2f}s) em {out}")

    crop_f = reframe.ffmpeg_reframe_filter(plan, out_w, out_h, fps, mode="crop")
    rc2, out2 = run(crop_f, Path("/tmp/vision_crop.mp4"), complex_=False,
                    ss=plan.points[0][0], dur=dur)
    w2, h2, d2 = (0, 0, 0.0)
    if rc2 == 0:
        w2, h2, d2 = size(out2)
    check("6b modo 'crop' (-vf estático) executado", rc2 == 0 and (w2, h2) == (out_w, out_h),
          f"{w2}x{h2} {d2:.2f}s filtro={crop_f[:70]}")

    small = CropPlan(points=[(0.0, 100, 0, 405, 720), (1.0, 200, 0, 405, 720),
                             (2.0, 300, 0, 405, 720)], mode="face")
    expr = reframe.ffmpeg_reframe_filter(small, out_w, out_h, fps, mode="expression")
    rc3, out3 = run(expr, Path("/tmp/vision_expr.mp4"), complex_=False, ss=0.0, dur=2.0)
    check("6c modo 'expression' (if/between com vírgulas escapadas) executado",
          rc3 == 0 and out3.is_file(), f"rc={rc3} filtro={expr[:130]}…")

    blur = reframe.blur_fill_filter(out_w, out_h)
    rc4, out4 = run(f"[0:v]{blur}[outv]", Path("/tmp/vision_blur.mp4"), complex_=True,
                    ss=0.0, dur=2.0)
    w4, h4, d4 = (0, 0, 0.0)
    if rc4 == 0:
        w4, h4, d4 = size(out4)
    check("6d blur_fill_filter executado (fundo desfocado 16:9 -> 9:16)",
          rc4 == 0 and (w4, h4) == (out_w, out_h), f"{w4}x{h4} {d4:.2f}s rc={rc4}")

    # a no-face (single segment) plan must also be renderable
    ninfo = video_info(NOFACE_VIDEO)
    nplan = reframe.plan_crop([], ninfo, AspectRatio.VERTICAL_9_16, 2.0, 5.0)
    ngraph = reframe.ffmpeg_reframe_filter(nplan, out_w, out_h, fps)
    rc5, out5 = run(ngraph, Path("/tmp/vision_single.mp4"), complex_=True, ss=2.0, dur=3.0)
    w5, h5, d5 = (0, 0, 0.0)
    if rc5 == 0:
        w5, h5, d5 = size(out5)
    check("6f plano de 1 segmento (sem rosto) também é renderizável",
          len(reframe.segments_of(nplan)) == 1 and rc5 == 0 and (w5, h5) == (out_w, out_h)
          and abs(d5 - 3.0) < 0.3,
          f"mode={nplan.mode} segs={len(reframe.segments_of(nplan))} -> {w5}x{h5} {d5:.2f}s")

    # interpolated box at an arbitrary time must also stay legal
    x, y, w_, h_ = reframe.crop_expr_at(plan, (plan.points[0][0] + plan.points[-1][0]) / 2.0)
    check("6e crop_expr_at interpola e respeita o AR",
          abs(w_ / h_ - AspectRatio.VERTICAL_9_16.value_w_h) <= 0.01 and x >= 0 and w_ + x <= 1280,
          f"t médio -> ({x},{y},{w_},{h_}) ar={w_ / h_:.4f}")


# --------------------------------------------------------------------------- #
# 7 — aspect table
# --------------------------------------------------------------------------- #
def test_7_ar_table() -> None:
    for (sw, sh) in ((1920, 1080), (1080, 1920), (1000, 1000)):
        for ar in AspectRatio:
            w, h = reframe.crop_for_aspect(sw, sh, ar)
            x, y, ww, hh = reframe.static_center_crop(sw, sh, ar)
            ok = (ww == w and hh == h and abs(w / h - ar.value_w_h) <= 0.01
                  and w <= sw and h <= sh and x + w == sw - x and y + h == sh - y)
            check(f"7 {sw}x{sh} -> {ar.value}", ok,
                  f"{w}x{h} ar={w / h:.4f} alvo={ar.value_w_h:.4f} centro=({x},{y}) "
                  f"blur_necessario={reframe.needs_blur_fill(sw, sh, ar)}")


# --------------------------------------------------------------------------- #
def main() -> int:
    skip_perf = "--skip-perf" in sys.argv
    print("== vídeos reais de teste ==")
    ensure_videos(build_perf=not skip_perf)
    print(f"\n== 1) detecção (YuNet backend={faces.detector_name()!r}, "
          f"OpenCV {cv2.__version__}, numpy {np.__version__}) ==")
    test_1_detection()
    print("\n== 2) trilha, fala visual, histerência e plano 9:16 ==")
    _pts, _info, plan = test_2_track_and_plan()
    print("\n== 3) vídeo sem rosto (testsrc2) ==")
    test_3_noface()
    print("\n== 4) compilação ==")
    test_4_compile()
    if skip_perf:
        print("\n== 5) performance: pulado (--skip-perf) ==")
    else:
        print("\n== 5) performance ==")
        test_5_perf()
    print("\n== 6) filtros executados de verdade pelo FFmpeg ==")
    test_6_ffmpeg(plan)
    print("\n== 7) tabela de proporções ==")
    test_7_ar_table()
    print(f"\n===== {len(PASS)} PASS / {len(FAIL)} FAIL =====")
    for f in FAIL:
        print("  FAIL:", f)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
