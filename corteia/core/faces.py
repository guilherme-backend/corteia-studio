"""Face detection and visual speech tracking for the reframing pipeline.

The public contract lives in ``ARCHITECTURE.md``. Two environment facts shape
this module and are verified at runtime rather than assumed:

* OpenCV 5 removed ``cv2.CascadeClassifier`` (and ``cv2.legacy``), so the
  primary detector is :class:`cv2.FaceDetectorYN` (YuNet) with the bundled ONNX
  model. The Haar path is kept behind a ``hasattr`` guard so the same code
  still works on OpenCV 4.
* No tracker API is available either, so tracks are maintained by hand:
  constant-velocity prediction + IoU association + EMA blending. Everything is
  deterministic — same video, same sample grid, same numbers.

Frames are never bulk-decoded: each sample seeks with
``CAP_PROP_POS_MSEC`` and the capture is released when the window is done.
"""

from __future__ import annotations

import logging
import math
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import cv2
import numpy as np

from .. import paths
from ..models import FaceTrackPoint, VideoInfo
from .errors import CancelledError

log = logging.getLogger("corteia")

# --------------------------------------------------------------------------- #
# public constants (documented tuning)
# --------------------------------------------------------------------------- #
YUNET_FILE = "face_detection_yunet_2023mar.onnx"

#: Detector input: the longest side is capped, so a 4K frame costs about as
#: much as a 960 px one. Boxes are mapped back to full resolution afterwards.
DETECT_MAX_SIDE = 960
DETECT_MAX_SIDE_LOW_RAM = 640

#: YuNet confidence needed to keep a box. 0.6 is the OpenCV default; 0.55 keeps
#: mildly turned faces without the false positives random noise produces.
SCORE_THRESHOLD = 0.55
NMS_THRESHOLD = 0.3
TOP_K = 5000
MIN_FACE_PX = 48

# --- association / tracking ------------------------------------------------
IOU_MIN = 0.12            #: minimum overlap to reuse a track
CENTER_MATCH_NORM = 0.06  #: IoU fallback: centres closer than 6% of the frame
BOX_EMA_ALPHA = 0.45      #: blend of detection vs. prediction for the box
VEL_EMA_ALPHA = 0.35      #: blend of the measured step into the velocity
MAX_MISSING_SAMPLES = 4   #: ~0.7 s at 6 fps before a track is considered gone
MIN_TRACK_HITS = 2        #: detections a track needs before it is reported

# --- visual speech ---------------------------------------------------------
MOUTH_ROI_W = 32          #: normalised mouth patch size (px)
MOUTH_ROI_H = 24
DIFF_WEIGHT = 0.7         #: weight of the instantaneous mean-abs difference
VAR_WEIGHT = 0.3          #: weight of the local temporal variance of that diff
#: Minimum spread of the pooled difference distribution, in mean-abs intensity
#: units (0..1). A face that only breathes sits around 0.001-0.003, visible
#: talking is >= 0.008, so this floor stops a static shot from being stretched
#: across the whole 0..1 range.
SPEAK_MIN_SPREAD = 0.006
SPEAK_P_LOW = 10.0
SPEAK_P_HIGH = 90.0

# --- jitter suppression (merge_tracks) ------------------------------------
MEDIAN_WINDOW = 3
#: Hard limit of centre movement per sample, in normalised frame units.
#: 0.08 per sample at 6 fps == 48% of the frame height per second.
MAX_STEP_NORM = 0.08

HAAR_CASCADE_FILE = "haarcascade_frontalface_default.xml"

_DEFAULT_PROGRESS_TEXT = "analisando rostos"


