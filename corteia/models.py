"""Data models shared by the processing core and the UI.

Every structure is serialisable to plain JSON (``to_dict`` / ``from_dict``) so a
project can be checkpointed after each pipeline stage and resumed later.
Timestamps are always in **seconds, float, relative to the source video** unless
a name explicitly says ``clip_`` (relative to the produced clip).
"""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


# --------------------------------------------------------------------------- #
# enums
# --------------------------------------------------------------------------- #
class Stage(str, Enum):
    """Pipeline stages, in execution order."""

    IMPORT = "import"
    PROBE = "probe"
    TRANSCRIBE = "transcribe"
    SCENES = "scenes"
    SILENCE = "silence"
    ANALYZE = "analyze"
    FACE = "face"
    RENDER = "render"
    EXPORT = "export"
    DONE = "done"


class AspectRatio(str, Enum):
    VERTICAL_9_16 = "9:16"
    HORIZONTAL_16_9 = "16:9"
    SQUARE_1_1 = "1:1"
    PORTRAIT_4_5 = "4:5"

    @property
    def value_w_h(self) -> float:
        w, h = self.value.split(":")
        return float(w) / float(h)

    @property
    def label(self) -> str:
        return {
            "9:16": "9:16  ·  TikTok / Reels / Shorts",
            "16:9": "16:9  ·  YouTube",
            "1:1": "1:1  ·  Instagram feed",
            "4:5": "4:5  ·  Instagram portrait",
        }[self.value]


class SubtitleStyle(str, Enum):
    VIRAL = "viral"
    PROFESSIONAL = "professional"
    PODCAST = "podcast"
    DYNAMIC = "dynamic"
    MINIMALIST = "minimalist"
    NONE = "none"

    @property
    def label(self) -> str:
        return {
            "viral": "Viral — palavras em cores diferentes",
            "professional": "Profissional — branco com contorno preto",
            "podcast": "Podcast — karaoke palavra a palavra",
            "dynamic": "Dinâmico — destaque automático",
            "minimalist": "Minimalista — discreto e elegante",
            "none": "Sem legendas",
        }[self.value]


class Resolution(str, Enum):
    R_720 = "720p"
    R_1080 = "1080p"
    R_2160 = "4K"

    @property
    def short_side(self) -> int:
        return {"720p": 720, "1080p": 1080, "4K": 2160}[self.value]


class Codec(str, Enum):
    H264 = "libx264"
    H265 = "libx265"


class ClipStatus(str, Enum):
    SUGGESTED = "suggested"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPORTED = "exported"


