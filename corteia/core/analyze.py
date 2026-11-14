"""Automatic clip planning: transcript → self-contained candidate windows → score.

The pipeline is deliberately explicit and auditable:

1. **Blocks** — consecutive transcript segments are merged into utterance blocks,
   broken on long silences, on topic changes (lexical similarity between
   neighbouring blocks) and on the configured maximum duration.  A block is the
   smallest unit a cut may start or end on, which is what prevents sentences from
   being sliced in half.
2. **Candidates** — every block sequence whose duration fits
   ``[target_min, target_max]`` becomes a candidate.
3. **Signals** — each candidate is described by named signals (hook, emotion,
   story arc, energy, scene changes, speakers, completeness, duration fit,
   silence load), each normalised 0..1 across the candidate set.
4. **Score** — a documented weighted sum mapped to 0..100.  With an LLM
   configured, the model's ranking is fused with the local signals instead of
   replacing them; without one, the local model still produces real numbers.
5. **Selection** — greedy top-N with overlap suppression, then boundary
   snapping against silences so nothing is cut mid-word.
"""

from __future__ import annotations

import logging
import math
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Sequence

from ..models import (AnalysisOptions, Clip, ClipStatus, CropPlan, ScoreBreakdown,
                      Transcript, VideoInfo, Word, Scene, Silence)
from . import lexicon

log = logging.getLogger("corteia.analyze")

GAP_BLOCK = 1.25          # s of silence that ends a block
BLOCK_MAX_SLACK = 1.8     # s of overshoot tolerated to keep a sentence whole
EDGE_LEAD_IN = 0.12       # s before the first word
EDGE_TAIL_OUT = 0.22      # s after the last word
SENT_END_RE = re.compile(r"[.!?…]$")
SENT_MID_RE = re.compile(r"[.?…]\s+[A-ZÀ-Ú0-9\"']")


@dataclass(slots=True)
class Block:
    start: float
    end: float
    text: str
    seg_indexes: list[int] = field(default_factory=list)
    speakers: set[int] = field(default_factory=set)
    topic_break: bool = False

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


@dataclass(slots=True)
class Candidate:
    start: float
    end: float
    blocks: list[int]
    text: str
    signals: dict[str, float] = field(default_factory=dict)
    raw: dict[str, float] = field(default_factory=dict)
    terms: dict[str, list[str]] = field(default_factory=dict)
    score: float = 0.0
    title: str = ""
    description: str = ""
    keywords: list[str] = field(default_factory=list)
    reason: str = ""
    scene_count: int = 0
    speaker_count: int = 0
    bd: ScoreBreakdown = field(default_factory=ScoreBreakdown)

    def text_key(self) -> str:
        return f"{self.start:.2f}-{self.end:.2f}"

    @property
    def duration(self) -> float:
        return self.end - self.start

    def iou(self, other: "Candidate") -> float:
        a0, a1, b0, b1 = self.start, self.end, other.start, other.end
        inter = max(0.0, min(a1, b1) - max(a0, b0))
        union = max(0.0, (a1 - a0) + (b1 - b0) - inter)
        return inter / union if union else 0.0


