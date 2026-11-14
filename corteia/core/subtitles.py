"""Subtitle generation: word-accurate cues and an ASS (Advanced SubStation Alpha)
renderer.  ASS is used instead of SRT because it is the only widely supported
format that can burn karaoke word timing, per-word colour, outlines and boxes
through libass — and burning is exactly what vertical social video needs.

Timing contract
---------------
``transcript`` words are in **source** seconds.  A clip may drop silences, so
every emitted time is mapped through :class:`corteia.core.timeline.TimelineMap`
into **clip** seconds before being written.  ``Cue.words`` therefore always
carry clip-relative times, ready for the UI preview and for the ASS file.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from ..models import SubtitleSettings, SubtitleStyle, Transcript, Word
from . import lexicon
from .timeline import TimelineMap

log = logging.getLogger("corteia.subs")

ASS_ALIGN = {"top": 8, "center": 5, "bottom": 2}
VIRAL_PALETTE = ["#FFE400", "#22E07A", "#35C8FF", "#FF5C7A", "#FFB020"]


@dataclass(slots=True)
class Cue:
    """One rendered subtitle block, in clip-relative time."""

    start: float
    end: float
    words: list[Word] = field(default_factory=list)
    lines: list[str] = field(default_factory=list)
    highlight: set[int] = field(default_factory=set)
    speaker: int | None = None

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)

    def text(self) -> str:
        return " ".join(w.text for w in self.words) or " ".join(self.lines)


@dataclass(slots=True)
class AssDoc:
    width: int
    height: int
    style_name: str
    playres_x: int
    playres_y: int
    header: str
    dialogs: list[str]

    def text(self) -> str:
        body = "\n".join(self.dialogs)
        return (f"{self.header}\n[Events]\nFormat: Layer, Start, End, Style, Name, "
                f"MarginL, MarginR, MarginV, Effect, Text\n{body}\n")


# --------------------------------------------------------------------------- #
# presets
# --------------------------------------------------------------------------- #
def preset(style: SubtitleStyle) -> SubtitleSettings:
    """Default look of each advertised style (editable afterwards)."""
    if style is SubtitleStyle.VIRAL:
        return SubtitleSettings(
            style=style, font="Montserrat", size=0, color="#FFFFFF",
            highlight_color="#FFE400", highlight_color2="#22E07A",
            outline=5, shadow=2, border_style=1, position="center",
            max_words_per_line=3, highlight_importance=True, highlight_mode="multicolor",
            uppercase=True, reading_speed_cps=30)
    if style is SubtitleStyle.PROFESSIONAL:
        return SubtitleSettings(
            style=style, font="Arial", size=0, color="#FFFFFF",
            highlight_color="#FFE400", outline=3, shadow=1, border_style=1,
            position="bottom", max_words_per_line=8, highlight_importance=False,
            uppercase=False, reading_speed_cps=21)
    if style is SubtitleStyle.PODCAST:
        return SubtitleSettings(
            style=style, font="Inter", size=0, color="#D9DBE6",
            highlight_color="#FFFFFF", highlight_color2="#FFE400",
            outline=3, shadow=0, border_style=1, position="center",
            max_words_per_line=4, highlight_importance=True, highlight_mode="yellow",
            uppercase=True, reading_speed_cps=27)
    if style is SubtitleStyle.DYNAMIC:
        return SubtitleSettings(
            style=style, font="Montserrat", size=0, color="#FFFFFF",
            highlight_color="#FFE400", highlight_color2="#22E07A",
            outline=4, shadow=2, border_style=1, position="center",
            max_words_per_line=4, highlight_importance=True, highlight_mode="yellow",
            uppercase=True, reading_speed_cps=29)
    if style is SubtitleStyle.MINIMALIST:
        return SubtitleSettings(
            style=style, font="Inter", size=0, color="#F2F3F7",
            highlight_color="#FFE400", outline=0, shadow=2, border_style=1,
            position="bottom", max_words_per_line=9, highlight_importance=False,
            uppercase=False, reading_speed_cps=19)
    return SubtitleSettings(style=SubtitleStyle.NONE)


STYLE_PRESETS: dict[SubtitleStyle, SubtitleSettings] = {s: preset(s) for s in SubtitleStyle}


# --------------------------------------------------------------------------- #
# words -> groups
# --------------------------------------------------------------------------- #
_PUNC_RE = re.compile(r"[.!?…]+$")


def _display(word: str, settings: SubtitleSettings) -> str:
    w = (word or "").strip()
    return w.upper() if settings.uppercase else w


def _importance(words: list[Word]) -> list[float]:
    raw = [lexicon.word_weight(w.text) for w in words]
    peak = max(raw, default=0.0)
    if peak <= 0:
        return [0.0] * len(words)
    return [r / peak for r in raw]


def _chunk(text_words: list[Word], settings: SubtitleSettings) -> list[list[Word]]:
    """Split one utterance into caption groups.

    Boundaries: hard break on sentence-ending punctuation, then a max-words
    limit, then a reading-speed limit (characters per second) so a long sentence
    never disappears before it can be read.
    """
    if not text_words:
        return []
    max_w = max(1, int(settings.max_words_per_line))
    cps = max(8, int(settings.reading_speed_cps))
    groups: list[list[Word]] = []
    cur: list[Word] = []
    for w in text_words:
        cur.append(w)
        dur = max(0.05, w.end - w.start)
        chars = sum(len(x.text.strip()) + 1 for x in cur)
        needs = chars / max(dur, 0.05)
        ends_sentence = bool(_PUNC_RE.search(w.text.strip()))
        if ends_sentence and len(cur) >= 2:
            groups.append(cur)
            cur = []
        elif len(cur) >= max_w:
            groups.append(cur)
            cur = []
        elif needs > cps * 1.6 and len(cur) >= 2:
            groups.append(cur)
            cur = []
    if cur:
        if groups and len(cur) == 1 and len(groups[-1]) < max_w:
            groups[-1].extend(cur)
        else:
            groups.append(cur)
    merged: list[list[Word]] = []
    for g in groups:
        d = g[-1].end - g[0].start
        if merged and d < 0.35 and len(g) <= 2:
            merged[-1].extend(g)
        else:
            merged.append(g)
    return merged


def _lines(group: list[Word], settings: SubtitleSettings, max_chars: int) -> list[str]:
    """Wrap a group into 1..2 visual lines balanced by character count."""
    texts = [_display(w.text, settings) for w in group]
    one = " ".join(texts)
    if len(one) <= max_chars or len(group) < 4:
        return [one]
    mid = len(group) // 2
    best, best_diff = None, 1e9
    for i in range(max(1, mid - 2), min(len(group) - 1, mid + 3)):
        a, b = " ".join(texts[:i]), " ".join(texts[i:])
        if max(len(a), len(b)) > max_chars * 1.25:
            continue
        diff = abs(len(a) - len(b))
        if diff < best_diff:
            best, best_diff = (a, b), diff
    return list(best) if best else [one]




# --------------------------------------------------------------------------- #
# cue building
# --------------------------------------------------------------------------- #
def _map_group(group: list[Word], tmap: TimelineMap, t0: float, t1: float,
               min_gap: float = 0.12) -> list[list[Word]]:
    """Translate source-time words into clip-time words.

    Returns one list per visual block: a block is broken wherever a removed
    silence sits between two consecutive kept words, so a caption never spans a
    cut (that would make the karaoke drift against the audio).
    """
    blocks: list[list[Word]] = []
    current: list[Word] = []
    prev_src_end: float | None = None
    for w in group:
        s = min(max(w.start, t0), t1)
        e = min(max(w.end, t0), t1)
        if e - s < 0.01:
            continue
        cs, ce = tmap.src_to_clip(s), tmap.src_to_clip(e)
        if cs is None or ce is None:
            continue                      # the word lives inside a removed silence
        if prev_src_end is not None and (w.start - prev_src_end) > min_gap:
            if tmap.src_to_clip((prev_src_end + w.start) / 2.0) is None:
                blocks.append(current)
                current = []
        current.append(Word(round(cs, 3), round(ce, 3), w.text, w.prob))
        prev_src_end = e
    if current:
        blocks.append(current)
    return [b for b in blocks if b]


def cues(transcript: Transcript | None, t0: float, t1: float,
         settings: SubtitleSettings, edits: dict[int, str] | None = None,
         tmap: TimelineMap | None = None, *, limit_lines: int = 2) -> list[Cue]:
    """Build word-accurate cues for the clip ``[t0, t1]`` (source seconds)."""
    if transcript is None or settings.style is SubtitleStyle.NONE or t1 <= t0:
        return []
    tmap = tmap or TimelineMap.full(t0, t1)
    edits = edits or {}
    out: list[Cue] = []
    max_chars = 22 if settings.max_words_per_line <= 3 else 34

    for idx, seg in enumerate(transcript.segments):
        if seg.end <= t0 or seg.start >= t1:
            continue
        words = list(seg.words)
        if idx in edits:                        # manual text: re-time proportionally
            words = _retimed(seg, edits[idx], t0, t1)
        if not words:                           # no word timestamps: whole segment
            a = tmap.src_time_at(max(t0, seg.start))
            b = tmap.src_time_at(min(t1, seg.end))
            if b - a < 0.12:
                continue
            out.append(Cue(round(a, 3), round(b, 3),
                           [Word(a, b, seg.text.strip())], [seg.text.strip()], set(), seg.speaker))
            continue
        for group in _chunk(words, settings):
            for piece in _map_group(group, tmap, t0, t1):
                imp = _importance(piece)
                hi = {i for i, v in enumerate(imp)
                      if settings.highlight_importance and v >= 0.55}
                out.append(Cue(
                    round(piece[0].start, 3), round(piece[-1].end, 3), piece,
                    _lines(piece, settings, max_chars)[:limit_lines], hi, seg.speaker))

    out = [c for c in out if c.duration >= 0.12]
    out.sort(key=lambda c: c.start)
    # enforce no overlap and a readable minimum gap
    fixed: list[Cue] = []
    prev_end = -1.0
    for c in out:
        s, e = c.start, c.end
        if s < prev_end + 0.02:
            s = prev_end + 0.02
        if e < s + 0.12:
            e = s + 0.12
        if s >= tmap.duration - 0.02:
            break
        e = min(e, max(s + 0.12, tmap.duration))
        fixed.append(Cue(round(s, 3), round(e, 3), c.words, c.lines, c.highlight, c.speaker))
        prev_end = e
    return fixed


def _retimed(seg, new_text: str, t0: float, t1: float) -> list[Word]:
    """Distribute the segment's time over the user's corrected words."""
    words = [w.strip() for w in (new_text or "").split() if w.strip()]
    if not words:
        return []
    span = max(0.05, min(seg.end, t1) - max(seg.start, t0))
    start = max(seg.start, t0)
    lengths = [len(w) + 1 for w in words]
    total = sum(lengths)
    out, acc = [], start
    for w, ln in zip(words, lengths):
        d = span * ln / total
        out.append(Word(round(acc, 3), round(acc + d, 3), w))
        acc += d
    return out


# --------------------------------------------------------------------------- #
# ASS
# --------------------------------------------------------------------------- #
def _hex_to_ass(color: str, alpha: int = 0, *, wrapped: bool = True) -> str:
    """``#RRGGBB`` → ASS ``&HAABBGGRR``.

    ASS uses two colour spellings: inside a Style line the value is
    ``&HAABBGGRR`` (no trailing ampersand), while inside a dialogue override tag
    it must be ``\\c&HAABBGGRR&``.  ``wrapped`` selects the latter.  *alpha* is a
    libass transparency byte: 0 opaque, 255 invisible.
    """
    c = (color or "#FFFFFF").strip().lstrip("#")
    if len(c) == 3:
        c = "".join(ch * 2 for ch in c)
    try:
        r, g, b = int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)
    except (ValueError, IndexError):
        r = g = b = 255
    a = max(0, min(255, int(alpha)))
    val = f"&H{a:02X}{b:02X}{g:02X}{r:02X}"
    return f"{val}&" if wrapped else val
BOLD_STYLES = {SubtitleStyle.VIRAL, SubtitleStyle.DYNAMIC, SubtitleStyle.PODCAST}


def _ass_time(seconds: float) -> str:
    """ASS timestamps are ``H:MM:SS.CC`` — exactly two centisecond digits."""
    centis = int(round(max(0.0, float(seconds)) * 100))
    h, rem = divmod(centis, 360000)
    m, rem = divmod(rem, 6000)
    s, cs = divmod(rem, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _esc(text: str) -> str:
    return (text or "").replace("\\", "\\\\").replace("{", "(").replace("}", ")").replace("\n", " ")


def auto_font_size(settings: SubtitleSettings, height: int) -> int:
    if settings.size and settings.size > 0:
        return int(settings.size)
    factor = {
        SubtitleStyle.VIRAL: 0.062, SubtitleStyle.DYNAMIC: 0.054,
        SubtitleStyle.PODCAST: 0.050, SubtitleStyle.PROFESSIONAL: 0.040,
        SubtitleStyle.MINIMALIST: 0.036, SubtitleStyle.NONE: 0.040,
    }.get(settings.style, 0.048)
    return max(16, int(round(height * factor)))


def _style_block(settings: SubtitleSettings, width: int, height: int) -> tuple[str, int]:
    size = auto_font_size(settings, height)
    align = ASS_ALIGN.get(settings.position, 2)
    margin_v = {"bottom": int(height * 0.16), "top": int(height * 0.11), "center": 0}[
        settings.position if settings.position in ("bottom", "top", "center") else "bottom"]
    # BorderStyle 3 draws the box with BackColour, so an opaque box needs alpha 0.
    if settings.background and settings.border_style == 3:
        back = _hex_to_ass(settings.background,
                           255 - max(0, min(255, int(settings.bg_opacity))), wrapped=False)
    elif settings.background:
        back = _hex_to_ass("#000000", 128, wrapped=False)      # shadow colour tint
    else:
        back = "&H00000000"
    primary = _hex_to_ass(settings.color, wrapped=False)
    secondary = _hex_to_ass(settings.highlight_color, wrapped=False)
    outline_c = _hex_to_ass("#000000", wrapped=False)
    name = "CorteIA"
    fields = [
        name,                          # Name
        _font_name(settings.font),     # Fontname
        str(size),                     # Fontsize
        primary, secondary, outline_c, back,
        "-1" if settings.style in BOLD_STYLES else "0",   # Bold
        "0", "0", "0",                 # Italic, Underline, StrikeOut
        "100", "100",                  # ScaleX, ScaleY
        "0",                           # Spacing
        "0",                           # Angle
        str(max(1, min(3, int(settings.border_style)))),  # BorderStyle
        str(max(0, int(settings.outline))),   # Outline
        str(max(0, int(settings.shadow))),    # Shadow
        str(align),                    # Alignment
        "60", "60",                    # MarginL, MarginR
        str(margin_v),                 # MarginV
        "1",                           # Encoding
    ]
    assert len(fields) == 23, f"ASS style needs 23 fields, got {len(fields)}"
    return f"Style: {','.join(fields)}\n", size


def _font_name(font: str) -> str:
    """Font names must avoid commas for the ASS field layout."""
    return re.sub(r"[,;]", " ", (font or "Arial").strip()) or "Arial"


def _karaoke_line(cue: Cue, settings: SubtitleSettings, size: int) -> str:
    total_cs = max(1, int(round(cue.duration * 100)))
    spans = []
    for i, w in enumerate(cue.words):
        spans.append(max(1, int(round((w.end - w.start) * 100))))
    scale = total_cs / max(1, sum(spans))
    spans = [max(1, int(round(s * scale))) for s in spans]
    spans[-1] += total_cs - sum(spans)

    # \kf fills words progressively with SecondaryColour.  That is the "podcast"
    # effect; the viral style instead keeps each word statically coloured, so
    # only the important words pop and they stay legible for the whole cue.
    karaoke = settings.style in (SubtitleStyle.PODCAST, SubtitleStyle.DYNAMIC)
    parts: list[str] = []
    for i, w in enumerate(cue.words):
        text = _esc(_display(w.text, settings))
        emph = i in cue.highlight
        pre = ""
        if settings.style is SubtitleStyle.DYNAMIC and emph:
            pre += r"\fad(90,60)\t(0,%d,\fscx108\fscy108)" % max(60, int(spans[i] * 10))
        if settings.style is SubtitleStyle.VIRAL and emph:
            col = VIRAL_PALETTE[(i // 3) % len(VIRAL_PALETTE)] \
                if settings.highlight_mode == "multicolor" else settings.highlight_color
            pre += "\\c" + _hex_to_ass(col)
        if settings.style is SubtitleStyle.PODCAST and emph:
            pre += r"\bord%d" % max(1, settings.outline + 1)
        if karaoke:
            parts.append(f"{{{pre}\\kf{spans[i]}}}{text}")
        elif emph:
            # Se o estilo já pintou esta palavra (paleta do viral), a cor dela
            # manda: aplicar highlight_color por cima era o que produzia o
            # override duplicado {\c&H..&\c&H..&}.
            col = settings.highlight_color2 if settings.highlight_mode == "green" else settings.highlight_color
            tag = "" if "\\c" in pre else f"\\c{_hex_to_ass(col)}"
            parts.append(f"{{{pre}{tag}}}{text}{{\\r}}")
        else:
            parts.append(f"{{{pre}}}{text}" if pre else text)
    return "\\N".join(_join_group(parts, cue, settings))


def _join_group(parts: list[str], cue: Cue, settings: SubtitleSettings) -> list[str]:
    """Regroup word tokens onto the wrapped lines already computed.

    ``cue.lines`` was produced from the same word list, so the number of
    whitespace-separated tokens per line is the exact partition we need.
    """
    counts = [max(1, len(line.split())) for line in cue.lines] if cue.lines else []
    if sum(counts) != len(parts):
        counts = _balance(len(parts), len(cue.lines) or 1)
    out: list[str] = []
    k = 0
    for cnt in counts:
        out.append(" ".join(parts[k:k + cnt]))
        k += cnt
    if k < len(parts):
        out[-1] = (out[-1] + " " + " ".join(parts[k:])).strip()
    return [o for o in out if o] or [" ".join(parts)]


def _balance(total: int, groups: int) -> list[int]:
    groups = max(1, groups)
    base, extra = divmod(total, groups)
    return [base + (1 if i < extra else 0) for i in range(groups)]


def ass_document(cue_list: list[Cue], settings: SubtitleSettings, width: int, height: int,
                 *, title: str = "CorteIA", speaker_names: dict[int, str] | None = None) -> str:
    """Full ``.ass`` file text for an already built cue list."""
    style_block, size = _style_block(settings, width, height)
    align = ASS_ALIGN.get(settings.position, 2)
    header = [
        "[Script Info]",
        f"; gerado por CorteIA Studio {title}",
        "ScriptType: v4.00+",
        f"PlayResX: {width}",
        f"PlayResY: {height}",
        "WrapStyle: 2",
        "ScaledBorderAndShadow: yes",
        "YCbCr Matrix: TV.709",
        "Copyright: CorteIA Studio",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, "
        "ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, "
        "MarginL, MarginR, MarginV, Encoding",
        style_block.rstrip("\n"),
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    dialogs: list[str] = []
    last_speaker = None
    for cue in cue_list:
        body = _karaoke_line(cue, settings, size)
        if not body.strip():
            continue
        tag = ""
        if cue.speaker is not None and settings.style in (
                SubtitleStyle.PODCAST, SubtitleStyle.PROFESSIONAL, SubtitleStyle.MINIMALIST) \
                and cue.speaker != last_speaker:
            name = (speaker_names or {}).get(cue.speaker, f"FALANTE {cue.speaker + 1}")
            tag = f"{{\\an7\\pos({max(20, int(width * 0.06))},{int(height * 0.12)})" \
                  f"\\fs{max(14, int(size * 0.45))}\\c{_hex_to_ass(settings.highlight_color)}}}" \
                  f"{_esc(name)}\\N"
        last_speaker = cue.speaker if cue.speaker is not None else last_speaker
        dialogs.append(
            f"Dialogue: 0,{_ass_time(cue.start)},{_ass_time(cue.end)},CorteIA,,0,0,0,,"
            f"{tag}{body}")
    return "\n".join(header) + "\n" + "\n".join(dialogs) + "\n"


def build_ass(transcript: Transcript | None, t0: float, t1: float, settings: SubtitleSettings,
              dest: Path, *, clip_w: int, clip_h: int,
              edits: dict[int, str] | None = None,
              tmap: TimelineMap | None = None,
              speaker_names: dict[int, str] | None = None) -> Path:
    """Write the subtitle file for a clip. Returns the path (empty file if no cues)."""
    dest = Path(dest)
    cue_list = cues(transcript, t0, t1, settings, edits, tmap)
    if not cue_list:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(ass_document([], settings, clip_w, clip_h), encoding="utf-8")
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(ass_document(cue_list, settings, clip_w, clip_h,
                                 speaker_names=speaker_names), encoding="utf-8")
    log.info("ASS escrito: %s (%d blocos, %.2f s)", dest.name, len(cue_list),
                             tmap.duration if tmap else (t1 - t0))
    return dest


def to_srt(cue_list: list[Cue], *, max_lines: int = 2) -> str:
    def stamp(x: float) -> str:
        ms = int(round(x * 1000))
        h, rem = divmod(ms, 3600000)
        m, rem = divmod(rem, 60000)
        s, ms = divmod(rem, 1000)
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"
    out = []
    for i, c in enumerate(cue_list, 1):
        text = "\n".join(c.lines[:max_lines])
        out.append(f"{i}\n{stamp(c.start)} --> {stamp(c.end)}\n{text}\n")
    return "\n".join(out)


def to_vtt(cue_list: list[Cue], *, max_lines: int = 2) -> str:
    def stamp(x: float) -> str:
        ms = int(round(x * 1000))
        h, rem = divmod(ms, 3600000)
        m, rem = divmod(rem, 60000)
        s, ms = divmod(rem, 1000)
        return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"
    body = "\n".join(f"{stamp(c.start)} --> {stamp(c.end)}\n"
                     f"{'<br>'.join(c.lines[:max_lines])}\n" for c in cue_list)
    return f"WEBVTT\n\n{body}"


def plain_transcript(cue_list: list[Cue]) -> str:
    """Text export for the 'copiar legenda' button."""
    return "\n".join(c.text() for c in cue_list)


def sync_report(cue_list: list[Cue], tmap: TimelineMap) -> dict[str, float]:
    """Diagnostics shown in the UI so the user can trust (or question) the sync."""
    if not cue_list:
        return {"blocks": 0, "covered": 0.0, "coverage": 0.0, "gap_max": 0.0,
                "overlap": 0, "avg_cps": 0.0}
    covered = sum(c.duration for c in cue_list)
    gaps = [b.start - a.end for a, b in zip(cue_list, cue_list[1:])]
    overlaps = sum(1 for g in gaps if g < -0.001)
    chars = sum(len(c.text()) for c in cue_list)
    dur = max(0.001, tmap.duration)
    return {
        "blocks": len(cue_list),
        "covered": round(covered, 2),
        "coverage": round(covered / dur, 3),
        "gap_max": round(max(gaps, default=0.0), 2),
        "overlap": overlaps,
        "avg_cps": round(chars / dur, 1),
    }