# --------------------------------------------------------------------------- #
# Face
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class Face:
    """One detection, in *source pixel* coordinates."""

    x: float
    y: float
    w: float
    h: float
    score: float = 0.0
    #: 5x2 float32: left eye, right eye, nose tip, mouth left, mouth right.
    #: All zeros when the detector provides no landmarks (Haar fallback).
    landmarks: np.ndarray = field(default_factory=lambda: np.zeros((5, 2), np.float32))

    @property
    def cx(self) -> float:
        """Centre X in source pixels."""
        return self.x + self.w / 2.0

    @property
    def cy(self) -> float:
        """Centre Y in source pixels."""
        return self.y + self.h / 2.0

    @property
    def area(self) -> float:
        return max(0.0, self.w) * max(0.0, self.h)

    @property
    def aspect(self) -> float:
        """Box width / height (YuNet frontal boxes are close to square)."""
        return self.w / self.h if self.h else 0.0

    @property
    def rect(self) -> tuple[int, int, int, int]:
        """Rounded ``(x, y, w, h)``; callers are responsible for clamping."""
        return int(round(self.x)), int(round(self.y)), int(round(self.w)), int(round(self.h))


# --------------------------------------------------------------------------- #
# detector lifecycle
# --------------------------------------------------------------------------- #
_lock = threading.Lock()
_detector = None                      # cv2.FaceDetectorYN | None
_haar = None                          # cv2.CascadeClassifier | None
_detector_state = "uninitialised"     # last initialisation outcome (-> note)
_init_checked = False


def _max_side() -> int:
    """Detector input cap, lowered on small machines."""
    try:  # pragma: no cover - hardware probing is environment dependent
        from ..hardware import probe

        rep = probe()
        if rep.ram_gb and rep.ram_gb < 8.0:
            return DETECT_MAX_SIDE_LOW_RAM
    except Exception:                 # noqa: BLE001 - never fail on probing
        pass
    return DETECT_MAX_SIDE


def _get_yunet():
    """Lazily build the YuNet detector; ``None`` when it cannot run here."""
    global _detector, _detector_state, _init_checked
    if _detector is not None:
        return _detector
    with _lock:
        if _detector is not None:
            return _detector
        _init_checked = True
        if not hasattr(cv2, "FaceDetectorYN"):
            _detector_state = f"este OpenCV ({cv2.__version__}) não tem cv2.FaceDetectorYN"
            return None
        model = paths.bundled_model(YUNET_FILE)
        if not model.is_file():
            _detector_state = f"modelo YuNet ausente em {model}"
            return None
        try:
            _detector = cv2.FaceDetectorYN.create(
                str(model), "", (320, 320), SCORE_THRESHOLD, NMS_THRESHOLD, TOP_K
            )
            _detector_state = "yu-net"
        except Exception as exc:      # noqa: BLE001
            _detector_state = f"falha ao criar o detector YuNet: {exc}"
            _detector = None
        return _detector


def _haar_cascade_path() -> Path | None:
    """Absolute path of the bundled Haar cascade, or ``None`` when absent."""
    base = getattr(cv2, "data", None)
    cascade_dir = getattr(base, "haarcascades", "") if base else ""
    if not cascade_dir:
        return None
    xml = Path(str(cascade_dir)) / HAAR_CASCADE_FILE
    return xml if xml.is_file() else None


def _get_haar():
    """Optional OpenCV-4 fallback: Haar cascade, only when the XML exists."""
    global _haar
    if _haar is not None:
        return _haar
    if not hasattr(cv2, "CascadeClassifier"):
        return None
    with _lock:
        if _haar is not None:
            return _haar
        xml = _haar_cascade_path()
        if xml is None:
            return None
        try:
            clf = cv2.CascadeClassifier(str(xml))
            _haar = None if clf.empty() else clf
        except Exception:             # noqa: BLE001
            _haar = None
        return _haar


def detector_available() -> bool:
    """True when *some* detector can run here (YuNet model present, or Haar)."""
    if _detector is not None or _haar is not None:
        return True
    if hasattr(cv2, "FaceDetectorYN") and paths.bundled_model(YUNET_FILE).is_file():
        return True
    return _get_yunet() is not None or _get_haar() is not None


def detector_name() -> str:
    """Backend in use: ``yu-net``, ``haar`` or ``""`` when there is none."""
    if _detector is not None:
        return "yu-net"
    if _haar is not None:
        return "haar"
    if _get_yunet() is not None:
        return "yu-net"
    if _get_haar() is not None:
        return "haar"
    return ""