class JobState(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


# --------------------------------------------------------------------------- #
# media
# --------------------------------------------------------------------------- #
def _export_row(e: Any) -> dict[str, Any]:
    """Uma linha do histórico de exportação como ``dict``.

    O exportador escreve dicts ricos (título, MB, segundos, codec, resolução);
    ``ExportJob`` é o objeto de fila.  Os dois convivem na mesma lista, então a
    serialização precisa aceitar ambos -- antes disso, exportar um corte tornava
    o ``save()`` seguinte impossível (``'dict' object has no attribute
    'to_dict'``) e reabrir o projeto perdia as colunas do histórico.
    """
    if isinstance(e, dict):
        return dict(e)
    if hasattr(e, "to_dict"):
        return e.to_dict()
    return {"out": str(e)}


def _export_state(e: Any) -> str:
    if isinstance(e, dict):
        return str(e.get("state") or "")
    return str(getattr(getattr(e, "state", None), "value", getattr(e, "state", "")) or "")


def _ev(value: Any) -> Any:
    """``value.value`` se for enum, senão o próprio valor.

    Um enum que virou ``str`` no meio do caminho (clone de dataclass, JSON
    relido por outra versão, atribuição vinda de um widget) não pode impedir o
    projeto de ser salvo: era exatamente isso que estourava ``AttributeError``
    dentro de ``to_dict`` e derrubava ``ctx.save()``.
    """
    return value.value if isinstance(value, Enum) else value


def _enum(cls, value: Any, default: Any) -> Any:
    """``cls(value)`` that never raises on data coming from a file.

    A project saved by a newer build (or hand-edited, or half-written by a
    crash) can carry an enum value this version does not know.  Raising here
    used to make ``Project.from_dict`` fail and the loader rename the file to
    ``*.corrupt`` -- losing every clip the user approved.  Falling back to the
    default keeps the project open and the anomaly visible in the log instead.
    """
    if isinstance(value, cls):
        return value
    try:
        return cls(value)
    except (ValueError, TypeError, KeyError):
        import logging

        logging.getLogger("corteia.models").warning(
            "valor %r desconhecido para %s; usando %r", value, cls.__name__,
            getattr(default, "value", default))
        return default


@dataclass(slots=True)
class VideoInfo:
    """Result of an ``ffprobe`` pass. Nothing is guessed: missing fields are
    ``None``/0 and callers must handle that."""

    path: str
    duration: float = 0.0
    width: int = 0
    height: int = 0
    fps: float = 0.0
    codec_video: str = ""
    codec_audio: str = ""
    bitrate: int = 0
    has_audio: bool = True
    rotation: int = 0

    @property
    def aspect(self) -> float:
        return self.width / self.height if self.height else 0.0

    @property
    def duration_display(self) -> str:
        return format_ts(self.duration)

    @property
    def resolution_display(self) -> str:
        return f"{self.width}x{self.height}"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "VideoInfo":
        return VideoInfo(**{k: v for k, v in d.items() if k in VideoInfo.__dataclass_fields__})


def format_ts(seconds: float) -> str:
    """``72.5`` -> ``01:12`` / ``3725.4`` -> ``1:02:05``."""
    if seconds is None:
        return "00:00"
    seconds = max(0.0, float(seconds))
    total = int(round(seconds))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


# --------------------------------------------------------------------------- #
# transcript
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class Word:
    start: float
    end: float
    text: str
    prob: float = 1.0

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)

    def to_dict(self) -> dict[str, Any]:
        return {"s": round(self.start, 3), "e": round(self.end, 3), "t": self.text, "p": round(self.prob, 3)}

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "Word":
        return Word(float(d["s"]), float(d["e"]), str(d["t"]), float(d.get("p", 1.0)))


@dataclass(slots=True)
class Segment:
    """One utterance. ``speaker`` is ``None`` until diarisation runs."""

    start: float
    end: float
    text: str
    words: list[Word] = field(default_factory=list)
    speaker: int | None = None

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)

    def to_dict(self) -> dict[str, Any]:
        return {
            "s": round(self.start, 3),
            "e": round(self.end, 3),
            "t": self.text,
            "w": [w.to_dict() for w in self.words],
            "sp": self.speaker,
        }

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "Segment":
        return Segment(
            float(d["s"]), float(d["e"]), str(d["t"]),
            [Word.from_dict(w) for w in d.get("w", [])],
            d.get("sp"),
        )


@dataclass(slots=True)
class Transcript:
    language: str = "pt"
    model: str = ""
    segments: list[Segment] = field(default_factory=list)

    @property
    def duration(self) -> float:
        return self.segments[-1].end if self.segments else 0.0

    @property
    def full_text(self) -> str:
        return " ".join(s.text.strip() for s in self.segments if s.text.strip())

    def words(self) -> list[Word]:
        out: list[Word] = []
        for s in self.segments:
            out.extend(s.words)
        return out

    def text_between(self, t0: float, t1: float) -> str:
        return " ".join(s.text.strip() for s in self.segments if s.end > t0 and s.start < t1)

    def to_dict(self) -> dict[str, Any]:
        return {"language": self.language, "model": self.model, "segments": [s.to_dict() for s in self.segments]}

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "Transcript":
        return Transcript(
            str(d.get("language", "pt")), str(d.get("model", "")),
            [Segment.from_dict(s) for s in d.get("segments", [])],
        )


# --------------------------------------------------------------------------- #
# analysis helpers
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class Scene:
    start: float
    end: float

    def to_dict(self) -> dict[str, Any]:
        return {"s": round(self.start, 3), "e": round(self.end, 3)}

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "Scene":
        return Scene(float(d["s"]), float(d["e"]))


@dataclass(slots=True)
class Silence:
    start: float
    end: float

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)

    def overlaps(self, t0: float, t1: float) -> bool:
        return self.end > t0 and self.start < t1

    def to_dict(self) -> dict[str, Any]:
        return {"s": round(self.start, 3), "e": round(self.end, 3)}

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "Silence":
        return Silence(float(d["s"]), float(d["e"]))


