#!/usr/bin/env python3
"""MATRIZ COMPLETA de exportacao do CorteIA Studio.

Roda exportacoes reais pelos caminhos publicos do nucleo (Exporter / Pipeline /
render) e mede cada saida com ffprobe + teste de integridade + medicao objetiva
de legenda em ROI. Nao edita nada em corteia/.

Saida: media-test/out-matrix/*.mp4 e media-test/out-matrix/_results.json
Uso:   . .venv/bin/activate && python scripts/_matrix_export.py
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import statistics
import subprocess
import sys
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
OUT = ROOT / "media-test" / "out-matrix"
OUT.mkdir(parents=True, exist_ok=True)
RESULTS = OUT / "_results.json"
GRAPH_DIR = Path("/tmp/corteia-studio/graphs")
BUDGET_S = 21 * 60.0

from corteia.context import AppContext                          # noqa: E402
from corteia.models import (                                    # noqa: E402
    AnalysisOptions, AspectRatio, Clip, ClipEdit, ClipStatus, Resolution,
    SubtitleStyle,
)
from corteia.core import media, pipeline as PL, render as RD     # noqa: E402

PODCAST = str(ROOT / "media-test" / "podcast_pt.mp4")
ROSTO = "/tmp/rosto_fala.mp4"
MUSIC = "/tmp/musica.mp3"
# Fontes estaveis: outro agente em sandbox compartilhado renomeou
# media-test/podcast_pt.mp4 -> podcast_pt.corrupt e /tmp/rosto_fala.mp4 ->
# /tmp/rosto_fala.corrupt no meio do trabalho. Copias byte-identicas (md5
# conferido) ficam em _src/ para a matriz rodar contra material imutavel.
SRC_DIR = OUT / "_src"
SRC_DIR.mkdir(parents=True, exist_ok=True)


def _stable(name: str, candidates: list[str]) -> str:
    dst = SRC_DIR / name
    for c in candidates:
        p = Path(c)
        if p.exists() and p.stat().st_size > 0:
            if not (dst.exists() and dst.stat().st_size == p.stat().st_size):
                import shutil
                shutil.copy2(str(p), str(dst))
            return str(dst)
    return candidates[0]


START = time.monotonic()
ADJUST: list[str] = []
CAP_WINDOW = 20.0            # encolhe automaticamente se o render estiver lento

PODCAST = _stable("podcast_pt.mp4", [str(SRC_DIR / "podcast_pt.mp4"),
                                     str(ROOT / "media-test" / "podcast_pt.mp4"),
                                     str(ROOT / "media-test" / "podcast_pt.corrupt")])
ROSTO = _stable("rosto_fala.mp4", [str(SRC_DIR / "rosto_fala.mp4"), "/tmp/rosto_fala.mp4",
                                   "/tmp/rosto_fala.corrupt"])
SOURCES = {"podcast": PODCAST, "rosto": ROSTO}

# ----------------------------------------------------------------- logging
class Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.lines.append(f"{record.levelname} {record.name}: {record.getMessage()}")
        except Exception:                                        # noqa: BLE001
            pass


CAP = Capture()
for _n in ("corteia", "corteia.subs", "corteia.export", "corteia.faces",
           "corteia.render", "corteia.media", "corteia.reframe", "corteia.silence"):
    lg = logging.getLogger(_n)
    lg.setLevel(logging.DEBUG)
    lg.handlers = [CAP]
    lg.propagate = False
logging.getLogger("faster_whisper").setLevel(logging.WARNING)
logging.getLogger("pyscenedetect").setLevel(logging.WARNING)


def log_tail(pat: str, last: int = 6) -> list[str]:
    rx = re.compile(pat, re.I)
    return [ln for ln in CAP.lines if rx.search(ln)][-last:]


# ----------------------------------------------------------------- helpers
def sh(cmd: list[str], timeout: int = 240) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def ffprobe(path: str | Path) -> dict:
    r = sh(["ffprobe", "-v", "error", "-print_format", "json",
            "-show_format", "-show_streams", str(path)])
    d = json.loads(r.stdout or "{}")
    st = d.get("streams", [])
    v = next((s for s in st if s.get("codec_type") == "video"), {})
    a = [s for s in st if s.get("codec_type") == "audio"]
    fmt = d.get("format", {})
    h = int(v.get("height") or 0)
    w = int(v.get("width") or 0)
    p = Path(path)
    return {"w": w, "h": h,
            "duration": round(float(fmt.get("duration") or 0.0), 3),
            "vcodec": v.get("codec_name", ""), "acodec": a[0].get("codec_name", "") if a else "",
            "audio_streams": len(a), "nb_streams": len(st),
            "bitrate_kbps": round(float(fmt.get("bit_rate") or 0) / 1000, 1),
            "fps": v.get("avg_frame_rate", ""),
            "size": int(fmt.get("size") or (p.stat().st_size if p.exists() else 0))}


def integrity(path: str | Path) -> dict:
    r = sh(["ffmpeg", "-v", "error", "-i", str(path), "-f", "null", "-"], timeout=300)
    err = (r.stderr or "").strip()
    return {"ok": r.returncode == 0 and not err, "stderr": err[:300]}


def md5(path: str | Path) -> str:
    return hashlib.md5(Path(path).read_bytes()).hexdigest()[:16]


def graph_snapshot() -> set[str]:
    return set(GRAPH_DIR.glob("*.txt")) if GRAPH_DIR.exists() else set()


def graph_new(before: set[str]) -> dict:
    new = sorted(graph_snapshot() - before, key=lambda p: p.stat().st_mtime)
    text = "\n".join(p.read_text(encoding="utf-8", errors="replace") for p in new)
    return {"n_files": len(new), "chars": len(text), "amix": "amix" in text,
            "sidechaincompress": "sidechaincompress" in text,
            "ass_filter": ("ass=" in text or "subtitles=" in text),
            "head": text[:700]}


def extract_frame(path: str, t: float, dest: Path) -> Path | None:
    sh(["ffmpeg", "-v", "error", "-y", "-ss", f"{t:.3f}", "-i", str(path),
        "-frames:v", "1", str(dest)], timeout=120)
    return dest if dest.exists() and dest.stat().st_size > 0 else None


BANDS = ((0.06, 0.22, "top"), (0.40, 0.62, "center"), (0.70, 0.88, "bottom"))


def roi_diff(a: Path, b: Path) -> dict:
    """|burn - noburn| medio, por banda horizontal (x 0.06-0.94) e no quadro todo."""
    import cv2
    import numpy as np
    ia, ib = cv2.imread(str(a)), cv2.imread(str(b))
    if ia is None or ib is None:
        return {"error": "quadro ilegivel"}
    if ia.shape != ib.shape:
        ib = cv2.resize(ib, (ia.shape[1], ia.shape[0]))
    diff = np.abs(ia.astype(np.float32) - ib.astype(np.float32))
    h, w = diff.shape[:2]
    x0, x1 = int(w * 0.06), int(w * 0.94)
    bands = []
    for lo, hi, name in BANDS:
        roi = diff[int(h * lo):int(h * hi), x0:x1]
        bands.append({"band": name, "y": [lo, hi],
                      "mean_abs": round(float(roi.mean()), 3),
                      "gt40_pct": round(float((roi > 40).mean() * 100.0), 2)})
    hot = max(bands, key=lambda b: b["mean_abs"])
    return {"frame": f"{w}x{h}", "full_mean_abs": round(float(diff.mean()), 3),
            "bands": bands, "hot_band": hot["band"], "hot_mean_abs": hot["mean_abs"]}


def volumedetect(path: str) -> dict:
    r = sh(["ffmpeg", "-v", "info", "-i", str(path), "-af", "volumedetect",
            "-f", "null", "-"], timeout=240)
    txt = r.stderr or ""
    m = re.search(r"mean_volume:\s*(-?[\d.]+)\s*dB", txt)
    x = re.search(r"max_volume:\s*(-?[\d.]+)\s*dB", txt)
    return {"mean_db": float(m.group(1)) if m else None,
            "max_db": float(x.group(1)) if x else None}


class Opts:
    """Objeto de opcoes para crop_plan_for (nomes que o nucleo le)."""

    def __init__(self, follow: bool = True) -> None:
        self.follow_speaker = follow
        self.zoom = 1.0
        self.offset_x = 0.0
        self.offset_y = 0.0
        self.manual_crop = ""


# ----------------------------------------------------------------- contexto
ctx = AppContext()
ctx.new_project("Matriz")
SET = ctx.project.export_settings
SET.out_dir = str(OUT)
SET.resolution = Resolution.R_1080
SET.aspect = AspectRatio.VERTICAL_9_16
SET.fps = 30
SET.quality_crf = 20
SET.preset = "fast"
SET.burn_subtitles = True
SET.filename_pattern = "{index:02d}-{title}-corteia"
PARALLEL = int(ctx.config.int_("proc.max_parallel_jobs") or 2)
SET.max_parallel = PARALLEL

ANALYZE: dict[str, dict] = {}
t = time.monotonic()
clips_pod = ctx.pipeline.analyze(PODCAST, AnalysisOptions(
    whisper_model="base", target_min=6.0, target_max=20.0, max_clips=3, min_score=0.0))
ANALYZE["podcast"] = {"seconds": round(time.monotonic() - t, 1),
                      "clips": [[round(c.start, 2), round(c.end, 2), round(c.duration, 2),
                                 round(c.score.total, 1)] for c in clips_pod],
                      "log": log_tail(r"transcri|RTF|sil|cena|facial", 12)}
t = time.monotonic()
clips_ros = ctx.pipeline.analyze(ROSTO, AnalysisOptions(
    whisper_model="base", target_min=6.0, target_max=20.0, max_clips=3, min_score=0.0))
ANALYZE["rosto"] = {"seconds": round(time.monotonic() - t, 1),
                    "clips": [[round(c.start, 2), round(c.end, 2), round(c.duration, 2),
                               round(c.score.total, 1)] for c in clips_ros],
                    "log": log_tail(r"transcri|RTF|facial|track", 8)}
print("ANALYSIS", json.dumps(ANALYZE, ensure_ascii=False)[:1200], flush=True)
if not clips_pod or not clips_ros:
    sys.exit("FATAL: analise nao retornou cortes")

SRC_POD = Path(clips_pod[0].source_path)
SRC_ROS = Path(clips_ros[0].source_path)
BASE_LO, BASE_HI = float(clips_pod[0].start), float(clips_pod[0].end)
SRC_DUR = float(media.probe(SRC_POD).duration)
ROS_LO, ROS_HI = float(clips_ros[0].start), float(clips_ros[0].end)


def mk(source: Path, lo: float, hi: float, title: str, *,
       aspect: AspectRatio | None = None, remove_sil: bool = False,
       music: str | None = None, duck: bool = True) -> Clip:
    c = Clip(source_path=str(source), start=lo, end=hi, title=title,
             status=ClipStatus.APPROVED)
    c.edit = ClipEdit(aspect=aspect, remove_silences=remove_sil,
                      music_path=music, duck_music=duck)
    return c


def speech_window(secs: float) -> float:
    """Inicio da janela de `secs` segundos com mais palavras (fala densa)."""
    tr = ctx.transcript_for(SRC_POD)
    words = [float(w.start) for s in (tr.segments if tr else []) for w in (s.words or [])]
    if not words:
        return BASE_LO
    best, best_n = words[0], -1
    for t0 in words:
        if t0 + secs > SRC_DUR:
            continue
        n = sum(1 for x in words if t0 <= x <= t0 + secs)
        if n > best_n:
            best, best_n = t0, n
    return float(best)


SUB_LO = speech_window(7.0)

ASPECTS = [("A1", AspectRatio.VERTICAL_9_16, "9x16-1080p", (1080, 1920)),
           ("A2", AspectRatio.HORIZONTAL_16_9, "16x9-1080p", (1920, 1080)),
           ("A3", AspectRatio.SQUARE_1_1, "1x1-1080p", (1080, 1080)),
           ("A4", AspectRatio.PORTRAIT_4_5, "4x5-1080p", (1080, 1350))]
STYLES = [("B1", SubtitleStyle.VIRAL, "viral", "center"),
          ("B2", SubtitleStyle.PROFESSIONAL, "professional", "bottom"),
          ("B3", SubtitleStyle.PODCAST, "podcast", "center"),
          ("B4", SubtitleStyle.DYNAMIC, "dynamic", "center"),
          ("B5", SubtitleStyle.MINIMALIST, "minimalist", "bottom")]

clips_A = {cid: mk(SRC_POD, BASE_LO, BASE_HI, f"{cid}-{lab}", aspect=ar)
           for cid, ar, lab, _ in ASPECTS}
clips_B = {}
for cid, st, lab, _pos in STYLES:
    clips_B[(cid, "burn")] = mk(SRC_POD, SUB_LO, SUB_LO + 7.0, f"{cid}-{lab}-BURN")
    clips_B[(cid, "noburn")] = mk(SRC_POD, SUB_LO, SUB_LO + 7.0, f"{cid}-{lab}-NOBURN")
clip_C1 = mk(SRC_POD, BASE_LO, BASE_HI, "C1-silOFF", remove_sil=False)
clip_C2 = mk(SRC_POD, BASE_LO, BASE_HI, "C2-silON", remove_sil=True)
clip_D1 = mk(SRC_ROS, ROS_LO, ROS_HI, "D1-faceON", aspect=AspectRatio.VERTICAL_9_16)
clip_D2 = mk(SRC_ROS, ROS_LO, ROS_HI, "D2-faceOFF", aspect=AspectRatio.VERTICAL_9_16)
clips_E = {i: mk(SRC_POD, float(c.start), float(c.end), f"E{i+1}-lote")
           for i, c in enumerate(clips_pod)}
clip_F1 = mk(SRC_POD, BASE_LO, BASE_HI, "F1-com-musica", music=MUSIC, duck=True)
clip_F2 = mk(SRC_POD, BASE_LO, BASE_HI, "F2-sem-musica")

ALL_POD = list(clips_A.values()) + list(clips_B.values()) + [clip_C1, clip_C2] \
    + list(clips_E.values()) + [clip_F1, clip_F2]
n_pod = ctx.replace_clips_for(SRC_POD, ALL_POD)
n_ros = ctx.replace_clips_for(SRC_ROS, [clip_D1, clip_D2])
missing = [c.title for c in ALL_POD + [clip_D1, clip_D2] if ctx.clip(c.id) is None]
print(f"REGISTRY pod={n_pod} rosto={n_ros} missing={missing}", flush=True)

ROWS: list[dict] = []


def dump() -> None:
    payload = {"started_at": time.time(), "elapsed_s": round(elapsed(), 1),
               "budget_s": BUDGET_S, "window_cap": CAP_WINDOW, "adjustments": ADJUST,
               "sources": SOURCES,
               "analysis": ANALYZE, "sub_window": [round(SUB_LO, 2), round(SUB_LO + 7, 2)],
               "base_window": [round(BASE_LO, 2), round(BASE_HI, 2)],
               "settings": {"resolution": SET.resolution.value, "fps": SET.fps,
                            "crf": SET.quality_crf, "preset": SET.preset,
                            "codec": SET.codec.value, "audio_bitrate": SET.audio_bitrate,
                            "max_parallel": SET.max_parallel,
                            "proc.max_parallel_jobs": PARALLEL,
                            "subs.model": ctx.config.str_("subs.model"),
                            "sample_fps_face": ctx.config.float_("proc.sample_fps_face")},
               "rows": ROWS}
    RESULTS.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")


def elapsed() -> float:
    return time.monotonic() - START


def fit_window(c: Clip, cap: float) -> None:
    """Encolhe a janela do corte em campo se o orçamento apertar (6-8 s)."""
    lo = float(c.start)
    hi = min(float(c.end), lo + max(6.0, cap), SRC_DUR - 0.2)
    if hi - lo < (float(c.end) - lo) - 0.01:
        c.start, c.end = lo, hi


def make_plan(clip: Clip, aspect: AspectRatio, *, follow=None, force=False):
    """CropPlan real pelo nucleo; follow=None usa o ClipEdit (caminho do exporter)."""
    src = Path(clip.source_path)
    info = media.probe_or_raise(src)
    if follow is None:
        return PL.crop_plan_for(src, info, clip.start, clip.end, aspect, clip.edit,
                                force=force)
    return PL.crop_plan_for(src, info, clip.start, clip.end, aspect, Opts(follow),
                            force=force)


def plan_of(clip: Clip, aspect: AspectRatio, *, follow=None, force=False) -> dict:
    pl = make_plan(clip, aspect, follow=follow, force=force)
    pts = pl.points or []
    return {"mode": pl.mode, "keyframes": len(pts), "faces_used": pl.faces_used,
            "note": pl.note,
            "first": [round(pts[0][0], 2), pts[0][1], pts[0][2], pts[0][3], pts[0][4]] if pts else None,
            "last": [round(pts[-1][0], 2), pts[-1][1], pts[-1][2], pts[-1][3], pts[-1][4]] if pts else None,
            "x_span": [min(p[1] for p in pts), max(p[1] for p in pts)] if pts else None}


def run_case(case: str, clip: Clip, *, style: SubtitleStyle | None = None,
             burn: bool = True, expect: str = "", follow_off: bool = False,
             band: str = "center", extra: dict | None = None) -> dict:
    aspect = clip.edit.aspect or SET.aspect
    row = {"case": case, "title": clip.title, "source": Path(clip.source_path).name,
           "window": [round(clip.start, 2), round(clip.end, 2)],
           "aspect": aspect.value, "resolution": SET.resolution.value,
           "style": style.value if style else "-", "burn": burn,
           "remove_silences": clip.edit.remove_silences,
           "music": Path(clip.edit.music_path).name if clip.edit.music_path else "-",
           "declared_band": band, "expect": expect, "follow_off": follow_off}
    if extra:
        row.update(extra)
    ctx.project.options.subtitle_style = style or SubtitleStyle.VIRAL
    SET.burn_subtitles = bool(burn)
    ctx.config.set("export.burn_subtitles", bool(burn), save=False)
    src = Path(clip.source_path)
    before = graph_snapshot()
    nlog = len(CAP.lines)
    t0 = time.monotonic()
    try:
        if follow_off:
            # mesmo caminho do Exporter, mas com o plan de recorte facial desativado
            info = media.probe_or_raise(src)
            tmap = ctx.pipeline.timeline_for(clip)
            plan_obj = make_plan(clip, aspect, follow=False, force=True)
            row["plan"] = plan_of(clip, aspect, follow=False)
            out = Path(ctx.export_target(clip))
            out.parent.mkdir(parents=True, exist_ok=True)
            ass = ctx.exporter._ass_for(clip, src, tmap, out, aspect)
            RD.render_clip(clip, source=src, info=info, transcript=None, settings=SET,
                           plan=plan_obj, tmap=tmap, out=out, ass_path=ass)
            path = str(out)
            # a gravacao acima usou force=True sobre a mesma chave que o exporter
            # usou no D1 (follow ON): reler pelo caminho do exporter sem forcar
            # mostra se follow_speaker participa da chave do cache.
            leak = plan_of(clip, aspect)
            row["cache_key_check"] = {"served_after_off": {"mode": leak["mode"],
                                                           "keyframes": leak["keyframes"]},
                                      "correct_behaviour": "recomputar mode=face"}
            for k in list(PL._CROP_CACHE):
                if k[0] == str(src):
                    PL._CROP_CACHE.pop(k)
        else:
            path = ctx.exporter.export_one(clip.id)
            row["plan"] = plan_of(clip, aspect)
        row["seconds"] = round(time.monotonic() - t0, 2)
        row["out"] = path
        p = Path(path)
        row["exists"] = p.exists()
        if not p.exists():
            row["verdict"] = "FAIL: arquivo nao gerado"
        else:
            pr = ffprobe(path)
            exp = float(ctx.pipeline.timeline_for(clip).duration)
            row.update({k: pr[k] for k in ("w", "h", "duration", "vcodec", "acodec",
                                           "audio_streams", "bitrate_kbps", "fps")})
            row.update({"dur_real": pr["duration"], "dur_expected": round(exp, 2),
                        "dur_delta": round(pr["duration"] - exp, 2),
                        "kb": round(p.stat().st_size / 1024, 1), "md5": md5(path)})
            row["integrity"] = integrity(path)
            row["graph"] = graph_new(before)
            want = SET.target_size if aspect is SET.aspect else RD.output_size(SET, aspect)
            row["target_size"] = list(want)
            row["size_ok"] = (pr["w"], pr["h"]) == tuple(want)
            res_ok = bool(row["size_ok"])
            dur_ok = abs(pr["duration"] - exp) <= max(0.75, exp * 0.06)
            if not row["integrity"]["ok"]:
                row["verdict"] = f"FAIL: ffmpeg -v error -> {row['integrity']['stderr'][:180]}"
            elif not res_ok:
                row["verdict"] = f"FAIL: resolucao {pr['w']}x{pr['h']} != alvo {want[0]}x{want[1]}"
            elif not dur_ok:
                row["verdict"] = f"FAIL: duracao {pr['duration']}s != esperada {exp:.2f}s"
            else:
                row["verdict"] = "PASS"
            row["log"] = [ln for ln in CAP.lines[nlog:] if "difere do alvo" in ln
                          or "legenda" in ln.lower() or "ASS" in ln][:6]
    except Exception as exc:                                   # noqa: BLE001
        row["seconds"] = round(time.monotonic() - t0, 2)
        row["verdict"] = f"FAIL: {type(exc).__name__}: {str(exc)[:260]}"
        row["error"] = str(exc)[:400]
        row["log"] = CAP.lines[nlog:][-6:]
        row["graph"] = graph_new(before)
    ROWS.append(row)
    dump()
    print(f"[{case}] {row['verdict']} | {row.get('seconds')}s | "
          f"{row.get('w')}x{row.get('h')} | {row.get('dur_real')}s | {row.get('kb')}KB",
          flush=True)
    global CAP_WINDOW
    done = [r["seconds"] for r in ROWS if "seconds" in r]
    if done and (row["seconds"] > 70 or (statistics.fmean(done[-4:]) > 35 and CAP_WINDOW > 8)):
        if CAP_WINDOW > 8:
            CAP_WINDOW = 7.0
            ADJUST.append(f"janela reduzida para {CAP_WINDOW}s apos {case} "
                          f"(ultimo {row['seconds']}s, media {statistics.fmean(done):.1f}s, "
                          f"{elapsed():.0f}s decorridos)")
            print("ADJUST:", ADJUST[-1], flush=True)
    return row


def measure_subs(burn_row: dict, base_row: dict, tag: str) -> dict:
    out = {"method": "quadro do meio (e 30/70%) do render com burn menos o mesmo "
                     "trecho sem burn; media |diferenca| por banda, x 0.06-0.94, "
                     "bandas center[0.40-0.62] bottom[0.70-0.88]; limiar >5",
           "burn": burn_row.get("out"), "noburn": base_row.get("out")}
    if not out["burn"] or not out["noburn"]:
        return {"error": "render ausente", **out}
    d = min(ffprobe(out["burn"])["duration"], ffprobe(out["noburn"])["duration"])
    for frac, key in ((0.50, "mid"), (0.30, "p30"), (0.70, "p70")):
        tt = max(0.05, min(d - 0.1, d * frac))
        fa = extract_frame(out["burn"], tt, OUT / f"_{tag}-{key}-burn.png")
        fb = extract_frame(out["noburn"], tt, OUT / f"_{tag}-{key}-noburn.png")
        if not fa or not fb:
            out[key] = {"error": "quadro nao extraido"}
            continue
        m = roi_diff(fa, fb)
        m["t"] = round(tt, 2)
        declared = burn_row.get("declared_band", "center")
        m["roi_declared"] = next((b["mean_abs"] for b in m.get("bands", [])
                                  if b["band"] == declared), None)
        m["pass_gt5"] = (m.get("roi_declared") is not None and m["roi_declared"] > 5)
        out[key] = m
    mid = out.get("mid", {})
    out["headline"] = {"roi_declared": mid.get("roi_declared"),
                       "hot_band": mid.get("hot_band"),
                       "pass_gt5": mid.get("pass_gt5")}
    return out


# =============================================================== CASOS
def main() -> None:
    # 1) aspecto / resolucao ------------------------------------------------
    for cid, _ar, lab, target in ASPECTS:
        if elapsed() > BUDGET_S:
            ROWS.append({"case": cid, "title": lab, "verdict": "SKIP: orcamento exaurido"})
            continue
        c = clips_A[cid]
        fit_window(c, CAP_WINDOW)
        run_case(cid, c, style=SubtitleStyle.VIRAL, burn=True,
                 expect=f"{lab}: {target[0]}x{target[1]}, h264, legible", band="center")

    # 2) cinco estilos de legenda ------------------------------------------
    for cid, st, lab, pos in STYLES:
        if elapsed() > BUDGET_S:
            ROWS.append({"case": cid, "title": lab, "verdict": "SKIP: orcamento exaurido"})
            continue
        rb = run_case(f"{cid}-burn", clips_B[(cid, "burn")], style=st, burn=True,
                      expect=f"legenda {lab} queimada, posicao {pos}", band=pos)
        rn = run_case(f"{cid}-noburn", clips_B[(cid, "noburn")], style=st, burn=False,
                      expect="referencia do mesmo trecho sem legenda", band=pos)
        rb["subtitle_measure"] = measure_subs(rb, rn, cid)
        rb["noburn_md5"] = rn.get("md5")
        h = rb["subtitle_measure"].get("headline", {})
        rb["verdict"] = (rb["verdict"] + (" | legenda OK" if h.get("pass_gt5")
                                          else f" | FALHA legenda roi={h.get('roi_declared')}")
                         ) if rb["verdict"].startswith("PASS") else rb["verdict"]
        dump()
        print(f"[{cid}] legenda roi={h.get('roi_declared')} banda={h.get('hot_band')} "
              f"pass={h.get('pass_gt5')}", flush=True)

    # 3) remocao de silencio -----------------------------------------------
    if elapsed() <= BUDGET_S:
        fit_window(clip_C1, CAP_WINDOW)
        clip_C2.start, clip_C2.end = clip_C1.start, clip_C1.end
        r1 = run_case("C1-silence-off", clip_C1, style=SubtitleStyle.VIRAL, burn=True,
                      expect="duracao == janela sem cortes", band="center")
        r2 = run_case("C2-silence-on", clip_C2, style=SubtitleStyle.VIRAL, burn=True,
                      expect="arquivo mais curto que C1", band="center")
        tm = ctx.pipeline.timeline_for(clip_C2)
        r2["tmap_removed_s"] = round(float(getattr(tm, "removed", 0.0)), 2)
        r2["tmap_cuts"] = int(getattr(tm, "cuts", 0))
        r2["tmap_ranges"] = len(getattr(tm, "ranges", []))
        delta = round(float(r1.get("dur_real", 0)) - float(r2.get("dur_real", 0)), 2)
        r2["delta_vs_off"] = delta
        r1["tmap_removed_s"] = round(float(getattr(
            ctx.pipeline.timeline_for(clip_C1), "removed", 0.0)), 2)
        if r2["verdict"].startswith("PASS"):
            r2["verdict"] = ("PASS" if delta > 0.05 else
                             f"FAIL: remove_silences=True nao encurtou o arquivo "
                             f"(delta={delta}s)")
        dump()
        print(f"[C] off={r1.get('dur_real')}s on={r2.get('dur_real')}s delta={delta}s",
              flush=True)

    # 4) rastreamento facial ----------------------------------------------
    if elapsed() <= BUDGET_S:
        r_on = run_case("D1-face-on", clip_D1, style=SubtitleStyle.VIRAL, burn=True,
                        expect="CropPlan mode=face com keyframes sobre o rosto",
                        band="center")
        r_off = run_case("D2-face-off", clip_D2, style=SubtitleStyle.VIRAL, burn=True,
                         expect="plan facial desativado (recorte fixo)", band="center",
                         follow_off=True)
        r_on["face_log"] = log_tail(r"facial|rostos|trilha|face", 8)
        r_off["face_log"] = log_tail(r"facial|rostos|trilha|face", 2)
        if r_on.get("out") and r_off.get("out"):
            d = min(ffprobe(r_on["out"])["duration"], ffprobe(r_off["out"])["duration"])
            fa = extract_frame(r_on["out"], d * 0.5, OUT / "_D-mid-on.png")
            fb = extract_frame(r_off["out"], d * 0.5, OUT / "_D-mid-off.png")
            if fa and fb:
                r_on["face_vs_static_diff"] = roi_diff(fa, fb)
        dump()
        print(f"[D] on={r_on.get('plan')} off={r_off.get('plan')}", flush=True)

    # 5) lote paralelo -----------------------------------------------------
    if elapsed() <= BUDGET_S:
        ctx.project.options.subtitle_style = SubtitleStyle.VIRAL
        SET.burn_subtitles = True
        ctx.config.set("export.burn_subtitles", True, save=False)
        for i, c in clips_E.items():
            fit_window(c, CAP_WINDOW)
        before = graph_snapshot()
        nlog = len(CAP.lines)
        t0 = time.monotonic()
        res = ctx.exporter.export_all(clips=[clips_E[i] for i in (1, 2, 3)
                                             if i in clips_E])
        wall = round(time.monotonic() - t0, 2)
        files = []
        for i, c in sorted(clips_E.items()):
            p = Path(c.exported_path) if c.exported_path else None
            item = {"clip": i, "title": c.title, "window": [round(c.start, 1), round(c.end, 1)],
                    "out": str(p) if p else "", "state": c.status.value}
            if p and p.exists():
                pr = ffprobe(str(p))
                item.update({"w": pr["w"], "h": pr["h"], "dur_real": pr["duration"],
                             "dur_expected": round(float(
                                 ctx.pipeline.timeline_for(c).duration), 2),
                             "kb": round(p.stat().st_size / 1024, 1),
                             "vcodec": pr["vcodec"], "bitrate_kbps": pr["bitrate_kbps"],
                             "md5": md5(str(p)), "integrity": integrity(str(p))})
                item["ok"] = item["integrity"]["ok"]
            else:
                item["ok"] = False
                item["integrity"] = {"ok": False, "stderr": "arquivo ausente"}
            files.append(item)
        ok_n = sum(1 for f in files if f["ok"])
        ROWS.append({"case": "E-lote", "title": "3 cortes em paralelo",
                     "source": SRC_POD.name, "aspect": SET.aspect.value,
                     "resolution": SET.resolution.value, "style": "viral", "burn": True,
                     "remove_silences": False, "music": "-", "declared_band": "center",
                     "expect": f"workers={SET.max_parallel} (proc.max_parallel_jobs={PARALLEL})",
                     "seconds": wall, "report_ok": res.get("ok"),
                     "report_failed": res.get("failed"), "max_parallel": SET.max_parallel,
                     "files": files, "graph": graph_new(before),
                     "log": CAP.lines[nlog:][-8:],
                     "verdict": ("PASS" if ok_n == len(files) and not res.get("failed")
                                 else f"FAIL: {ok_n}/{len(files)} ok, "
                                      f"failed={res.get('failed')}: "
                                      f"{[f.get('integrity', {}).get('stderr', '')[:120] for f in files if not f['ok']][:2]}")})
        dump()
        print(f"[E-lote] {ROWS[-1]['verdict']} wall={wall}s ok={ok_n}/{len(files)}", flush=True)

    # 6) musica ------------------------------------------------------------
    if not Path(MUSIC).exists():
        sh(["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=220:duration=20",
            "-q:a", "4", MUSIC])
    if elapsed() <= BUDGET_S:
        fit_window(clip_F1, CAP_WINDOW)
        clip_F2.start, clip_F2.end = clip_F1.start, clip_F1.end
        r_m = run_case("F1-musica-on", clip_F1, style=SubtitleStyle.VIRAL, burn=True,
                       expect="trilha mixada (amix) + ducking (sidechaincompress)",
                       band="center",
                       extra={"music_volume": clip_F1.edit.music_volume,
                              "duck_music": clip_F1.edit.duck_music,
                              "music_path": clip_F1.edit.music_path})
        r_n = run_case("F2-musica-off", clip_F2, style=SubtitleStyle.VIRAL, burn=True,
                       expect="somente audio original (1 stream, sem amix)", band="center")
        if r_m.get("out"):
            r_m["volume"] = volumedetect(r_m["out"])
        if r_n.get("out"):
            r_n["volume"] = volumedetect(r_n["out"])
        if r_m["verdict"].startswith("PASS"):
            mixed = bool(r_m.get("graph", {}).get("amix"))
            duck = bool(r_m.get("graph", {}).get("sidechaincompress"))
            if mixed or r_m.get("audio_streams", 0) >= 2:
                r_m["verdict"] = ("PASS" if duck else
                                  f"PASS(sem ducking): mixer aplicado, sidechaincompress ausente")
            else:
                r_m["verdict"] = (f"FAIL: mixer ausente no filtro_complex "
                                  f"(amix=False, streams_audio={r_m.get('audio_streams')})")
        if r_n["verdict"].startswith("PASS") and r_n.get("graph", {}).get("amix"):
            r_n["verdict"] = "FAIL: sem trilha mas o grafo contem amix"
        dump()
        print(f"[F] {r_m['verdict']} | amix={r_m.get('graph',{}).get('amix')} "
              f"duck={r_m.get('graph',{}).get('sidechaincompress')} "
              f"vol {r_m.get('volume')} vs {r_n.get('volume')}", flush=True)

    done = [r for r in ROWS if "seconds" in r]
    failed = [f"{r['case']}: {r.get('verdict')}" for r in ROWS
              if r.get("verdict") and not str(r["verdict"]).startswith("PASS")]
    summary = {"cases_defined": 4 + 10 + 2 + 2 + 3 + 2, "rows": len(ROWS),
               "exports_ran": len(done),
               "pass": sum(1 for r in ROWS if str(r.get("verdict", "")).startswith("PASS")),
               "fail_or_skip": failed, "wall_s": round(elapsed(), 1),
               "export_seconds": {r["case"]: r.get("seconds") for r in ROWS if "seconds" in r},
               "adjustments": ADJUST}
    dump()
    data = json.loads(RESULTS.read_text())
    data["summary"] = summary
    RESULTS.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    print("SUMMARY", json.dumps(summary, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