def detector_note() -> str:
    """Portuguese explanation of a detection fallback.

    ``reframe.plan_crop`` copies this into ``CropPlan.note`` so the UI can be
    honest about why faces were not used instead of inventing motion.
    """
    if not _init_checked and _detector is None and _haar is None:
        detector_available()
    if _detector is not None:
        return ""
    if _haar is not None:
        return "Detector YuNet indisponível; enquadramento por Haar clássico."
    return f"Nenhum detector facial disponível ({_detector_state}); enquadramento por saliência."


# --------------------------------------------------------------------------- #
# detection
# --------------------------------------------------------------------------- #
def _to_bgr(frame: np.ndarray) -> np.ndarray:
    if frame.ndim == 2:
        return cv2.cvtColor(frame, cv2.COLOR_GRAY2BGR)
    if frame.shape[2] == 4:
        return cv2.cvtColor(frame, cv2.COLOR_BGRA2BGR)
    return frame


def _yunet_faces(frame: np.ndarray, min_size: int, score_threshold: float) -> list[Face]:
    det = _get_yunet()
    if det is None:
        return []
    src = np.ascontiguousarray(_to_bgr(frame))
    h0, w0 = src.shape[:2]
    if h0 < 16 or w0 < 16:
        return []
    scale = min(1.0, float(_max_side()) / float(max(h0, w0)))
    if scale < 1.0:
        rw = max(32, int(round(w0 * scale)))
        rh = max(32, int(round(h0 * scale)))
        small = cv2.resize(src, (rw, rh), interpolation=cv2.INTER_AREA)
    else:
        rw, rh, small = w0, h0, src
    try:
        # cv2.FaceDetectorYN is a single mutable network: setInputSize + detect
        # must be atomic, or two exports running at once (the default
        # proc.max_parallel_jobs is 2) corrupt its shapes and OpenCV raises
        # "buf.shape() == m.shape()" -- after which face tracking quietly
        # returns nothing and every crop falls back to motion saliency.
        with _lock:
            det.setInputSize((rw, rh))
            _ret, raw = det.detect(small)
    except cv2.error as exc:          # noqa: BLE001
        log.warning("detecção facial falhou: %s", exc)
        return []
    if raw is None:
        return []
    arr = np.asarray(raw, dtype=np.float32)
    if arr.ndim != 2 or arr.shape[0] == 0:
        return []
    inv = 1.0 / scale if scale else 1.0
    out: list[Face] = []
    for row in arr:
        x, y, w, h = (float(row[0]), float(row[1]), float(row[2]), float(row[3]))
        score = float(row[14]) if row.shape[0] > 14 else 1.0
        if score < score_threshold or w <= 0 or h <= 0:
            continue
        X, Y, W, H = x * inv, y * inv, w * inv, h * inv
        if min(W, H) < float(min_size):
            continue
        # the detector can overshoot by a pixel or two near the border
        X = max(0.0, min(X, w0 - 1.0))
        Y = max(0.0, min(Y, h0 - 1.0))
        W = max(1.0, min(W, w0 - X))
        H = max(1.0, min(H, h0 - Y))
        lm = np.zeros((5, 2), np.float32)
        if row.shape[0] >= 14:
            pts = row[4:14].reshape(5, 2) * inv
            lm = np.clip(pts, [[0.0, 0.0]], [[float(w0), float(h0)]]).astype(np.float32)
        out.append(Face(x=X, y=Y, w=W, h=H, score=score, landmarks=lm))
    out.sort(key=lambda f: -f.area)
    return out