@dataclass(slots=True)
class FaceTrackPoint:
    """One sampled crop box. ``cx``/``cy`` are the *center* of the talking face
    in normalised source coordinates (0..1); ``w`` is the face box width, also
    normalised — it drives zoom. ``score`` is detector confidence, ``speaking``
    the visual-speech activity of the track."""

    t: float
    cx: float
    cy: float
    w: float
    score: float = 0.0
    speaking: float = 0.0
    track: int = -1

    def to_dict(self) -> dict[str, Any]:
        return {
            "t": round(self.t, 3), "cx": round(self.cx, 4), "cy": round(self.cy, 4),
            "w": round(self.w, 4), "sc": round(self.score, 3), "sp": round(self.speaking, 3),
            "tr": self.track,
        }

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "FaceTrackPoint":
        return FaceTrackPoint(
            float(d["t"]), float(d["cx"]), float(d["cy"]), float(d["w"]),
            float(d.get("sc", 0.0)), float(d.get("sp", 0.0)), int(d.get("tr", -1)),
        )


@dataclass(slots=True)
class CropPlan:
    """Key-framed crop window (source pixels) for a time range."""

    points: list[tuple[float, int, int, int, int]] = field(default_factory=list)  # (t, x, y, w, h)
    mode: str = "face"          # face | saliency | static | center
    faces_used: int = 0
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "points": [[round(t, 3), x, y, w, h] for t, x, y, w, h in self.points],
            "mode": self.mode, "faces_used": self.faces_used, "note": self.note,
        }

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "CropPlan":
        return CropPlan(
            [(float(p[0]), int(p[1]), int(p[2]), int(p[3]), int(p[4])) for p in d.get("points", [])],
            str(d.get("mode", "face")), int(d.get("faces_used", 0)), str(d.get("note", "")),
        )


# --------------------------------------------------------------------------- #
# clip
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class ScoreBreakdown:
    """Every number shown in the UI must be traceable to a named signal.
    ``weights`` documents the model so scores are never opaque magic."""

    total: float = 0.0
    signals: dict[str, float] = field(default_factory=dict)
    weights: dict[str, float] = field(default_factory=dict)
    llm_used: bool = False
    llm_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "total": round(self.total, 2),
            "signals": {k: round(v, 3) for k, v in self.signals.items()},
            "weights": {k: round(v, 3) for k, v in self.weights.items()},
            "llm_used": self.llm_used, "llm_reason": self.llm_reason,
        }

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "ScoreBreakdown":
        return ScoreBreakdown(
            float(d.get("total", 0.0)),
            {k: float(v) for k, v in d.get("signals", {}).items()},
            {k: float(v) for k, v in d.get("weights", {}).items()},
            bool(d.get("llm_used", False)), str(d.get("llm_reason", "")),
        )


@dataclass(slots=True)
class SubtitleSettings:
    style: SubtitleStyle = SubtitleStyle.VIRAL
    font: str = "Arial Black"
    size: int = 0            # 0 = auto from clip height
    color: str = "#FFFFFF"
    highlight_color: str = "#FFE400"
    highlight_color2: str = "#22E07A"
    outline: int = 3
    shadow: int = 1
    border_style: int = 1     # ASS BorderStyle: 1 outline+shadow, 3 opaque box
    position: str = "bottom"  # bottom | center | top
    background: str = ""      # "" = none, else ASS &HAABBGGRR-ish hex "#RRGGBB"
    bg_opacity: int = 180
    max_words_per_line: int = 4
    highlight_importance: bool = True
    highlight_mode: str = "yellow"   # yellow | green | multicolor
    uppercase: bool = True
    reading_speed_cps: int = 26

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["style"] = _ev(self.style)
        return d

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "SubtitleSettings":
        known = {k: v for k, v in d.items() if k in SubtitleSettings.__dataclass_fields__}
        if "style" in known:
            known["style"] = _enum(SubtitleStyle, known["style"], SubtitleStyle.VIRAL)
        return SubtitleSettings(**known)


