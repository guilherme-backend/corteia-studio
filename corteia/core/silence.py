"""Silence detection and speech-interval maths (FFmpeg ``silencedetect``).

``silencedetect`` is the app's real speech-gated energy source: it reports the
windows where the track stays under a dB threshold, which is exactly what the
"jump cut" behaviour needs. Everything here parses that output — no synthetic
numbers, no fixed windows.
"""

from __future__ import annotations

import logging
import re
import threading
from pathlib import Path

from ..models import Silence
from . import ffmpeg_cli, media
from .errors import MediaError, ProcessingError

log = logging.getLogger("corteia")

_RE_START = re.compile(r"silence_start:\s*(-?\d+(?:\.\d+)?)")
_RE_END = re.compile(r"silence_end:\s*(-?\d+(?:\.\d+)?)(?:\s*\|\s*silence_duration:\s*(-?\d+(?:\.\d+)?))?")


# --------------------------------------------------------------------------- #
# detection
# --------------------------------------------------------------------------- #
def detect_silences(
    path: Path,
    *,
    db: float = -35.0,
    min_ms: int = 350,
    cancel: threading.Event | None = None,
    progress=None,
) -> list[Silence]:
    """Return every silent window of the file, clamped to ``[0, duration]``.

    Runs ``ffmpeg -af silencedetect=n=<db>dB:d=<min_ms/1000>`` and parses stderr.
    A file without audio (or a purely silent one) returns ``[]`` / one window.
    """
    p = Path(path)
    try:
        info = media.probe(p)
    except MediaError as exc:
        raise ProcessingError("silence", f"Não foi possível analisar o áudio de “{p.name}”.",
                              exc.detail) from exc
    duration = info.duration
    if not info.has_audio or not info.codec_audio:
        log.info("%s não tem faixa de áudio: nenhum silêncio.", p.name)
        return []

    cmd = [
        "ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "info", "-progress", "pipe:2",
        "-i", str(p),
        "-af", f"silencedetect=n={float(db):.1f}dB:d={max(0.05, int(min_ms) / 1000.0):.3f}",
        "-vn", "-sn", "-dn", "-f", "null", "-",
    ]
    try:
        _, tail = ffmpeg_cli.capture(
            cmd, cancel=cancel,
            total_duration=duration if duration > 0 else None,
            on_progress=progress,
        )
    except MediaError as exc:
        raise ProcessingError("silence", "A análise de silêncio falhou neste vídeo.",
                              exc.detail) from exc

    silences = parse_silencedetect(tail, duration)
    log.debug("%d silêncios em %s (n=%sdB d=%sms)", len(silences), p.name, db, min_ms)
    return silences


def parse_silencedetect(text: str, duration: float = 0.0) -> list[Silence]:
    """Parse ``silence_start:``/``silence_end: | silence_duration:`` pairs.

    Tolerant of FFmpeg gluing status lines together and of a trailing silence that
    never got an explicit ``silence_end`` (closed at ``duration`` when known).
    """
    out: list[Silence] = []
    open_start: float | None = None
    for line in text.splitlines():
        if "silence_start:" in line:
            m = _RE_START.search(line)
            if m:
                open_start = float(m.group(1))
            continue
        if "silence_end:" in line:
            m = _RE_END.search(line)
            if not m:
                continue
            end = float(m.group(1))
            explicit_dur = float(m.group(2)) if m.group(2) is not None else None
            start = open_start if open_start is not None else (
                end - explicit_dur if explicit_dur is not None else end)
            open_start = None
            if end < start:                       # out-of-order / corrupted line: skip
                continue
            out.append(Silence(start=start, end=end))
    if open_start is not None and duration > 0 and open_start < duration:
        out.append(Silence(start=open_start, end=duration))

    clean: list[Silence] = []
    for s in out:
        start = max(0.0, s.start)
        end = s.end if duration <= 0 else min(s.end, duration)
        if end > start:
            clean.append(Silence(start=start, end=end))
    clean.sort(key=lambda s: (s.start, s.end))
    return clean