def _haar_faces(frame: np.ndarray, min_size: int) -> list[Face]:
    clf = _get_haar()
    if clf is None:
        return []
    src = _to_bgr(frame)
    gray = cv2.cvtColor(src, cv2.COLOR_BGR2GRAY)
    h0, w0 = gray.shape[:2]
    scale = min(1.0, float(_max_side()) / float(max(h0, w0)))
    small = gray if scale >= 1.0 else cv2.resize(
        gray, (max(32, int(w0 * scale)), max(32, int(h0 * scale))), interpolation=cv2.INTER_AREA)
    found = clf.detectMultiScale(small, scaleFactor=1.15, minNeighbors=5,
                                 minSize=(max(16, int(min_size * scale)),) * 2)
    inv = 1.0 / scale if scale else 1.0
    out: list[Face] = []
    for row in np.asarray(found, dtype=np.float32).reshape(-1, 4):
        x, y, w, h = (float(row[0]), float(row[1]), float(row[2]), float(row[3]))
        X, Y, W, H = x * inv, y * inv, w * inv, h * inv
        if min(W, H) < min_size:
            continue
        out.append(Face(x=X, y=Y, w=W, h=H, score=0.6,
                        landmarks=np.zeros((5, 2), np.float32)))
    out.sort(key=lambda f: -f.area)
    return out


def detect_faces(frame: np.ndarray, min_size: int = MIN_FACE_PX, *,
                 score_threshold: float = SCORE_THRESHOLD) -> list[Face]:
    """Detect faces in a BGR/gray frame. Never raises: no face means ``[]``.

    Order: YuNet ONNX -> Haar cascade (OpenCV 4 only) -> empty list.
    """
    if frame is None or not isinstance(frame, np.ndarray) or frame.ndim < 2:
        return []
    faces = _yunet_faces(frame, max(1, int(min_size)), score_threshold)
    if faces:
        return faces
    if _get_haar() is not None:
        return _haar_faces(frame, max(1, int(min_size)))
    return faces


# --------------------------------------------------------------------------- #
# visual speech (mouth motion)
# --------------------------------------------------------------------------- #
def _mouth_roi(face: Face, gray: np.ndarray,
               size: tuple[int, int] = (MOUTH_ROI_W, MOUTH_ROI_H)) -> np.ndarray | None:
    """Normalised mouth patch (float32 0..1), or ``None`` when unusable.

    Uses the mouth landmarks when the detector provided them, otherwise the
    lower third of the box. All bounds are clamped to the frame so a face at
    the border can never crash the analysis.
    """
    gh, gw = gray.shape[:2]
    lm = face.landmarks
    if lm is not None and lm.shape[0] >= 5 and float(np.abs(lm).sum()) > 0.0:
        ml, mr = lm[3], lm[4]
        cx = float((ml[0] + mr[0]) / 2.0)
        cy = float((ml[1] + mr[1]) / 2.0) + face.h * 0.02
        span = float(np.hypot(ml[0] - mr[0], ml[1] - ml[1]))
        rw = span * 1.15 if span > 1.0 else face.w * 0.50
        rh = face.h * 0.24
    else:
        cx = face.cx
        cy = face.y + face.h * 0.80
        rw = face.w * 0.50
        rh = face.h * 0.30
    rw = max(6.0, min(rw, face.w * 0.90, gw * 0.9))
    rh = max(5.0, min(rh, face.h * 0.60, gh * 0.9))
    x0 = int(max(0, math.floor(cx - rw / 2.0)))
    x1 = int(min(gw, math.ceil(cx + rw / 2.0)))
    y0 = int(max(0, math.floor(cy - rh / 2.0)))
    y1 = int(min(gh, math.ceil(cy + rh / 2.0)))
    if x1 - x0 < 4 or y1 - y0 < 3:
        return None
    patch = gray[y0:y1, x0:x1]
    if patch.size == 0:
        return None
    patch = cv2.GaussianBlur(patch, (3, 3), 0)
    patch = cv2.resize(patch, size, interpolation=cv2.INTER_AREA)
    return patch.astype(np.float32) / 255.0