@dataclass(slots=True)
class ClipEdit:
    """Per-clip overrides created by the editor. ``None`` keeps the automatic value."""

    trim_start: float | None = None      # absolute source time
    trim_end: float | None = None
    crop_offset_x: int = 0
    crop_offset_y: int = 0
    zoom: float = 1.0
    position_override: str | None = None   # manual crop, "x,y,w,h"
    volume: float = 1.0
    music_path: str | None = None
    music_volume: float = 0.18
    duck_music: bool = True
    watermark_path: str | None = None
    watermark_text: str = ""
    watermark_pos: str = "br"
    watermark_opacity: float = 0.5
    watermark_scale: float = 0.18
    title_text: str = ""
    title_style: str = "bar"          # bar | plain | none
    aspect: AspectRatio | None = None
    subtitles: SubtitleSettings | None = None
    remove_silences: bool = True
    manual_text_edits: dict[int, str] = field(default_factory=dict)  # segment index -> text

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["aspect"] = _ev(self.aspect)
        d["subtitles"] = self.subtitles.to_dict() if self.subtitles else None
        return d

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "ClipEdit":
        known = {k: v for k, v in d.items() if k in ClipEdit.__dataclass_fields__}
        if known.get("aspect"):
            known["aspect"] = _enum(AspectRatio, known["aspect"], AspectRatio.VERTICAL_9_16)
        else:
            known["aspect"] = None
        if known.get("subtitles"):
            known["subtitles"] = SubtitleSettings.from_dict(known["subtitles"])
        else:
            known["subtitles"] = None
        known["manual_text_edits"] = {int(k): v for k, v in (d.get("manual_text_edits") or {}).items()}
        return ClipEdit(**known)


@dataclass(slots=True)
class Clip:
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    source_path: str = ""
    start: float = 0.0
    end: float = 0.0
    title: str = ""
    description: str = ""
    transcript: str = ""
    keywords: list[str] = field(default_factory=list)
    score: ScoreBreakdown = field(default_factory=ScoreBreakdown)
    status: ClipStatus = ClipStatus.SUGGESTED
    reason: str = ""
    edit: ClipEdit = field(default_factory=ClipEdit)
    scene_count: int = 0
    speaker_count: int = 0
    #: True when nothing cleared ``AnalysisOptions.min_score`` and this is the
    #: best available window instead (the UI shows a warning about it).
    below_floor: bool = False
    exported_path: str = ""

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)

    @property
    def rating(self) -> int:
        """1..5 stars derived from the total score (0..100)."""
        return max(1, min(5, round(self.score.total / 20.0)))

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "source_path": self.source_path,
            "start": round(self.start, 3), "end": round(self.end, 3),
            "title": self.title, "description": self.description, "transcript": self.transcript,
            "keywords": self.keywords, "score": self.score.to_dict(), "status": _ev(self.status),
            "reason": self.reason, "edit": self.edit.to_dict(),
            "scene_count": self.scene_count, "speaker_count": self.speaker_count,
            "exported_path": self.exported_path,
        }

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "Clip":
        return Clip(
            id=str(d.get("id") or uuid.uuid4().hex[:12]),
            source_path=str(d.get("source_path", "")),
            start=float(d.get("start", 0.0)), end=float(d.get("end", 0.0)),
            title=str(d.get("title", "")), description=str(d.get("description", "")),
            transcript=str(d.get("transcript", "")), keywords=list(d.get("keywords", [])),
            score=ScoreBreakdown.from_dict(d.get("score", {})),
            status=_enum(ClipStatus, d.get("status"), ClipStatus.SUGGESTED),
            reason=str(d.get("reason", "")),
            edit=ClipEdit.from_dict(d.get("edit", {})),
            scene_count=int(d.get("scene_count", 0)),
            speaker_count=int(d.get("speaker_count", 0)),
            exported_path=str(d.get("exported_path", "")),
        )


