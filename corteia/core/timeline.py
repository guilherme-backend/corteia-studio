"""Mapping between source time and clip time when silences are removed.

Silence cutting changes the timeline, and both the subtitle burn-in and the
audio/video rendering must agree on it.  ``TimelineMap`` is the single source of
truth: it stores the kept source ranges and converts times in both directions.

Clip time 0 == the first kept range's start (not ``clip.start``), so a leading
silence removal also shifts the subtitles correctly.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class TimelineMap:
    ranges: list[tuple[float, float]] = field(default_factory=list)

    # ------------------------------------------------------------- factories
    @classmethod
    def full(cls, t0: float, t1: float) -> "TimelineMap":
        return cls([(max(0.0, t0), max(max(0.0, t0), t1))])

    @classmethod
    def from_silences(cls, silences, t0: float, t1: float, *, min_keep: float = 0.35,
                      pad: float = 0.06, max_cut: int = 40,
                      preserve_between: float = 0.9) -> "TimelineMap":
        """Drop silence inside ``[t0, t1]``.

        ``pad`` keeps a breath of air around speech so words are not clipped;
        ``min_keep`` discards speech islands shorter than that (they would only
        add a concat segment); ``max_cut`` limits the number of segments by
        merging the shortest silences first, which keeps the render cheap.
        """
        t0, t1 = float(t0), float(t1)
        if t1 <= t0:
            return cls([(t0, t1)])
        sil = sorted((max(t0, s.start - pad), min(t1, s.end + pad))
                     for s in silences if s.end > t0 and s.start < t1)
        merged: list[tuple[float, float]] = []
        for a, b in sil:
            if merged and a <= merged[-1][1] + 0.01:
                merged[-1] = (merged[-1][0], max(merged[-1][1], b))
            else:
                merged.append((a, b))
        if not merged:
            return cls([(t0, t1)])

        # keep = holes between silences
        keep: list[tuple[float, float]] = []
        cursor = t0
        for a, b in merged:
            if a > cursor:
                keep.append((cursor, a))
            cursor = max(cursor, b)
        if cursor < t1:
            keep.append((cursor, t1))
        keep = [(a, b) for a, b in keep if b - a >= min_keep] or [(t0, t1)]

        # reduce segment count by re-merging across the shortest gaps
        while len(keep) > max_cut:
            gaps = [(keep[i + 1][0] - keep[i][1], i) for i in range(len(keep) - 1)]
            gaps.sort()
            _, i = gaps[0]
            keep[i] = (keep[i][0], keep[i + 1][1])
            del keep[i + 1]

        obj = cls(keep)
        if obj.duration <= 0:
            return cls([(t0, t1)])
        return obj

    # ---------------------------------------------------------------- basics
    @property
    def duration(self) -> float:
        return sum(max(0.0, b - a) for a, b in self.ranges)

    @property
    def removed(self) -> float:
        span = (self.ranges[-1][1] - self.ranges[0][0]) if self.ranges else 0.0
        return max(0.0, span - self.duration)

    @property
    def cuts(self) -> int:
        return max(0, len(self.ranges) - 1)

    def is_identity(self, t0: float, t1: float) -> bool:
        return len(self.ranges) == 1 and abs(self.ranges[0][0] - t0) < 0.01 \
            and abs(self.ranges[0][1] - t1) < 0.01

    # ------------------------------------------------------------ mapping
    def src_to_clip(self, t: float) -> float | None:
        """``None`` when the instant was cut away."""
        acc = 0.0
        for a, b in self.ranges:
            if t < a:
                return None
            if t <= b:
                return acc + (t - a)
            acc += b - a
        return None

    def clip_to_src(self, t: float) -> float:
        acc = 0.0
        for a, b in self.ranges:
            d = b - a
            if t <= acc + d:
                return a + max(0.0, t - acc)
            acc += d
        return self.ranges[-1][1] if self.ranges else t

    def window(self, s0: float, s1: float) -> tuple[float, float, list[tuple[float, float]]]:
        """Map a source utterance to clip time.

        Returns ``(clip_start, clip_end, pieces)`` where *pieces* are the
        clip-relative sub-intervals that actually survived the cut.  ``clip_start``
        is ``-1`` when the whole utterance was removed.
        """
        acc = 0.0
        pieces: list[tuple[float, float]] = []
        for a, b in self.ranges:
            lo, hi = max(s0, a), min(s1, b)
            if hi > lo:
                pieces.append((acc + lo - a, acc + hi - a))
            acc += b - a
        if not pieces:
            return -1.0, -1.0, []
        return pieces[0][0], pieces[-1][1], pieces

    def src_time_at(self, src_t: float) -> float:
        """Clip time, clamped — used by the UI so the preview never breaks."""
        v = self.src_to_clip(src_t)
        if v is not None:
            return v
        best, bd = 0.0, 1e9
        for a, b in self.ranges:
            for c in (a, b):
                d = abs(c - src_t)
                if d < bd:
                    bd, best = d, self.src_to_clip(c) or 0.0
        return best

    # ------------------------------------------------------------- ffmpeg
    def trim_concat_filter(self, vlabel: str = "0:v", alabel: str = "0:a",
                           *, v_out: str = "vout", a_out: str = "aout",
                           fps: float | None = None, sar: int = 1,
                           vf_extra: str = "", af_extra: str = "") -> str:
        """Build ``trim``/``asetpts`` + ``concat`` for the kept ranges.

        ``vf_extra`` is appended to every video branch *before* the concat, which
        is how the per-segment crop from :mod:`corteia.core.reframe` is injected.
        """
        if not self.ranges:
            return ""
        if len(self.ranges) == 1 and not vf_extra and not af_extra:
            a, b = self.ranges[0]
            return (f"{vlabel}trim=start={a:.3f}:end={b:.3f},setpts=PTS-STARTPTS[{v_out}];"
                    f"{alabel}atrim=start={a:.3f}:end={b:.3f},asetpts=PTS-STARTPTS[{a_out}]")
        parts: list[str] = []
        for i, (a, b) in enumerate(self.ranges):
            v = f"{vlabel}trim=start={a:.3f}:end={b:.3f},setpts=PTS-STARTPTS"
            if fps:
                v += f",fps={fps:g}"
            v += f"{vf_extra},setsar={sar}[cv{i}]"
            parts.append(v)
            parts.append(f"{alabel}atrim=start={a:.3f}:end={b:.3f},asetpts=PTS-STARTPTS"
                         f"{af_extra}[ca{i}]")
        n = len(self.ranges)
        chain = "".join(f"[cv{i}][ca{i}]" for i in range(n))
        parts.append(f"{chain}concat=n={n}:v=1:a=1[{v_out}][{a_out}]")
        return ";".join(parts)

    def __str__(self) -> str:
        return f"TimelineMap({len(self.ranges)} seg, {self.duration:.2f}s, {self.cuts} cortes)"