def _speaking_from_tracks(tracks: list["_Track"]) -> dict[int, list[float]]:
    """Map each track's mouth deltas to 0..1 with a *pooled* robust scale.

    Pooling across tracks is deliberate: track selection compares the
    ``speaking`` value of two people at the same instant, so normalising every
    track on its own would give the silent one a 1.0 as well.
    """
    raw_by_track: dict[int, np.ndarray] = {}
    pooled: list[float] = []
    for tr in tracks:
        if not tr.diffs:
            continue
        d = np.asarray(tr.diffs, np.float32)
        local = np.array([d[max(0, i - 1):i + 2].std() for i in range(len(d))], np.float32)
        raw = DIFF_WEIGHT * d + VAR_WEIGHT * local
        raw_by_track[tr.track_id] = raw
        pooled.extend(raw.tolist())
    if not pooled:
        return {tid: [0.0] * len(v) for tid, v in raw_by_track.items()}
    series = np.asarray(pooled, np.float32)
    lo = float(np.percentile(series, SPEAK_P_LOW))
    hi = float(np.percentile(series, SPEAK_P_HIGH))
    if hi - lo < SPEAK_MIN_SPREAD:
        hi = lo + SPEAK_MIN_SPREAD
    out: dict[int, list[float]] = {}
    for tid, raw in raw_by_track.items():
        norm = (raw - lo) / max(1e-9, hi - lo)
        out[tid] = np.clip(norm, 0.0, 1.0).tolist()
    return out


# --------------------------------------------------------------------------- #
# association tracker
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class _Track:
    """Mutable tracker state for one person (source pixels)."""

    track_id: int
    x: float
    y: float
    w: float
    h: float
    vx: float = 0.0
    vy: float = 0.0
    last_t: float = 0.0
    missing: int = 0
    hits: int = 1
    prev_roi: np.ndarray | None = None
    times: list[float] = field(default_factory=list)
    boxes: list[tuple[float, float, float, float, float]] = field(default_factory=list)
    diffs: list[float] = field(default_factory=list)

    def predicted(self, dt_samples: float) -> tuple[float, float, float, float]:
        p = max(0.0, min(6.0, dt_samples))
        return (self.x + self.vx * p, self.y + self.vy * p, self.w, self.h)

    def centre(self) -> tuple[float, float]:
        return self.x + self.w / 2.0, self.y + self.h / 2.0