# --------------------------------------------------------------------------- #
# export
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class ExportSettings:
    codec: Codec = Codec.H264
    resolution: Resolution = Resolution.R_1080
    fps: int = 30
    quality_crf: int = 20          # lower = better
    preset: str = "fast"           # x264/x265 speed preset
    audio_bitrate: str = "192k"
    aspect: AspectRatio = AspectRatio.VERTICAL_9_16
    out_dir: str = ""
    filename_pattern: str = "{index:02d}-{title}-corteia"
    max_parallel: int = 2
    burn_subtitles: bool = True
    faststart: bool = True

    @property
    def target_size(self) -> tuple[int, int]:
        """Output dimensions for ``aspect`` at ``resolution``.

        ``1080p`` names the *short* side, the way every platform does: a 9:16
        export is 1080×1920 (not 608×1080), 4:5 is 1080×1350, 1:1 is 1080×1080
        and 16:9 is 1920×1080.  Values are forced even so x264 never complains.
        """
        # os dois campos podem chegar como texto (projetos antigos, clones de
        # dataclass, widgets); convertemos antes de usar as propriedades
        res = _enum(Resolution, self.resolution, Resolution.R_1080)
        asp = _enum(AspectRatio, self.aspect, AspectRatio.VERTICAL_9_16)
        s = res.short_side
        ar = asp.value_w_h
        if ar >= 1.0:      # landscape or square: the short side is the height
            h, w = s, int(round(s * ar))
        else:              # portrait: the short side is the width
            w, h = s, int(round(s / ar))
        w, h = (w + 1) // 2 * 2, (h + 1) // 2 * 2
        cap = 3840 if res is Resolution.R_2160 else (
            1920 if res is Resolution.R_1080 else 1280)
        return min(w, cap), min(h, cap)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["codec"] = _ev(self.codec)
        d["resolution"] = _ev(self.resolution)
        d["aspect"] = _ev(self.aspect)
        return d

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "ExportSettings":
        known = {k: v for k, v in d.items() if k in ExportSettings.__dataclass_fields__}
        if "codec" in known:
            known["codec"] = _enum(Codec, known["codec"], Codec.H264)
        if "resolution" in known:
            known["resolution"] = _enum(Resolution, known["resolution"], Resolution.R_1080)
        if "aspect" in known:
            known["aspect"] = _enum(AspectRatio, known["aspect"], AspectRatio.VERTICAL_9_16)
        return ExportSettings(**known)


@dataclass(slots=True)
class ExportJob:
    clip_id: str
    source_path: str
    out_path: str
    state: JobState = JobState.PENDING
    progress: float = 0.0
    message: str = ""
    error: str = ""
    started_at: float = 0.0
    finished_at: float = 0.0
    settings: ExportSettings = field(default_factory=ExportSettings)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["state"] = _ev(self.state)
        d["settings"] = self.settings.to_dict()
        return d

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "ExportJob":
        return ExportJob(
            clip_id=str(d["clip_id"]), source_path=str(d["source_path"]), out_path=str(d["out_path"]),
            state=_enum(JobState, d.get("state"), JobState.PENDING), progress=float(d.get("progress", 0.0)),
            message=str(d.get("message", "")), error=str(d.get("error", "")),
            started_at=float(d.get("started_at", 0.0)), finished_at=float(d.get("finished_at", 0.0)),
            settings=ExportSettings.from_dict(d.get("settings", {})),
        )


# --------------------------------------------------------------------------- #
# project
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class AnalysisOptions:
    """User-facing knobs of the "gerar cortes com IA" action."""

    target_min: float = 15.0
    target_max: float = 60.0
    preset_duration: int = 0        # 15/30/45/60/90/120/0=custom
    max_clips: int = 12
    min_score: float = 25.0
    aspect: AspectRatio = AspectRatio.VERTICAL_9_16
    subtitle_style: SubtitleStyle = SubtitleStyle.VIRAL
    remove_silences: bool = True
    smart_boundaries: bool = True
    follow_speaker: bool = True
    use_llm: bool = True
    whisper_model: str = "base"
    language: str = "pt"

    @property
    def duration_label(self) -> str:
        if self.preset_duration:
            return f"{self.preset_duration}s"
        return f"{int(self.target_min)}–{int(self.target_max)}s"

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["aspect"] = _ev(self.aspect)
        d["subtitle_style"] = _ev(self.subtitle_style)
        return d

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "AnalysisOptions":
        known = {k: v for k, v in d.items() if k in AnalysisOptions.__dataclass_fields__}
        if "aspect" in known:
            known["aspect"] = _enum(AspectRatio, known["aspect"], AspectRatio.VERTICAL_9_16)
        if "subtitle_style" in known:
            known["subtitle_style"] = _enum(SubtitleStyle, known["subtitle_style"], SubtitleStyle.VIRAL)
        return AnalysisOptions(**known)