# --------------------------------------------------------------------------- #
# 1. blocks
# --------------------------------------------------------------------------- #
# Why topic segmentation is NOT lexical here: we measured cosine similarity of
# stopword-filtered TF-IDF windows on Portuguese conversation data and it had
# no separation between same-topic and different-topic pairs (intra median
# 0.042 vs inter median 0.044).  Spoken language reuses vocabulary, so a
# lexical "the subject changed" detector would be theatre.  Real boundaries come
# from pauses, speaker changes, scene cuts and — when an API is configured —
# from the language model (see :mod:`corteia.core.llm`).
def build_blocks(transcript: Transcript, silences: Sequence[Silence] | None = None, *,
                 gap: float = GAP_BLOCK, max_len: float = 0.0,
                 topic_breaks: Sequence[float] | None = None,
                 scene_breaks: Sequence[float] | None = None,
                 merge_speaker_change: bool = True) -> list[Block]:
    """Merge segments into utterance blocks — the atomic unit of a cut.

    A block ends when: a pause longer than *gap* occurs, an explicit topic or
    scene boundary falls inside, the block grew past *max_len*, or it already
    closes a sentence and is long enough to stand alone.
    """
    boundaries = sorted(float(b) for b in (topic_breaks or []))
    scene_b = sorted(float(b) for b in (scene_breaks or []))
    # a hard cut in the picture usually means a new segment in a live/stream
    boundaries = sorted(set([round(b, 1) for b in boundaries]
                            + [round(b, 1) for b in scene_b if scene_b]))
    bi = 0
    blocks: list[Block] = []
    for idx, seg in enumerate(transcript.segments):
        text = (seg.text or "").strip()
        if not text:
            continue
        cur = blocks[-1] if blocks else None
        if cur is None:
            blocks.append(_new_block(seg, idx))
            continue
        while bi < len(boundaries) and boundaries[bi] < cur.start:
            bi += 1
        crossed = bi < len(boundaries) and boundaries[bi] < seg.start
        reason = ""
        if seg.start - cur.end > gap:
            reason = "gap"
        elif crossed:
            reason = "topic"
        elif max_len and cur.duration + (seg.end - seg.start) > max_len * 1.35:
            reason = "length"
        elif merge_speaker_change and seg.speaker is not None and \
                len(cur.speakers) >= 3 and seg.speaker not in cur.speakers:
            reason = "speakers"
        elif SENT_END_RE.search(cur.text.strip()) and len(cur.text.split()) > 25:
            reason = "sentences"
        if reason:
            nb = _new_block(seg, idx)
            nb.topic_break = reason == "topic"
            blocks.append(nb)
        else:
            cur.end = max(cur.end, seg.end)
            cur.text = (cur.text + " " + text).strip()
            cur.seg_indexes.append(idx)
            if seg.speaker is not None:
                cur.speakers.add(seg.speaker)
    if not blocks:
        return []
    # recompute speakers per block from segment list
    return [b for b in blocks if b.duration > 0.05 and b.text]


def _new_block(seg, idx: int) -> Block:
    sp = {seg.speaker} if seg.speaker is not None else set()
    return Block(seg.start, seg.end, (seg.text or "").strip(), [idx], sp, False)


def scene_boundaries(scenes: Sequence[Scene], *, min_len: float = 3.0) -> list[float]:
    """Start times of scenes long enough to look like real segments.

    Fast-cut montages would otherwise produce a boundary every second, so short
    scenes are ignored — this keeps the proxy honest for podcasts and lives.
    """
    return [s.start for s in scenes if s.end - s.start >= min_len]

# --------------------------------------------------------------------------- #
# 2. candidates
# --------------------------------------------------------------------------- #
WEIGHTS: dict[str, float] = {
    "hook": 0.15,
    "emotion": 0.14,
    "story_arc": 0.10,
    "completeness": 0.12,
    "debate": 0.07,
    "energy": 0.10,
    "visual": 0.05,
    "density": 0.07,
    "duration_fit": 0.11,
    "speakers": 0.04,
    "silence_penalty": 0.05,
}


def target_range(options: AnalysisOptions) -> tuple[float, float]:
    """Acceptable clip span for the current settings."""
    if options.preset_duration:
        d = float(options.preset_duration)
        return max(5.0, d * 0.80), d * 1.12
    lo, hi = float(options.target_min), float(options.target_max)
    if hi < lo:
        lo, hi = hi, lo
    return max(5.0, lo), max(lo + 2.0, hi)


def enumerate_candidates(blocks: list[Block], options: AnalysisOptions,
                         *, max_span_blocks: int = 16) -> list[Candidate]:
    """Every block sequence whose span fits the requested duration window."""
    lo, hi = target_range(options)
    out: list[Candidate] = []
    n = len(blocks)
    for i in range(n):
        for j in range(i, min(n, i + max_span_blocks)):
            dur = blocks[j].end - blocks[i].start
            if dur > hi:
                break
            if dur < lo:
                continue
            text = " ".join(b.text for b in blocks[i:j + 1])
            out.append(Candidate(start=blocks[i].start, end=blocks[j].end,
                                 blocks=list(range(i, j + 1)), text=text))
    return out


