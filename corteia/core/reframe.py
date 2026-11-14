"""Crop planning for vertical / square reframing (``CropPlan`` producer).

Design decisions that matter for the rest of the pipeline:

* **Segments, not expressions.** ``ARCHITECTURE.md`` fixes the rendering
  strategy: ``render.py`` cuts the source in N blocks, applies a *static*
  ``crop`` to each and concatenates them. That is why the crop **size is
  constant across a plan** (only the position moves) — ``concat`` refuses
  blocks with different geometry. :func:`segments_of` is the hand-off.
* **Never invent motion.** When no face was found the plan falls back to real
  saliency (Sobel energy averaged over the window) or, if the video cannot be
  read, to a static centred crop. Both produce a two-point plan whose box is
  identical at both ends.
* **Honesty about limits.** Whenever the source geometry cannot honour the
  requested aspect ratio or zoom, the box is adjusted and ``CropPlan.note``
  says so in Portuguese.

Every public function is deterministic in time: same points in, same plan out.
"""

from __future__ import annotations

import logging
import math
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

import cv2
import numpy as np

from ..models import AspectRatio, CropPlan, FaceTrackPoint, VideoInfo

log = logging.getLogger("corteia")

# --------------------------------------------------------------------------- #
# tuning constants (rationale is summarised here and reported in the answer)
# --------------------------------------------------------------------------- #
#: Keyframes are emitted at most this far apart (seconds).
KEYFRAME_MAX_SPACING = 0.5
#: Hard cap so a render never explodes into hundreds of FFmpeg segments.
MAX_KEYFRAMES = 40
MIN_KEYFRAMES = 2

#: EMA time constant in *seconds*: ``alpha = 1 - exp(-dt / SMOOTH_TAU_S)``, so
#: the smoothing is independent of the sample rate. 0.25 s is about the
#: slowest a viewer accepts before the crop feels laggy, fast enough to kill
#: per-frame detector jitter.
SMOOTH_TAU_S = 0.25
#: Maximum panning speed as a fraction of the source height per second:
#: 0.25 on 720p means the window never moves faster than 180 px/s.
MAX_SPEED_FRAC = 0.25

#: YuNet frontal boxes are a touch taller than wide and ``FaceTrackPoint``
#: stores only the width, so the height is reconstructed with this ratio.
FACE_H_OVER_W = 1.05
#: Framing rule: the crop is at least this wide / this tall compared with the
#: face box, keeping shoulders and headroom inside the shot.
FACE_MARGIN_H = 2.2
FACE_MARGIN_V = 3.0
#: Never go below this crop height (source px) when zooming in.
MIN_CROP_PX = 96
#: The face centre is placed at this fraction of the crop height (0.42 puts
#: the eyes near the upper third instead of dead centre).
HEADROOM = 0.42

# --- track selection -------------------------------------------------------
SPEAK_WEIGHT = 0.6        #: weight of the visual-speech signal
AREA_WEIGHT = 0.4         #: weight of the relative face area
#: If the pooled maximum of ``speaking`` stays below this, the speech signal is
#: treated as absent and the choice falls back to "biggest face wins".
SPEAK_ACTIVE_MIN = 0.15
#: Hysteresis: a challenger must beat the current track by ``SWITCH_MARGIN`` in
#: the combined score for at least ``SWITCH_MIN_SAMPLES`` samples *and*
#: ``SWITCH_MIN_DWELL_S`` seconds, and the previous switch must be at least
#: ``SWITCH_MIN_DWELL_S`` old. Without it the crop would hop between two people
#: every 1/6 s whenever their areas are similar.
SWITCH_MARGIN = 0.08
SWITCH_MIN_SAMPLES = 2
SWITCH_MIN_DWELL_S = 0.6
#: Detection flicker tolerance: samples a missing track is held before
#: the plan takes over another one (4 samples = 0,67 s at 6 fps).
HOLD_MAX_STEPS = 4

# --- saliency fallback -----------------------------------------------------
SALIENCY_FRAMES = 6       #: frames averaged inside the window
SALIENCY_GRID = 64        #: coarse grid side of the energy map
_SALIENCY_CACHE: "OrderedDict[tuple, tuple[float, float] | None]" = OrderedDict()
_SALIENCY_CACHE_MAX = 64