@dataclass(slots=True)
class SourceVideo:
    path: str
    info: VideoInfo | None = None
    transcript: Transcript | None = None
    scenes: list[Scene] = field(default_factory=list)
    silences: list[Silence] = field(default_factory=list)
    faces: list[FaceTrackPoint] = field(default_factory=list)
    thumb: str = ""
    added_at: float = 0.0
    stages_done: list[str] = field(default_factory=list)
    origin: str = "file"        # file | url

    def stage_done(self, stage: Stage) -> bool:
        return stage.value in self.stages_done

    def mark_done(self, stage: Stage) -> None:
        if stage.value not in self.stages_done:
            self.stages_done.append(stage.value)

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path, "thumb": self.thumb, "added_at": self.added_at, "origin": self.origin,
            "info": self.info.to_dict() if self.info else None,
            "transcript": self.transcript.to_dict() if self.transcript else None,
            "scenes": [s.to_dict() for s in self.scenes],
            "silences": [s.to_dict() for s in self.silences],
            "faces": [f.to_dict() for f in self.faces],
            "stages_done": self.stages_done,
        }

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "SourceVideo":
        return SourceVideo(
            path=str(d["path"]),
            info=VideoInfo.from_dict(d["info"]) if d.get("info") else None,
            transcript=Transcript.from_dict(d["transcript"]) if d.get("transcript") else None,
            scenes=[Scene.from_dict(s) for s in d.get("scenes", [])],
            silences=[Silence.from_dict(s) for s in d.get("silences", [])],
            faces=[FaceTrackPoint.from_dict(f) for f in d.get("faces", [])],
            thumb=str(d.get("thumb", "")),
            added_at=float(d.get("added_at", 0.0)),
            stages_done=list(d.get("stages_done", [])),
            origin=str(d.get("origin", "file")),
        )


@dataclass(slots=True)
class Project:
    """One working session: sources + clips + settings. Persisted as JSON."""

    name: str = "Sem título"
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    created_at: float = 0.0
    updated_at: float = 0.0
    sources: list[SourceVideo] = field(default_factory=list)
    clips: list[Clip] = field(default_factory=list)
    options: AnalysisOptions = field(default_factory=AnalysisOptions)
    export_settings: ExportSettings = field(default_factory=ExportSettings)
    #: histórico de exportação; linhas como dict ver `_export_row`
    exports: list[Any] = field(default_factory=list)
    active_source_index: int = 0
    notes: str = ""

    @property
    def active_source(self) -> SourceVideo | None:
        if 0 <= self.active_source_index < len(self.sources):
            return self.sources[self.active_source_index]
        return None

    @property
    def approved_clips(self) -> list[Clip]:
        return [c for c in self.clips if c.status in (ClipStatus.APPROVED, ClipStatus.EXPORTED)]

    def find_clip(self, clip_id: str) -> Clip | None:
        return next((c for c in self.clips if c.id == clip_id), None)

    def stats(self) -> dict[str, Any]:
        done = [e for e in self.exports
                if _export_state(e) in (JobState.DONE.value, "done")]
        return {
            "sources": len(self.sources),
            "clips": len(self.clips),
            "approved": len(self.approved_clips),
            "exported": len(done),
            "total_clip_seconds": round(sum(c.duration for c in self.clips), 1),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "name": self.name, "created_at": self.created_at,
            "updated_at": self.updated_at, "active_source_index": self.active_source_index,
            "notes": self.notes,
            "sources": [s.to_dict() for s in self.sources],
            "clips": [c.to_dict() for c in self.clips],
            "options": self.options.to_dict(),
            "export_settings": self.export_settings.to_dict(),
            "exports": [_export_row(e) for e in self.exports],
        }

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "Project":
        return Project(
            name=str(d.get("name", "Sem título")), id=str(d.get("id") or uuid.uuid4().hex[:12]),
            created_at=float(d.get("created_at", 0.0)), updated_at=float(d.get("updated_at", 0.0)),
            sources=[SourceVideo.from_dict(s) for s in d.get("sources", [])],
            clips=[Clip.from_dict(c) for c in d.get("clips", [])],
            options=AnalysisOptions.from_dict(d.get("options", {})),
            export_settings=ExportSettings.from_dict(d.get("export_settings", {})),
            exports=[_export_row(e) for e in d.get("exports", [])],
            active_source_index=int(d.get("active_source_index", 0)),
            notes=str(d.get("notes", "")),
        )