# --------------------------------------------------------------------------- #
# 3. signals
# --------------------------------------------------------------------------- #
def _silence_load(silences: Sequence[Silence], t0: float, t1: float) -> float:
    if t1 <= t0:
        return 0.0
    acc = sum(max(0.0, min(t1, s.end) - max(t0, s.start)) for s in silences)
    return min(1.0, acc / (t1 - t0))


def _scene_count(scenes: Sequence[Scene], t0: float, t1: float) -> int:
    return sum(1 for s in scenes if s.start < t1 and s.end > t0)


def _window_db(curve, t0: float, t1: float) -> tuple[float, float]:
    """``(mean dBFS, std dBFS)`` inside the window; ``(0, 0)`` when unavailable."""
    if curve is None:
        return 0.0, 0.0
    times, db = curve[0], curve[1]
    if len(times) == 0:
        return 0.0, 0.0
    import numpy as np
    sel = db[(times >= t0) & (times < t1)]
    if sel.size == 0:
        return 0.0, 0.0
    return float(np.mean(sel)), float(np.std(sel))


def compute_signals(cands: list[Candidate], blocks: list[Block], options: AnalysisOptions,
                    *, silences: Sequence[Silence] | None = None,
                    scenes: Sequence[Scene] | None = None,
                    curve=None) -> None:
    """Fill ``raw``, ``signals`` and ``terms`` on every candidate (in place)."""
    sil_list = list(silences or [])
    sc_list = list(scenes or [])
    lo, hi = target_range(options)
    ideal = (lo + hi) / 2.0

    for c in cands:
        feats, terms = lexicon.text_features(c.text)
        c.raw = dict(feats)
        c.terms = terms
        n_words = max(1.0, feats["words"])

        head = c.text[:90]
        head_hook, _ = lexicon.hits(head, lexicon.HOOKS)
        c.raw["hook"] = feats["hook"] + head_hook * 0.6
        c.raw["story_arc"] = min(3.0, feats["story"] + feats["tension"] * 0.7
                                 + feats["payoff"] + feats["resolution"] * 0.8)

        first_block, last_block = blocks[c.blocks[0]], blocks[c.blocks[-1]]
        starts_clean = 1.0 if (first_block.topic_break
                               or not SENT_MID_RE.search(" " + first_block.text[:1] + " ")) else 0.6
        ends_clean = 1.0 if SENT_END_RE.search(last_block.text.strip()) else 0.45
        c.raw["completeness"] = starts_clean + ends_clean + (0.5 if feats["sentences"] >= 2 else 0.0)
        c.raw["debate"] = feats["debate"] + feats["question"] + feats["second_person"] * 0.4
        c.raw["density"] = feats["density"] * (1.0 + min(1.0, math.log1p(n_words / 12.0)))
        c.raw["duration_fit"] = max(0.0, 1.0 - abs(c.duration - ideal) / max(ideal, 1.0)) ** 1.5
        mean_db, spread = _window_db(curve, c.start, c.end)
        c.raw["energy"] = spread + max(0.0, mean_db + 40.0) * 0.03
        c.raw["visual"] = _scene_count(sc_list, c.start, c.end) / max(1.0, c.duration / 6.0)

        sp: set[int] = set()
        for i in c.blocks:
            sp |= blocks[i].speakers
        c.speaker_count = len(sp)
        c.scene_count = _scene_count(sc_list, c.start, c.end)
        c.raw["speakers"] = 0.0 if not sp else (1.0 if len(sp) == 2 else 0.6 if len(sp) > 2 else 0.3)
        c.raw["silence_penalty"] = _silence_load(sil_list, c.start, c.end)
        c.raw["silence_seconds"] = c.raw["silence_penalty"] * c.duration

        # Absolute (not rank-based) quality floors — see score().
        c.raw["engagement"] = (feats["hook"] + feats["emotion"] + feats["debate"]
                               + feats["question"] * 0.8 + feats["story"] * 0.9
                               + feats["resolution"] * 0.8 + feats["laughter"] * 1.1)
        content = [w for w in lexicon.tokens(c.text)
                   if w not in lexicon.STOPWORDS and len(w) > 3]
        c.raw["novelty"] = len(set(content)) / len(content) if content else 0.0
        c.raw["silence_seconds"] = round(c.raw["silence_seconds"], 2)

    _normalise(cands)