# --------------------------------------------------------------------------- #
# geometry helpers
# --------------------------------------------------------------------------- #
def _ar_of(aspect) -> float:
    """Numeric width/height of an :class:`AspectRatio` (plain ``"9:16"`` too)."""
    if isinstance(aspect, AspectRatio):
        return aspect.value_w_h
    if isinstance(aspect, str) and ":" in aspect:
        w, h = aspect.split(":", 1)
        return float(w) / float(h)
    return float(aspect)


def _src_size(info: VideoInfo) -> tuple[int, int]:
    w = int(getattr(info, "width", 0) or 0)
    h = int(getattr(info, "height", 0) or 0)
    if w <= 0 or h <= 0:
        log.warning("VideoInfo sem resolução válida (%dx%d); assumindo 1280x720", w, h)
        return 1280, 720
    return w, h


def crop_for_aspect(src_w: int, src_h: int, aspect: AspectRatio) -> tuple[int, int]:
    """Largest ``(w, h)`` of the source that has exactly ``aspect``.

    The binding dimension is taken from the source and the other derived, so
    the integer result stays within one pixel of the ratio and never exceeds
    the frame.
    """
    src_w, src_h = int(src_w), int(src_h)
    ar = _ar_of(aspect)
    if src_w <= 0 or src_h <= 0 or ar <= 0:
        return max(1, src_w), max(1, src_h)
    h = float(src_h)
    w = h * ar
    if w > src_w:
        w, h = float(src_w), float(src_w) / ar
    return max(1, min(int(round(w)), src_w)), max(1, min(int(round(h)), src_h))


def static_center_crop(src_w: int, src_h: int, aspect: AspectRatio) -> tuple[int, int, int, int]:
    """Static centred crop ``(x, y, w, h)`` — the no-information fallback."""
    w, h = crop_for_aspect(src_w, src_h, aspect)
    return (src_w - w) // 2, (src_h - h) // 2, w, h


def _clamp_box(cx: float, cy: float, w: int, h: int, src_w: int, src_h: int) -> tuple[int, int]:
    """Top-left of a ``w``×``h`` box centred on ``(cx, cy)``, kept inside source."""
    x = int(round(cx - w / 2.0))
    y = int(round(cy - h / 2.0))
    return max(0, min(x, max(0, src_w - w))), max(0, min(y, max(0, src_h - h)))


def _fit_ar_in(gw: float, gh: float, ar: float, src_w: int, src_h: int) -> tuple[int, int]:
    """Largest AR-accurate box fitting both a requested size and the source."""
    w, h = float(gw), float(gh)
    if h <= 0 or w <= 0 or ar <= 0:
        return crop_for_aspect(src_w, src_h, ar)
    if w / h > ar:
        w = h * ar
    else:
        h = w / ar
    if w > src_w:
        w, h = float(src_w), float(src_w) / ar
    if h > src_h:
        h, w = float(src_h), float(src_h) * ar
    return max(2, int(round(w))), max(2, int(round(h)))


# --------------------------------------------------------------------------- #
# keyframe utilities
# --------------------------------------------------------------------------- #
def crop_expr_at(plan: CropPlan, t: float) -> tuple[int, int, int, int]:
    """Box ``(x, y, w, h)`` at ``t`` by linear interpolation of the keyframes."""
    pts = list(plan.points or [])
    if not pts:
        log.warning("plano de recorte vazio; devolvendo caixa nulo")
        return 0, 0, 0, 0
    if len(pts) == 1:
        p = pts[0]
        return int(p[1]), int(p[2]), int(p[3]), int(p[4])
    t = float(t)
    if t <= pts[0][0]:
        p = pts[0]
        return int(p[1]), int(p[2]), int(p[3]), int(p[4])
    if t >= pts[-1][0]:
        p = pts[-1]
        return int(p[1]), int(p[2]), int(p[3]), int(p[4])
    for a, b in zip(pts, pts[1:]):
        if a[0] <= t <= b[0]:
            span = b[0] - a[0]
            k = 0.0 if span <= 0 else (t - a[0]) / span
            return (int(round(a[1] + k * (b[1] - a[1]))),
                    int(round(a[2] + k * (b[2] - a[2]))),
                    int(round(a[3] + k * (b[3] - a[3]))),
                    int(round(a[4] + k * (b[4] - a[4]))))
    p = pts[-1]
    return int(p[1]), int(p[2]), int(p[3]), int(p[4])


