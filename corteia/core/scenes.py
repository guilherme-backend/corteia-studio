"""Shot boundary detection with PySceneDetect (OpenCV backend, no GUI).

Scenes always cover the video from 0 to its duration: a clip needs a complete
list, not only the cut points, and a single-shot recording must come back as one
scene spanning the whole file.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path

from ..models import Scene, format_ts
from . import media
from .errors import CancelledError, ProcessingError

log = logging.getLogger("corteia")

# ContentDetector scores are absolute (default 27.0) while AdaptiveDetector works
# on the ratio against a rolling average (default 3.0). One divisor keeps both in
# step so the UI only ever exposes a single "sensitivity" number.
_ADAPTIVE_DIVISOR = 9.0


def _build_detector(threshold: float, min_scene_len: int):
    """Adaptive detector when available, ContentDetector otherwise."""
    from scenedetect import AdaptiveDetector, ContentDetector

    msl = max(1, int(min_scene_len))
    try:
        return AdaptiveDetector(
            adaptive_threshold=max(0.5, round(float(threshold) / _ADAPTIVE_DIVISOR, 3)),
            min_scene_len=msl,
        ), "AdaptiveDetector"
    except TypeError:                       # older/newer signature: fall back safely
        log.debug("AdaptiveDetector signature mudou; usando ContentDetector")
        return ContentDetector(threshold=float(threshold), min_scene_len=msl), "ContentDetector"


def _import_backend():
    """Import the scenedetect pieces we need, mapping the error to Portuguese."""
    try:
        from scenedetect import SceneManager, open_video          # noqa: F401
        from scenedetect.stats_manager import StatsManager        # noqa: F401
    except Exception as exc:                                   # noqa: BLE001
        raise ProcessingError(
            "scenes",
            "O detector de cenas (PySceneDetect) não está disponível neste ambiente.",
            str(exc),
        ) from exc


def detect_scenes(
    path: Path,
    *,
    threshold: float = 27.0,
    min_scene_len: int = 15,
    cancel: threading.Event | None = None,
    progress=None,
) -> list[Scene]:
    """Return the shots of ``path`` as ``Scene(start, end)`` in seconds.

    ``threshold`` follows PySceneDetect's content scale (27.0 default). Frames are
    converted to seconds with the ``VideoInfo`` frame rate, so the numbers agree
    with the rest of the pipeline. Never raises on "no cuts": one scene is returned.
    """
    p = Path(path)
    try:
        info = media.probe(p)
    except ProcessingError as exc:
        raise ProcessingError("scenes", f"Não foi possível abrir “{p.name}” para analisar as cenas.",
                              exc.detail) from exc
    duration = float(info.duration or 0.0)
    fps = float(info.fps) if info.fps and info.fps > 0 else 0.0

    _import_backend()
    from scenedetect import SceneManager, open_video
    from scenedetect.video_stream import VideoOpenFailure

    detector, kind = _build_detector(threshold, min_scene_len)
    try:
        video = open_video(str(p))
    except VideoOpenFailure as exc:
        raise ProcessingError("scenes", f"Não foi possível abrir “{p.name}” para analisar as cenas.",
                              str(exc)) from exc
    if fps <= 0:
        try:
            fps = float(video.frame_rate)
        except (AttributeError, TypeError, ValueError):
            fps = 30.0
    if duration <= 0:
        try:
            duration = float(video.duration.get_seconds())
        except (AttributeError, ValueError):
            duration = 0.0
    if fps <= 0:
        fps = 30.0

    state = {"last": -1.0}

    def _callback(_frame, timecode) -> None:              # pragma: no cover - thread body
        if cancel is not None and cancel.is_set():
            raise CancelledError()
        if progress is None or duration <= 0:
            return
        t = _to_seconds(timecode, fps)
        pct = max(0.0, min(1.0, t / duration))
        if pct > state["last"] + 0.01:
            state["last"] = pct
            try:
                progress(pct, f"{format_ts(t)} / {format_ts(duration)}")
            except Exception:                             # noqa: BLE001
                log.exception("callback de progresso falhou")

    manager = SceneManager()
    manager.add_detector(detector)
    try:
        manager.detect_scenes(video=video, show_progress=False, callback=_callback)
        pairs = manager.get_scene_list(start_in_scene=True)
    except CancelledError:
        log.info("Detecção de cenas cancelada em %s", p.name)
        raise
    except Exception as exc:                              # noqa: BLE001
        raise ProcessingError("scenes", "A análise de cenas falhou neste vídeo.", str(exc)) from exc
    finally:
        try:
            video.release()
        except Exception:                                  # noqa: BLE001
            pass

    scenes = _pairs_to_scenes(pairs, fps, duration)
    if cancel is not None and cancel.is_set():
        raise CancelledError()
    if progress is not None:
        try:
            progress(1.0, f"{len(scenes)} cenas")
        except Exception:                                 # noqa: BLE001
            log.exception("callback de progresso falhou")
    log.debug("scenedetect[%s]: %d cenas em %s", kind, len(scenes), p.name)
    return scenes


def _to_seconds(timecode, fps: float) -> float:
    """Frame timecode -> seconds, using the probed fps when it is trustworthy."""
    try:
        frames = float(timecode.frame_num)
    except (AttributeError, TypeError, ValueError):
        frames = -1.0
    if frames >= 0 and fps > 0:
        return frames / fps
    try:
        return float(timecode.get_seconds())
    except (AttributeError, TypeError, ValueError):
        return 0.0


def _pairs_to_scenes(pairs, fps: float, duration: float) -> list[Scene]:
    """Normalise PySceneDetect output into a gapless scene list."""
    out: list[Scene] = []
    for start, end in pairs or []:
        s = max(0.0, _to_seconds(start, fps))
        e = _to_seconds(end, fps)
        if duration > 0:
            e = min(e, duration) if e <= duration + 1.0 else duration
        if e > s:
            out.append(Scene(start=s, end=e))
    if not out:
        return [Scene(0.0, duration)] if duration > 0 else []
    out.sort(key=lambda sc: (sc.start, sc.end))
    out[0].start = 0.0
    for i in range(1, len(out)):
        # close 1-frame gaps produced by rounding at the cut boundary
        if abs(out[i].start - out[i - 1].end) <= max(0.06, 1.5 / max(fps, 1.0)):
            out[i].start = out[i - 1].end
        elif out[i].start > out[i - 1].end:
            out[i - 1].end = out[i].start
    if duration > 0:
        out[-1].end = duration          # the video cannot end before its own duration
    merged: list[Scene] = [sc for sc in out if sc.end - sc.start > 0.001]
    return merged or [Scene(0.0, duration if duration > 0 else out[-1].end)]


# --------------------------------------------------------------------------- #
# queries
# --------------------------------------------------------------------------- #
def scenes_in(scenes: list[Scene], t0: float, t1: float) -> list[Scene]:
    """Scenes overlapping ``[t0, t1]`` (returned as they are, not clipped)."""
    a, b = float(min(t0, t1)), float(max(t0, t1))
    return [s for s in scenes if s.end > a and s.start < b]


def scene_count_in(scenes: list[Scene], t0: float, t1: float) -> int:
    """How many shot changes fall inside a range (drives the "cortes" signal)."""
    return len(scenes_in(scenes, t0, t1))


def boundaries(scenes: list[Scene], tolerance: float = 0.35) -> list[float]:
    """Cut instants (seconds), useful to snap a clip edge to a real transition."""
    out: list[float] = []
    for s in sorted(scenes, key=lambda sc: sc.start):
        if out and abs(s.start - out[-1]) <= tolerance:
            continue
        out.append(s.start)
    return out