# --------------------------------------------------------------------------- #
# interval helpers
# --------------------------------------------------------------------------- #
def merge_silences(silences: list[Silence], pad: float = 0.0) -> list[Silence]:
    """Sort and fuse overlapping windows (``pad`` widens each side first)."""
    if not silences:
        return []
    items = sorted((Silence(max(0.0, s.start - pad), s.end + pad) for s in silences),
                   key=lambda s: (s.start, s.end))
    merged = [items[0]]
    for cur in items[1:]:
        last = merged[-1]
        if cur.start <= last.end:
            last.end = max(last.end, cur.end)
        else:
            merged.append(cur)
    return merged


def gaps(silences: list[Silence], duration: float, min_gap: float = 0.12
         ) -> list[tuple[float, float]]:
    """Speech intervals: the complement of the silences inside ``[0, duration]``."""
    if duration <= 0:
        return []
    windows = merge_silences(silences)
    out: list[tuple[float, float]] = []
    cursor = 0.0
    for s in windows:
        if s.start > cursor:
            out.append((cursor, min(s.start, duration)))
        cursor = max(cursor, min(s.end, duration))
    if cursor < duration:
        out.append((cursor, duration))
    return [(a, b) for a, b in out if b - a >= float(min_gap)]


def inside_silence(t: float, silences: list[Silence], pad: float = 0.05) -> bool:
    """True when ``t`` falls in a silent window (optionally widened by ``pad``)."""
    for s in silences:
        if s.start - pad <= t <= s.end + pad:
            return True
    return False


def cut_points(
    silences: list[Silence],
    t0: float,
    t1: float,
    min_keep: float = 0.35,
    max_cut: int = 40,
) -> list[tuple[float, float]]:
    """Speech ranges to **keep** inside ``[t0, t1]`` (silences are what gets cut).

    Silences separated by less than ``min_keep`` are fused, so a short breath
    between two pauses does not produce a useless micro-clip; speech shorter than
    ``min_keep`` is dropped; if more than ``max_cut`` ranges survive, the longest
    ones win (returned in chronological order).
    """
    a, b = float(min(t0, t1)), float(max(t0, t1))
    if b <= a:
        return []
    clipped = [Silence(max(a, s.start), min(b, s.end)) for s in silences
               if s.end > a and s.start < b]
    clipped = [s for s in clipped if s.end > s.start]
    # fuse silences whose speech bridge is too short to keep (keeps cuts meaningful)
    windows = merge_silences(clipped, pad=0.0)
    fused: list[Silence] = []
    for cur in windows:
        if fused and cur.start - fused[-1].end < min_keep:
            fused[-1] = Silence(fused[-1].start, max(fused[-1].end, cur.end))
        else:
            fused.append(cur)
    # also absorb silences touching the edges of the range
    keep = _complement(fused, a, b)
    keep = [(s, e) for s, e in keep if e - s >= min_keep]
    if max_cut and max_cut > 0 and len(keep) > max_cut:
        keep = sorted(sorted(keep, key=lambda iv: iv[1] - iv[0], reverse=True)[:max_cut])
    return [(round(s, 3), round(e, 3)) for s, e in keep]


def _complement(silences: list[Silence], a: float, b: float) -> list[tuple[float, float]]:
    """Non-silent intervals of ``[a, b]`` given already-clipped silence windows."""
    out: list[tuple[float, float]] = []
    cursor = a
    for s in silences:
        if s.start > cursor:
            out.append((cursor, min(s.start, b)))
        cursor = max(cursor, min(s.end, b))
        if cursor >= b:
            break
    if cursor < b:
        out.append((cursor, b))
    return out


def total_speech(silences: list[Silence], duration: float) -> float:
    """Seconds of actual speech in a track of ``duration`` seconds."""
    if duration <= 0:
        return 0.0
    silent = sum(min(s.end, duration) - max(0.0, s.start) for s in merge_silences(silences)
                 if min(s.end, duration) > max(0.0, s.start))
    silent = max(0.0, min(silent, duration))
    return max(0.0, duration - silent)


def speech_ratio(silences: list[Silence], t0: float, t1: float) -> float:
    """Fraction of ``[t0, t1]`` that is speech (0..1); 0.0 for an empty range."""
    a, b = float(min(t0, t1)), float(max(t0, t1))
    span = b - a
    if span <= 0:
        return 0.0
    ratio = (total_speech(silences, b) - total_speech(silences, a)) / span
    return max(0.0, min(1.0, ratio))