def segments_of(plan: CropPlan) -> list[tuple[float, float, int, int, int, int]]:
    """Blocks ``(t_start, t_end, x, y, w, h)`` — one per keyframe pair.

    The box is sampled at the block midpoint. Plans built by
    :func:`plan_crop` always share a single ``w``/``h``; a plan edited by hand
    (or loaded from an old project) whose sizes differ is normalised to the
    median size so ``concat`` still receives identical geometry.
    """
    pts = [(float(p[0]), int(p[1]), int(p[2]), int(p[3]), int(p[4])) for p in (plan.points or [])]
    if len(pts) < 2:
        return []
    widths = sorted(p[3] for p in pts)
    heights = sorted(p[4] for p in pts)
    uniform = widths[0] == widths[-1] and heights[0] == heights[-1]
    w_med, h_med = widths[len(widths) // 2], heights[len(heights) // 2]
    max_w, max_h = widths[-1], heights[-1]
    segs: list[tuple[float, float, int, int, int, int]] = []
    for a, b in zip(pts, pts[1:]):
        if b[0] <= a[0]:
            continue
        x, y, w, h = crop_expr_at(plan, (a[0] + b[0]) / 2.0)
        if not uniform:
            w, h = w_med, h_med
            x = max(0, min(x, max(0, max_w - w)))
            y = max(0, min(y, max(0, max_h - h)))
        segs.append((a[0], b[0], int(x), int(y), int(w), int(h)))
    return segs


def _dense_grid(t0: float, t1: float) -> list[float]:
    """Uniform keyframe times over ``[t0, t1]`` with spacing <= 0.5 s."""
    dur = max(1e-3, t1 - t0)
    n = max(MIN_KEYFRAMES, int(math.ceil(dur / KEYFRAME_MAX_SPACING)) + 1)
    return [t0 + i * dur / (n - 1) for i in range(n)]


def _group_keyframes(dense: Sequence[tuple[float, int, int, int, int]],
                     max_n: int = MAX_KEYFRAMES
                     ) -> list[tuple[float, int, int, int, int]]:
    """Downsample keyframes to ``max_n`` using duration-weighted means.

    Each point is weighted by the time span it represents, so a long static
    stretch keeps dominating the average instead of being diluted by the
    neighbouring short moves. The first and last times are preserved exactly.
    """
    if len(dense) <= max_n or len(dense) < 3:
        return list(dense)
    weights: list[float] = []
    for i, p in enumerate(dense):
        prev_t = dense[i - 1][0] if i else p[0]
        next_t = dense[i + 1][0] if i + 1 < len(dense) else p[0]
        weights.append(max(1e-3, (next_t - prev_t) / 2.0))
    span = len(dense) / float(max_n)
    bins: list[list[int]] = [[] for _ in range(max_n)]
    for i in range(len(dense)):
        bins[min(max_n - 1, int(i / span))].append(i)
    out: list[tuple[float, int, int, int, int]] = []
    for idx in bins:
        if not idx:
            continue
        wsum = sum(weights[i] for i in idx)
        x = sum(weights[i] * dense[i][1] for i in idx) / wsum
        y = sum(weights[i] * dense[i][2] for i in idx) / wsum
        w = sum(weights[i] * dense[i][3] for i in idx) / wsum
        h = sum(weights[i] * dense[i][4] for i in idx) / wsum
        out.append((float(dense[idx[0]][0]), int(round(x)), int(round(y)),
                    int(round(w)), int(round(h))))
    out[0] = (float(dense[0][0]), *out[0][1:])
    out[-1] = (float(dense[-1][0]), *out[-1][1:])
    return out


# --------------------------------------------------------------------------- #
# track selection
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class _Sample:
    t: float
    cx: float
    cy: float
    w: float
    track: int
    speaking: float


def _by_time(points: Iterable[FaceTrackPoint]) -> dict[float, dict[int, FaceTrackPoint]]:
    table: dict[float, dict[int, FaceTrackPoint]] = {}
    for p in points:
        table.setdefault(round(float(p.t), 3), {})[int(p.track)] = p
    return dict(sorted(table.items()))


def _select_track(points: list[FaceTrackPoint], follow_speaker: bool,
                  notes: list[str]) -> list[_Sample]:
    """One track per sample, chosen by speech + area, with switching hysteresis.

    A detector dropout of a few samples never changes the subject: the last
    known box is held for ``HOLD_MAX_STEPS`` samples first, because a single
    missed detection must not make the crop jump to the other person.
    """
    table = _by_time(points)
    max_w = max((p.w for p in points), default=0.0) or 1.0
    peak = max((p.speaking for p in points), default=0.0)
    use_speech = bool(follow_speaker) and peak >= SPEAK_ACTIVE_MIN
    if follow_speaker and not use_speech:
        notes.append("sem sinal de fala confiável: priorizando o rosto maior")

    def value(p: FaceTrackPoint) -> float:
        area = (p.w / max_w) ** 2
        return SPEAK_WEIGHT * p.speaking + AREA_WEIGHT * area if use_speech else area

    chosen: list[_Sample] = []
    current: int | None = None
    cand: int | None = None
    cand_count = 0
    cand_since = 0.0
    last_switch = points[0].t
    hold: FaceTrackPoint | None = None
    held = 0

    for t, present in table.items():
        best = max(present, key=lambda tid: (value(present[tid]), tid))
        if current is None:
            current, last_switch = best, t

        if current in present:
            hold = present[current]
            held = 0
            if best != current:
                advantage = value(present[best]) - value(present[current])
                if advantage >= SWITCH_MARGIN:
                    if cand == best:
                        cand_count += 1
                    else:
                        cand, cand_count, cand_since = best, 1, t
                    if (cand_count >= SWITCH_MIN_SAMPLES
                            and t - cand_since >= SWITCH_MIN_DWELL_S
                            and t - last_switch >= SWITCH_MIN_DWELL_S):
                        notes.append(f"falante ativo mudou para a trilha {best}")
                        current, cand, cand_count, last_switch = best, None, 0, t
                        hold = present[current]
                else:
                    cand, cand_count = None, 0
            else:
                cand, cand_count = None, 0
        elif hold is not None and held < HOLD_MAX_STEPS:
            held += 1                      # detection flicker: keep the last box
        else:
            notes.append(f"trilha {current} sem detecção por {held + 1} amostras: "
                         f"assumindo a trilha {best}")
            current, held, last_switch = best, 0, t
            hold = present[best]
            cand, cand_count = None, 0

        if hold is None:
            continue
        chosen.append(_Sample(t=float(t), cx=float(hold.cx), cy=float(hold.cy),
                              w=float(hold.w), track=int(current),
                              speaking=float(hold.speaking)))
    return chosen


# --------------------------------------------------------------------------- #
# smoothing
# --------------------------------------------------------------------------- #
def _smooth(samples: list[_Sample], src_w: int, src_h: int
            ) -> list[tuple[float, float, float, float]]:
    """EMA + speed clamp in source pixels -> ``(t, cx_px, cy_px, face_w_px)``."""
    out: list[tuple[float, float, float, float]] = []
    prev_t: float | None = None
    prev_cx = prev_cy = 0.0
    for s in samples:
        tx, ty = s.cx * src_w, s.cy * src_h
        if prev_t is None:
            out.append((s.t, tx, ty, s.w * src_w))
            prev_cx, prev_cy, prev_t = tx, ty, s.t
            continue
        dt = max(1e-3, s.t - prev_t)
        alpha = 1.0 - math.exp(-dt / SMOOTH_TAU_S)
        limit = MAX_SPEED_FRAC * src_h * dt
        dx = max(-limit, min(limit, alpha * (tx - prev_cx)))
        dy = max(-limit, min(limit, alpha * (ty - prev_cy)))
        prev_cx, prev_cy, prev_t = prev_cx + dx, prev_cy + dy, s.t
        out.append((s.t, prev_cx, prev_cy, s.w * src_w))
    return out


def _value_at(series: Sequence[tuple[float, float, float, float]], t: float
              ) -> tuple[float, float, float]:
    """Piecewise-linear read of a smoothed series, held outside its range."""
    if not series:
        return 0.0, 0.0, 0.0
    if t <= series[0][0]:
        return series[0][1], series[0][2], series[0][3]
    if t >= series[-1][0]:
        return series[-1][1], series[-1][2], series[-1][3]
    for a, b in zip(series, series[1:]):
        if a[0] <= t <= b[0]:
            span = b[0] - a[0]
            k = 0.0 if span <= 0 else (t - a[0]) / span
            return (a[1] + k * (b[1] - a[1]),
                    a[2] + k * (b[2] - a[2]),
                    a[3] + k * (b[3] - a[3]))
    return series[-1][1], series[-1][2], series[-1][3]


# --------------------------------------------------------------------------- #
# saliency fallback (real, cached)
# --------------------------------------------------------------------------- #
def _bgr(frame: np.ndarray) -> np.ndarray:
    if frame.ndim == 2:
        return cv2.cvtColor(frame, cv2.COLOR_GRAY2BGR)
    if frame.shape[2] == 4:
        return cv2.cvtColor(frame, cv2.COLOR_BGRA2BGR)
    return frame


def _cache_put(key: tuple, value) -> None:
    _SALIENCY_CACHE[key] = value
    _SALIENCY_CACHE.move_to_end(key)
    while len(_SALIENCY_CACHE) > _SALIENCY_CACHE_MAX:
        _SALIENCY_CACHE.popitem(last=False)


def saliency_center(path, info: VideoInfo, t0: float, t1: float,
                    aspect: AspectRatio, *, frames: int = SALIENCY_FRAMES
                    ) -> tuple[float, float] | None:
    """Normalised centre of the highest motion-energy region of the window.

    Averages the Sobel magnitude of ``frames`` samples taken across
    ``[t0, t1]``, then finds where an AR-shaped window sums the most gradient
    energy (integral image). Cached per ``(path, window, aspect, size)``
    because the editor calls it again on every preview refresh. ``None`` when
    the file cannot be read — the caller then falls back to a centred crop.
    """
    src_w, src_h = _src_size(info)
    key = (str(path), round(float(t0), 2), round(float(t1), 2), _ar_of(aspect),
           src_w, src_h, int(frames))
    if key in _SALIENCY_CACHE:
        _SALIENCY_CACHE.move_to_end(key)
        return _SALIENCY_CACHE[key]
    cap = cv2.VideoCapture(str(path))
    try:
        if not cap.isOpened() or t1 <= t0:
            return None
        fps = float(cap.get(cv2.CAP_PROP_FPS) or 0.0) or 30.0
        count = float(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0.0)
        hi = min(float(t1), count / fps) if count > 0 else float(t1)
        if hi <= t0:
            return None
        n = max(2, int(frames))
        gw = SALIENCY_GRID
        gh = max(8, int(round(SALIENCY_GRID * src_h / max(1, src_w))))
        acc = np.zeros((gh, gw), np.float32)
        used = 0
        for i in range(n):
            t = float(t0) + i * (hi - float(t0)) / (n - 1)
            cap.set(cv2.CAP_PROP_POS_MSEC, max(0.0, t) * 1000.0)
            ok, frame = cap.read()
            if not ok or frame is None:
                continue
            small = cv2.resize(cv2.cvtColor(_bgr(frame), cv2.COLOR_BGR2GRAY),
                               (gw, gh), interpolation=cv2.INTER_AREA)
            small = cv2.GaussianBlur(small, (5, 5), 0).astype(np.float32)
            gx = cv2.Sobel(small, cv2.CV_32F, 1, 0, ksize=3)
            gy = cv2.Sobel(small, cv2.CV_32F, 0, 1, ksize=3)
            acc += np.sqrt(gx * gx + gy * gy)
            used += 1
        if used == 0:
            _cache_put(key, None)
            return None
        acc = cv2.medianBlur(acc, 3)
        cw, ch = crop_for_aspect(gw, gh, aspect)
        if cw < 4 or ch < 4 or cw > gw or ch > gh:
            _cache_put(key, None)
            return None
        pad = np.zeros((gh + 1, gw + 1), np.float64)
        pad[1:, 1:] = np.cumsum(np.cumsum(acc.astype(np.float64), axis=0), axis=1)
        best_score, best_x0, best_y0 = -1.0, 0, 0
        for y0 in range(0, gh - ch + 1):
            row_hi, row_lo = pad[y0 + ch], pad[y0]
            for x0 in range(0, gw - cw + 1):
                s = row_hi[x0 + cw] - row_lo[x0 + cw] - row_hi[x0] + row_lo[x0]
                if s > best_score:
                    best_score, best_x0, best_y0 = float(s), x0, y0
        result = (round((best_x0 + cw / 2.0) / gw, 4), round((best_y0 + ch / 2.0) / gh, 4))
        _cache_put(key, result)
        log.debug("saliência %s [%.2f-%.2f] -> (%.3f, %.3f) score %.1f",
                  path, t0, t1, result[0], result[1], best_score)
        return result
    finally:
        cap.release()


# --------------------------------------------------------------------------- #
# crop size
# --------------------------------------------------------------------------- #
def _crop_size(face_w_px: float, ar: float, src_w: int, src_h: int, zoom: float,
               notes: list[str]) -> tuple[int, int]:
    """The one crop size used by the whole plan (position is what moves)."""
    face_h_px = max(1.0, face_w_px) * FACE_H_OVER_W
    need_h = max(FACE_MARGIN_V * face_h_px, FACE_MARGIN_H * max(1.0, face_w_px) / ar, 1.0)
    zoom = max(0.5, min(4.0, float(zoom) or 1.0))
    crop_h, crop_w = need_h / zoom, (need_h / zoom) * ar
    clamped_to_source = crop_w > src_w or crop_h > src_h
    if clamped_to_source:
        crop_w, crop_h = float(src_w), float(src_w) / ar
        if crop_h > src_h:
            crop_h, crop_w = float(src_h), float(src_h) * ar
    w, h = _fit_ar_in(crop_w, crop_h, ar, src_w, src_h)
    if h < MIN_CROP_PX:
        w, h = _fit_ar_in(MIN_CROP_PX * ar, MIN_CROP_PX, ar, src_w, src_h)
        notes.append("zoom limitado pelo tamanho da fonte")
    if clamped_to_source and not (w >= src_w or h >= src_h):
        notes.append("a fonte não comporta o recorte pedido; tamanho ajustado")
    return int(w), int(h)


# --------------------------------------------------------------------------- #
# plan
# --------------------------------------------------------------------------- #
def _parse_manual(manual: str) -> tuple[float, float, float, float] | None:
    """Parse ``"x,y,w,h"`` (ints or floats) into ``(cx, cy, w, h)``."""
    try:
        parts = [float(v) for v in str(manual).replace(";", ",").split(",") if v.strip()]
    except ValueError:
        return None
    if len(parts) != 4:
        return None
    x, y, w, h = parts
    if w <= 0 or h <= 0:
        return None
    return x + w / 2.0, y + h / 2.0, w, h


def plan_crop(points: list[FaceTrackPoint], info: VideoInfo, aspect: AspectRatio,
              t0: float, t1: float, *, follow_speaker: bool = True, zoom: float = 1.0,
              offset_x: float = 0, offset_y: float = 0, manual: str = "") -> CropPlan:
    """Build the crop keyframes for ``[t0, t1]`` of *info*.

    Order of decisions: an explicit ``manual`` box wins; otherwise faces
    (track choice with hysteresis -> EMA + speed clamp -> constant-size box
    clamped to the source -> keyframes at most 0.5 s apart, grouped to 40 when
    exceeded); with no face at all, real saliency or a static centred crop.
    ``CropPlan.mode`` reports which path ran and ``CropPlan.note`` every
    compromise made. ``offset_x``/``offset_y`` are in **source pixels**.
    """
    src_w, src_h = _src_size(info)
    ar = _ar_of(aspect)
    t0 = max(0.0, float(t0))
    t1 = float(t1) if t1 and float(t1) > t0 else t0 + 0.5
    notes: list[str] = []

    # ---- 1) manual -------------------------------------------------------
    req = _parse_manual(manual) if manual else None
    if manual and req is None:
        notes.append(f"recorte manual inválido: '{manual}' (esperado x,y,w,h)")
    if req is not None:
        cx, cy, gw, gh = req
        w, h = _fit_ar_in(gw, gh, ar, src_w, src_h)
        x, y = _clamp_box(cx, cy, w, h, src_w, src_h)
        return CropPlan(points=[(round(t0, 3), x, y, w, h), (round(t1, 3), x, y, w, h)],
                        mode="manual", faces_used=0,
                        note="; ".join(notes + ["recorte manual"]))

    # ---- 2) faces --------------------------------------------------------
    win = [p for p in (points or [])
           if t0 - 1e-3 <= float(p.t) <= t1 + 1e-3 and float(p.w) > 0]
    if win:
        win.sort(key=lambda p: (float(p.t), int(p.track)))
        track_ids = {int(p.track) for p in win}
        samples = _select_track(win, follow_speaker, notes)
        if not samples:
            notes.append("trilhas sem amostras válidas")
        else:
            series = _smooth(samples, src_w, src_h)
            face_w = float(np.median([s.w for s in samples])) * src_w
            crop_w, crop_h = _crop_size(face_w, ar, src_w, src_h, zoom, notes)
            ox, oy = float(offset_x or 0.0), float(offset_y or 0.0)
            dense: list[tuple[float, int, int, int, int]] = []
            for t in _dense_grid(t0, t1):
                cx_px, cy_px, _w_px = _value_at(series, t)
                # the face sits at HEADROOM of the crop height, not dead centre
                x, y = _clamp_box(cx_px + ox, cy_px + (0.5 - HEADROOM) * crop_h + oy,
                                  crop_w, crop_h, src_w, src_h)
                dense.append((round(t, 3), x, y, crop_w, crop_h))
            keyframes = [(float(t), max(0, min(x, src_w - crop_w)),
                          max(0, min(y, src_h - crop_h)), crop_w, crop_h)
                         for t, x, y, _w, _h in _group_keyframes(dense)]
            extra = f"{len(track_ids)} trilha(s), recorte {crop_w}x{crop_h}, " \
                    f"{len(keyframes)} keyframes"
            return CropPlan(points=keyframes, mode="face", faces_used=len(track_ids),
                            note="; ".join([n for n in notes + [extra] if n]))

    # ---- 3) no faces: real saliency, else a static centred crop ----------
    source_path = Path(str(getattr(info, "path", "") or ""))
    centre = saliency_center(source_path, info, t0, t1, aspect) if source_path else None
    if centre is not None:
        w, h = crop_for_aspect(src_w, src_h, aspect)
        x, y = _clamp_box(centre[0] * src_w + float(offset_x or 0.0),
                          centre[1] * src_h + float(offset_y or 0.0), w, h, src_w, src_h)
        mode = "saliency"
        notes.append("nenhum rosto na janela: recorte fixo na área de maior movimento")
    else:
        x, y, w, h = static_center_crop(src_w, src_h, aspect)
        x = max(0, min(int(x + float(offset_x or 0.0)), src_w - w))
        y = max(0, min(int(y + float(offset_y or 0.0)), src_h - h))
        mode = "center"
        notes.append("nenhum rosto na janela: recorte central estático")
    try:  # informational only: why detection produced nothing
        from . import faces as _faces

        why = _faces.detector_note() if not _faces.detector_available() else ""
    except Exception:  # noqa: BLE001
        why = ""
    return CropPlan(points=[(round(t0, 3), x, y, w, h), (round(t1, 3), x, y, w, h)],
                    mode=mode, faces_used=0,
                    note="; ".join([n for n in ([why] + notes) if n]))


# --------------------------------------------------------------------------- #
# FFmpeg
# --------------------------------------------------------------------------- #
def _q(expr: str) -> str:
    """Quote an FFmpeg expression so its commas survive a filtergraph.

    The filtergraph parser treats ``,`` ``;`` ``[`` ``]`` and ``:`` as
    structural; inside single quotes they are literal, which is exactly what
    an ``if(between(t,a,b),x,y)`` expression needs. A quote inside the payload
    is closed, escaped and reopened.
    """
    return "'" + expr.replace("'", "'\\''") + "'"


def _scale_tail(out_w: int, out_h: int, fps: float) -> str:
    return (f"scale={int(out_w)}:{int(out_h)}:flags=bicubic,setsar=1,fps=fps={fps:g}")


def _crop_static(w: int, h: int, x: int, y: int) -> str:
    return f"crop={int(w)}:{int(h)}:{int(x)}:{int(y)}"


def ffmpeg_reframe_filter(plan: CropPlan, out_w: int, out_h: int, fps: float,
                          *, mode: str = "filtergraph") -> str:
    """FFmpeg text implementing *plan*.

    ``"filtergraph"`` (default, the segment strategy): one labelled chain per
    keyframe pair plus ``concat`` — drop it straight into ``-filter_complex``.
    The output pad is always ``[outv]``. Times are relative to the first
    keyframe, so the input must already be seeked there (``-ss``), which is
    what ``render.py`` does. Audio is deliberately not handled here: seeking
    the input once keeps sound and picture in sync while the video is
    assembled block by block.

    ``"crop"``: a single static ``-vf`` crop (static / two-keyframe plans).
    ``"expression"``: one ``crop`` whose ``x``/``y`` are nested
    ``if(between(t,...))`` expressions — exact motion, but the string grows
    with the keyframe count, so only for short plans.
    """
    segs = segments_of(plan)
    if not segs:
        pts = list(plan.points or [])
        if not pts:
            raise ValueError("plano de recorte sem keyframes")
        x, y, w, h = crop_expr_at(plan, pts[0][0])
        segs = [(pts[0][0], pts[-1][0] if len(pts) > 1 else pts[0][0] + 0.5, x, y, w, h)]
    base = segs[0][0]
    tail = _scale_tail(out_w, out_h, fps)

    if mode == "crop":
        x, y, w, h = crop_expr_at(plan, (segs[0][0] + segs[-1][1]) / 2.0)
        return f"{_crop_static(w, h, x, y)},{tail}"

    if mode == "expression":
        nested_x = str(int(segs[-1][2]))
        nested_y = str(int(segs[-1][3]))
        for a, b, x, y, _w, _h in reversed(segs):
            inner_x, inner_y = nested_x, nested_y
            nested_x = f"if(between(t,{a - base:.3f},{b - base:.3f}),{int(x)},{inner_x})"
            nested_y = f"if(between(t,{a - base:.3f},{b - base:.3f}),{int(y)},{inner_y})"
        return (f"crop={int(segs[0][4])}:{int(segs[0][5])}:{_q(nested_x)}:{_q(nested_y)}"
                f",{tail}")

    chains: list[str] = []
    labels: list[str] = []
    for i, (a, b, x, y, w, h) in enumerate(segs):
        lab = f"[crtv{i}]"
        labels.append(lab)
        chains.append(
            f"[0:v]trim=start={max(0.0, a - base):.3f}:end={max(0.0, b - base):.3f},"
            f"setpts=PTS-STARTPTS,{_crop_static(w, h, x, y)},{tail}{lab}"
        )
    if len(labels) == 1:
        return chains[0].replace(labels[0], "[outv]")
    chains.append(f"{''.join(labels)}concat=n={len(labels)}:v=1:a=0[outv]")
    return ";".join(chains)


def blur_fill_filter(out_w: int, out_h: int, *, sigma: float = 24.0) -> str:
    """Blurred-background fill when the source AR cannot cover the target AR.

    A 16:9 frame reframed to 9:16 always leaves side bars; instead of black
    ones this chain blows a copy up to cover the canvas, blurs it, and
    overlays the correctly-sized picture on top. Append it after an input pad
    inside ``-filter_complex`` (``[0:v]`` + this), and give the caller's own
    label at the end: the chain ends unlabelled.
    """
    out_w, out_h = int(out_w), int(out_h)
    s = max(1.0, float(sigma))
    return (
        f"split=2[bfill_bg][bfill_fg];"
        f"[bfill_bg]scale={out_w}:{out_h}:force_original_aspect_ratio=increase:"
        f"flags=bicubic,crop={out_w}:{out_h},gblur=sigma={s:g}[bfill_blur];"
        f"[bfill_fg]scale={out_w}:{out_h}:force_original_aspect_ratio=decrease:"
        f"flags=bicubic[bfill_fit];"
        f"[bfill_blur][bfill_fit]overlay=(W-w)/2:(H-h)/2,setsar=1"
    )


def needs_blur_fill(src_w: int, src_h: int, aspect: AspectRatio) -> bool:
    """True when cropping the source to ``aspect`` would need padding."""
    ar = _ar_of(aspect)
    src_ar = src_w / src_h if src_h else 0.0
    return bool(ar) and src_ar + 1e-6 < ar