def _iou(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> float:
    ax0, ay0, aw, ah = a
    bx0, by0, bw, bh = b
    iw = max(0.0, min(ax0 + aw, bx0 + bw) - max(ax0, bx0))
    ih = max(0.0, min(ay0 + ah, by0 + bh) - max(ay0, by0))
    inter = iw * ih
    union = aw * ah + bw * bh - inter
    return inter / union if union > 0 else 0.0


def _assign(pred: list[tuple[float, float, float, float]], faces: list[Face],
            frame_diag: float = 0.0) -> list[int]:
    """Greedy IoU matching. Returns detection -> track index (-1 = new track).

    A centre-distance affinity is kept as a tie-breaker: with a large motion
    step or a heavily occluded face the IoU can fall below ``IOU_MIN`` while
    the centre is obviously the same person, and losing the track would reset
    both the mouth history and the speech signal.
    """
    pairs: list[tuple[float, int, int]] = []
    diag = frame_diag or 1.0
    for i, p in enumerate(pred):
        pcx, pcy = p[0] + p[2] / 2.0, p[1] + p[3] / 2.0
        for j, f in enumerate(faces):
            score = _iou(p, (f.x, f.y, f.w, f.h))
            if score < IOU_MIN:
                dist = math.hypot(pcx - f.cx, pcy - f.cy) / diag
                size = max(p[2], p[3]) / max(1.0, max(f.w, f.h))
                if dist < CENTER_MATCH_NORM and 0.5 < size < 2.0:
                    score = max(score, IOU_MIN + 0.05 * (1.0 - dist / CENTER_MATCH_NORM))
            if score >= IOU_MIN:
                pairs.append((score, i, j))
    pairs.sort(key=lambda p: (-p[0], p[1], p[2]))
    used_t: set[int] = set()
    used_f: set[int] = set()
    out = [-1] * len(faces)
    for _score, i, j in pairs:
        if i in used_t or j in used_f:
            continue
        used_t.add(i)
        used_f.add(j)
        out[j] = i
    return out


# --------------------------------------------------------------------------- #
# smoothing / de-jitter
# --------------------------------------------------------------------------- #
def merge_tracks(points: list[FaceTrackPoint]) -> list[FaceTrackPoint]:
    """De-jitter a sample series: median of 3, then a per-sample step clamp.

    Points are grouped by ``track`` (a negative id is treated as one group),
    sorted by time and rebuilt. ``score``/``speaking`` are carried over
    untouched, so the speech signal is never smoothed into something invented.
    """
    if not points:
        return []
    groups: dict[int, list[FaceTrackPoint]] = {}
    for p in points:
        groups.setdefault(int(p.track) if p.track is not None else -1, []).append(p)
    out: list[FaceTrackPoint] = []
    for tid, group in sorted(groups.items()):
        series: dict[float, FaceTrackPoint] = {}
        for p in sorted(group, key=lambda q: q.t):
            key = round(float(p.t), 3)
            if key not in series or p.score > series[key].score:
                series[key] = p
        ordered = [series[k] for k in sorted(series)]
        n = len(ordered)
        smoothed: list[tuple[float, float, float]] = []
        prev: tuple[float, float, float] | None = None
        for idx, p in enumerate(ordered):
            lo = max(0, idx - MEDIAN_WINDOW // 2)
            hi = min(n, idx + MEDIAN_WINDOW // 2 + 1)
            window = ordered[lo:hi]
            cx = float(np.median([q.cx for q in window]))
            cy = float(np.median([q.cy for q in window]))
            w = float(np.median([q.w for q in window]))
            if prev is not None:
                cx = prev[0] + max(-MAX_STEP_NORM, min(MAX_STEP_NORM, cx - prev[0]))
                cy = prev[1] + max(-MAX_STEP_NORM, min(MAX_STEP_NORM, cy - prev[1]))
            prev = (min(1.0, max(0.0, cx)), min(1.0, max(0.0, cy)),
                    min(1.0, max(0.0, w)))
            smoothed.append(prev)
        for p, (cx, cy, w) in zip(ordered, smoothed):
            out.append(FaceTrackPoint(t=float(p.t), cx=cx, cy=cy, w=w,
                                      score=float(p.score), speaking=float(p.speaking),
                                      track=tid))
    out.sort(key=lambda p: (p.t, p.track))
    return out


# --------------------------------------------------------------------------- #
# clip analysis
# --------------------------------------------------------------------------- #
def _sample_times(t0: float, t1: float, sample_fps: float, duration: float) -> list[float]:
    """Sample grid inside ``[t0, t1)`` at ``sample_fps``, clamped to duration."""
    step = 1.0 / max(0.5, float(sample_fps))
    if duration > 0:
        t1 = min(t1, duration)
    if t1 <= t0:
        return []
    times: list[float] = []
    t = float(t0)
    while t < t1 - 1e-6 and len(times) < 100000:
        times.append(round(t, 3))
        t += step
    if not times:
        times = [round(float(t0), 3)]
    return times


def analyze_clip_track(path, info: VideoInfo, t0: float, t1: float, *,
                       sample_fps: float = 6.0, min_size: int = MIN_FACE_PX,
                       on_progress: Callable[[float, str], None] | None = None,
                       cancel: threading.Event | None = None) -> list[FaceTrackPoint]:
    """Track faces across ``[t0, t1]`` of *path* and measure visual speech.

    Returns :class:`FaceTrackPoint` samples ordered by time with normalised
    ``cx``/``cy``/``w`` (0..1 of the source frame), a detector ``score`` and a
    0..1 ``speaking`` value coming from mouth-region motion. An empty list
    means "no face found" — that is not an error: ``reframe`` decides the rest.
    """
    times = _sample_times(float(t0), float(t1), sample_fps,
                          float(getattr(info, "duration", 0.0) or 0.0))
    if not times:
        return []
    src = str(path)
    cap = cv2.VideoCapture(src)
    if not cap.isOpened():
        log.warning("não foi possível abrir %s para análise facial", src)
        return []
    tracks: list[_Track] = []
    next_id = 0
    total = len(times)
    frame_size: tuple[int, int] | None = None
    prev_t: float | None = None
    try:
        for idx, t in enumerate(times):
            if cancel is not None and cancel.is_set():
                raise CancelledError()
            cap.set(cv2.CAP_PROP_POS_MSEC, max(0.0, t) * 1000.0)
            ok, frame = cap.read()
            if not ok or frame is None:
                if on_progress:
                    on_progress((idx + 1) / total, _DEFAULT_PROGRESS_TEXT)
                continue
            if frame_size is None:
                frame_size = (int(frame.shape[1]), int(frame.shape[0]))
            fw, fh = int(frame.shape[1]), int(frame.shape[0])
            faces = detect_faces(frame, min_size)
            dt_samples = 1.0 if prev_t is None else max(0.5, (t - prev_t) * sample_fps)
            prev_t = t
            pred = [tr.predicted(dt_samples) for tr in tracks]
            assign = _assign(pred, faces, math.hypot(fw, fh))

            gray: np.ndarray | None = None   # decoded lazily, only if a face exists
            for j, face in enumerate(faces):
                ti = assign[j]
                if ti >= 0:
                    tr = tracks[ti]
                    px, py, pw, ph = pred[ti]
                    a = BOX_EMA_ALPHA
                    nx = a * face.x + (1.0 - a) * px
                    ny = a * face.y + (1.0 - a) * py
                    nw = a * face.w + (1.0 - a) * pw
                    nh = a * face.h + (1.0 - a) * ph
                    ocx, ocy = tr.centre()
                    v = VEL_EMA_ALPHA
                    tr.vx = (1.0 - v) * tr.vx + v * ((nx + nw / 2.0 - ocx) / max(1.0, dt_samples))
                    tr.vy = (1.0 - v) * tr.vy + v * ((ny + nh / 2.0 - ocy) / max(1.0, dt_samples))
                    tr.x, tr.y, tr.w, tr.h = nx, ny, nw, nh
                    tr.missing = 0
                    tr.hits += 1
                else:
                    tr = _Track(track_id=next_id, x=face.x, y=face.y, w=face.w, h=face.h)
                    next_id += 1
                    tracks.append(tr)
                tr.last_t = t
                cx, cy = tr.centre()
                tr.times.append(t)
                tr.boxes.append((cx, cy, tr.w, tr.h, face.score))
                if tr.w > 0:
                    if gray is None:
                        gray = cv2.cvtColor(_to_bgr(frame), cv2.COLOR_BGR2GRAY)
                    roi = _mouth_roi(face, gray)
                    if roi is None:
                        tr.diffs.append(tr.diffs[-1] if tr.diffs else 0.0)
                    elif tr.prev_roi is None or tr.prev_roi.shape != roi.shape:
                        tr.prev_roi = roi
                        tr.diffs.append(0.0)
                    else:
                        tr.diffs.append(float(np.abs(roi - tr.prev_roi).mean()))
                        tr.prev_roi = roi

            matched = {a for a in assign if a >= 0}
            for i, tr in enumerate(tracks):
                if i not in matched:
                    tr.missing += 1
            tracks = [tr for tr in tracks if tr.missing <= MAX_MISSING_SAMPLES]

            if on_progress:
                on_progress((idx + 1) / total, _DEFAULT_PROGRESS_TEXT)
    finally:
        cap.release()

    speak = _speaking_from_tracks(tracks)
    fw, fh = frame_size or (int(getattr(info, "width", 0) or 1) or 1,
                             int(getattr(info, "height", 0) or 1) or 1)
    points: list[FaceTrackPoint] = []
    for tr in tracks:
        if tr.hits < MIN_TRACK_HITS:
            continue
        values = speak.get(tr.track_id, [0.0] * len(tr.times))
        for k, (cx, cy, w, _h, score) in enumerate(tr.boxes):
            points.append(FaceTrackPoint(
                t=float(tr.times[k]),
                cx=float(cx) / fw,
                cy=float(cy) / fh,
                w=float(w) / fw,
                score=float(score),
                speaking=float(values[k]) if k < len(values) else 0.0,
                track=int(tr.track_id),
            ))
    points.sort(key=lambda p: (p.t, p.track))
    if on_progress:
        on_progress(1.0, _DEFAULT_PROGRESS_TEXT)
    log.debug("analise facial: %d amostras, %d tracks, %d pontos",
              len(times), len(tracks), len(points))
    return points