def _normalise(cands: list[Candidate]) -> None:
    """Tie-aware percentile rank of each raw signal to 0..1 across the set.

    Ties must share one value: when every window has exactly two speakers, the
    signal carries no information and a naive sort would still spread it from 0
    to 1, inventing differences that do not exist.  Equal values therefore get
    the mean of their positions, which collapses to a neutral 0.5 when all
    candidates are the same.
    """
    if not cands:
        return
    keys: set[str] = set()
    for c in cands:
        keys |= set(c.raw.keys())
    for key in keys:
        values = [c.raw.get(key, 0.0) for c in cands]
        counts = Counter(values)
        cumulative = 0.0
        position: dict[float, float] = {}
        for v in sorted(counts):
            n = counts[v]
            position[v] = (cumulative + (cumulative + n - 1)) / 2.0
            cumulative += n
        denom = max(1.0, len(values) - 1)
        for c, v in zip(cands, values):
            c.signals[key] = round(position[v] / denom, 4)
    for c in cands:
        c.signals["silence_penalty"] = round(1.0 - c.signals.get("silence_penalty", 0.0), 4)
        absolute = c.raw.get("duration_fit", 0.0)
        rank = c.signals.get("duration_fit", 0.0)
        c.signals["duration_fit"] = round(max(absolute, rank * 0.5 + absolute * 0.5), 4)


# --------------------------------------------------------------------------- #
# 4. score
# --------------------------------------------------------------------------- #
SIGNAL_LABELS: dict[str, str] = {
    "hook": "gancho de abertura",
    "emotion": "carga emocional",
    "story_arc": "arco de história",
    "completeness": "fecho completo",
    "debate": "potencial de discussão",
    "energy": "dinâmica do áudio",
    "visual": "mudanças visuais",
    "density": "densidade de informação",
    "duration_fit": "ajuste de duração",
    "speakers": "troca de falantes",
    "silence_penalty": "ausência de silêncio",
    "llm": "avaliação do modelo de linguagem",
}


def score(cands: list[Candidate], llm: dict[str, float] | None = None) -> None:
    """Weighted sum of normalised signals → 0..100, fused with the LLM if given."""
    llm = llm or {}
    denom = sum(WEIGHTS.values())
    for c in cands:
        contrib: dict[str, float] = {}
        total = 0.0
        for key, weight in WEIGHTS.items():
            v = c.signals.get(key, 0.0)
            contrib[key] = round(v, 4)
            total += v * weight
        raw_local = total / denom
        # Two absolute gates stop a content-free window from winning on audio
        # variance or scene-count rank alone: engagement (did the words carry
        # anything?) and novelty (repetition of the same three words).
        eng = c.raw.get("engagement", 0.0)
        nov = c.raw.get("novelty", 0.5)
        gate = min(1.0, 0.40 + eng / 4.0) * (0.80 + 0.20 * min(1.0, nov * 1.8))
        local = raw_local * gate
        contrib["gate"] = round(gate, 3)
        llm_val = llm.get(c.text_key())
        if llm_val is None:
            c.bd = ScoreBreakdown(total=round(local * 100.0, 2), signals=contrib,
                                  weights=dict(WEIGHTS), llm_used=False)
        else:
            fused = 0.55 * local + 0.45 * float(llm_val)
            contrib["llm"] = round(float(llm_val), 4)
            w = dict(WEIGHTS)
            w["llm"] = 0.45
            c.bd = ScoreBreakdown(total=round(fused * 100.0, 2), signals=contrib, weights=w,
                                  llm_used=True, llm_reason=str(llm.get(c.text_key() + ":why", "")))
        c.score = c.bd.total


def explain(c: Candidate, top: int = 3) -> str:
    """Human-readable justification shown in the UI tooltip."""
    ranked = sorted(((v, k) for k, v in c.bd.signals.items() if k in SIGNAL_LABELS),
                    reverse=True)[:top]
    parts = [f"{SIGNAL_LABELS.get(k, k)} {int(v * 100)}%" for v, k in ranked]
    terms = []
    for group in ("hook_terms", "emotion_terms", "resolution_terms", "debate_terms"):
        terms.extend(c.terms.get(group, [])[:2])
    if terms:
        parts.append("termos: " + ", ".join(sorted(set(terms))[:4]))
    if c.speaker_count > 1:
        parts.append(f"{c.speaker_count} falantes")
    if c.scene_count > 1:
        parts.append(f"{c.scene_count} planos")
    return " · ".join(parts) if parts else "nenhum sinal forte"


# --------------------------------------------------------------------------- #
# 5. boundaries
# --------------------------------------------------------------------------- #
def snap_boundaries(c: Candidate, blocks: list[Block], duration: float,
                    silences: Sequence[Silence] | None, *,
                    smart: bool = True, lead_in: float = EDGE_LEAD_IN,
                    tail_out: float = EDGE_TAIL_OUT,
                    word_starts: Sequence[Word] | None = None) -> tuple[float, float]:
    """Move the window edges onto natural speech boundaries.

    With *smart* off we still never start before 0 or end after the video.
    With it on, the start is pulled to the previous word's silence midpoint and
    the end is pushed to the next word's onset, so a sentence is never halved.
    """
    sil = list(silences or [])
    first, last = blocks[c.blocks[0]], blocks[c.blocks[-1]]
    start, end = first.start, last.end

    if smart:
        prev_block = blocks[c.blocks[0] - 1] if c.blocks[0] > 0 else None
        gap_from_prev = first.start - (prev_block.end if prev_block else first.start)
        start = max(0.0, first.start - min(lead_in, max(0.04, gap_from_prev * 0.45)))

        nxt = blocks[c.blocks[-1] + 1] if c.blocks[-1] + 1 < len(blocks) else None
        gap_to_next = (nxt.start - last.end) if nxt else tail_out
        allow = tail_out if (SENT_END_RE.search(last.text.strip()) or nxt is None
                             or gap_to_next > GAP_BLOCK) else min(tail_out, gap_to_next * 0.5)
        end = min(duration or end, last.end + max(0.05, allow))

        # never leave the cursor inside a removed silence: snap to its edge
        for s in sil:
            if s.start - 0.05 <= start <= s.end:
                near_start_edge = abs(start - s.start) < abs(start - s.end)
                start = s.start - 0.03 if near_start_edge else s.end + 0.03
            if s.start <= end - 0.05 <= s.end:
                end = s.start - 0.02 if (end - s.start) < (s.end - end) else s.end + 0.05
        if word_starts:
            starts = [w.start for w in word_starts if w.start < start]
            if starts and start - max(starts) < 0.6:
                start = max(starts)
    return round(max(0.0, start), 3), round(min(duration or end, end), 3)


def dedupe(cands: list[Candidate], *, threshold: float = 0.42,
           max_per_start: int = 1) -> list[Candidate]:
    """Drop windows that are essentially the same moment."""
    ordered = sorted(cands, key=lambda x: -x.score)
    kept: list[Candidate] = []
    starts: dict[int, int] = {}
    for c in ordered:
        key = int(round(c.start))
        if starts.get(key, 0) >= max_per_start:
            continue
        if any(c.iou(k) > threshold for k in kept[-40:]):
            continue
        kept.append(c)
        starts[key] = starts.get(key, 0) + 1
    return kept


def select(cands: list[Candidate], options: AnalysisOptions) -> list[Candidate]:
    """Apply the score floor, then keep the best non-overlapping windows.

    The floor is a real filter.  When *nothing* clears it we still hand back the
    single best window -- an empty result is worse than an honest one, and the
    caller gets ``fallback`` so the clip says "abaixo do mínimo" instead of
    pretending it met the bar.
    """
    passing = [c for c in cands if c.score >= options.min_score]
    pool = passing or sorted(cands, key=lambda x: -x.score)[:1]
    pool.sort(key=lambda x: -x.score)
    picked: list[Candidate] = []
    for c in pool:
        if len(picked) >= options.max_clips:
            break
        if any(c.iou(k) > 0.20 for k in picked):
            continue
        picked.append(c)
    picked.sort(key=lambda x: x.start)
    return picked


# --------------------------------------------------------------------------- #
# 6. Clip objects
# --------------------------------------------------------------------------- #
def to_clip(c: Candidate, source_path: str, options: AnalysisOptions,
            *, rank: int = 1, total: int = 1) -> Clip:
    kws = lexicon.keywords(c.text, 6)
    title = lexicon.make_title(c.text)
    if not title or title == "Momento do vídeo":
        title = f"Melhor momento {rank:02d}"
    clip = Clip(
        source_path=source_path,
        start=c.start, end=c.end,
        title=title,
        description=lexicon.describe(c.text, kws, c.duration),
        transcript=c.text,
        keywords=kws,
        score=c.bd,
        status=ClipStatus.SUGGESTED,
        reason=explain(c),
        scene_count=c.scene_count,
        speaker_count=c.speaker_count,
    )
    clip.edit.aspect = options.aspect
    return clip


def plan_clips(transcript: Transcript, *, options: AnalysisOptions,
               source_path: str = "", duration: float = 0.0,
               silences: Sequence[Silence] | None = None,
               scenes: Sequence[Scene] | None = None,
               curve=None, llm_scores: dict[str, float] | None = None,
               topic_breaks: Sequence[float] | None = None,
               progress=None) -> tuple[list[Candidate], list[Clip]]:
    """Full local analysis: blocks → candidates → signals → score → clips."""
    def step(pct: float, label: str) -> None:
        if progress:
            progress(pct, label)

    sc_list = list(scenes or [])
    blocks = build_blocks(transcript, silences,
                          max_len=options.target_max,
                          topic_breaks=topic_breaks,
                          scene_breaks=scene_boundaries(sc_list) if len(sc_list) > 2 else None)
    if not blocks:
        step(100, "nenhuma fala utilizável")
        return [], []
    step(8, f"{len(blocks)} blocos de fala")
    if len(blocks) == 1 and blocks[0].duration > options.target_max * 3:
        # a wall of continuous speech: split on sentence ends so windows exist
        blocks = _split_long(blocks[0], options.target_max)
        step(12, f"fala contínua dividida em {len(blocks)} blocos")
    cands = enumerate_candidates(blocks, options)
    step(24, f"{len(cands)} janelas candidatas")
    if not cands:
        # nothing fits: offer the best blocks as fallback windows
        cands = [Candidate(b.start, b.end, [i], b.text) for i, b in enumerate(blocks)
                 if b.duration >= 3.0]
        if not cands:
            step(100, "vídeo curto demais para a duração pedida")
            return [], []
    compute_signals(cands, blocks, options, silences=silences, scenes=scenes, curve=curve)
    step(52, "calculando sinais de engajamento")
    score(cands, llm=llm_scores)
    step(66, "pontuando")
    cands = dedupe(cands)
    chosen = select(cands, options)
    step(78, f"{len(chosen)} cortes selecionados")

    clips: list[Clip] = []
    total = len(chosen)
    for i, c in enumerate(chosen, start=1):
        s, e = snap_boundaries(c, blocks, duration, silences, smart=options.smart_boundaries)
        if e - s < 1.0:
            s, e = c.start, c.end
        c.start, c.end = s, e
        clip = to_clip(c, source_path, options, rank=i, total=total)
        if c.score < options.min_score:
            # Nada passou do mínimo: entregamos o melhor que existe, mas o
            # motivo diz isso com todas as letras para a UI poder avisar.
            clip.reason = (f"{clip.reason} · abaixo do mínimo de "
                           f"{options.min_score:.0f} (melhor disponível)").strip(" ·")
            clip.below_floor = True
        clips.append(clip)
        step(78 + (i / max(1, total)) * 20, f"corte {i}/{total}")
    step(100, "análise concluída")
    return cands, clips


def _split_long(block: Block, target_max: float) -> list[Block]:
    """Cut one long block into sentence-bounded pieces near ``target_max``."""
    sents = lexicon.sentences(block.text)
    if not sents:
        return [block]
    per = max(1, math.ceil(len(sents) / max(1.0, block.duration / max(5.0, target_max))))
    out, cursor = [], block.start
    dur_ratio = block.duration / max(1, sum(len(s) for s in sents))
    for i in range(0, len(sents), per):
        text = " ".join(sents[i:i + per])
        d = max(1.0, len(text) * dur_ratio)
        out.append(Block(cursor, min(block.end, cursor + d), text, block.seg_indexes,
                         set(block.speakers), True))
        cursor += d
    return out
