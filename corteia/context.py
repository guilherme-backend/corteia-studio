"""Application context: the single object passed to every page.

Holds the settings store, the project store, the processing pipeline and the
signal bus. UI code never talks to the core directly: it calls the context,
which keeps jobs off the GUI thread.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, Signal

from .config import Config, get_config
from .models import Clip, ExportSettings, JobState, Project
from .project import ProjectStore

_BAD_FILENAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def sanitize_name(name: str, max_len: int = 80) -> str:
    """Windows-safe filename fragment."""
    out = _BAD_FILENAME.sub(" ", (name or "").strip())
    out = re.sub(r"\s+", " ", out).strip(" .-")
    out = out[:max_len].rstrip(" .")
    return out or "corte"


class Signals(QObject):
    """Central event bus. Everything cross-page flows through here."""

    # project / navigation
    project_changed = Signal()
    project_loaded = Signal(object)                 # Project
    recents_changed = Signal()
    navigate = Signal(str)                          # page id
    toast = Signal(str, str)                        # message, level: info|ok|warn|error

    # source video
    sources_changed = Signal()
    active_source_changed = Signal()

    # pipeline progress
    stage_started = Signal(str, str)                # stage id, label
    stage_progress = Signal(str, str, float, bool)  # stage, label, percent, indeterminate
    stage_finished = Signal(str, bool, str)         # stage, ok, message
    busy_changed = Signal(bool, str)

    # clips
    clips_changed = Signal()
    clip_updated = Signal(str)                      # clip id
    clip_selected = Signal(str)

    # export queue
    export_progress = Signal(str, float, str)       # clip id, percent, message
    export_state = Signal(str, str)                 # clip id, JobState value
    exports_finished = Signal(int, int)             # ok, failed
    history_changed = Signal()


class AppContext:
    def __init__(self, config: Config | None = None) -> None:
        self.config = config or get_config()
        self.signals = Signals()
        self.store = ProjectStore(self.config)
        self._pipeline: Any = None
        self._exporter: Any = None

    # lazy import keeps Qt start-up light and avoids circular imports -------
    @property
    def pipeline(self) -> Any:
        if self._pipeline is None:
            from .core.pipeline import Pipeline
            self._pipeline = Pipeline(self)
        return self._pipeline

    @property
    def exporter(self) -> Any:
        if self._exporter is None:
            from .core.exporter import Exporter
            self._exporter = Exporter(self)
        return self._exporter

    # ---------------------------------------------------------------- project
    @property
    def project(self) -> Project:
        return self.store.current

    def new_project(self, name: str = "") -> Project:
        p = self.store.new(name)
        self.signals.project_changed.emit()
        self.signals.project_loaded.emit(p)
        self.save()
        return p

    def save(self) -> None:
        self.store.save_current()
        self.signals.recents_changed.emit()

    def load_project(self, path: Path) -> Project | None:
        p = self.store.load(path)
        if p is not None:
            self.signals.project_changed.emit()
            self.signals.project_loaded.emit(p)
        return p

    # ---------------------------------------------------------------- helpers
    def toast(self, message: str, level: str = "info") -> None:
        self.signals.toast.emit(message, level)

    def go(self, page: str) -> None:
        self.signals.navigate.emit(page)

    def clip(self, clip_id: str) -> Clip | None:
        return self.project.find_clip(clip_id)

    def default_export_settings(self) -> ExportSettings:
        return self.project.export_settings

    def _pattern_name(self, clip: Clip, settings: ExportSettings) -> str:
        # posição entre os aprovados; se o corte não foi aprovado (exportar
        # direto da lista é caminho comum), usa a posição na mesma fonte --
        # antes todos os não aprovados viravam "00-", e dois cortes de mesmo
        # título escreviam no MESMO arquivo
        idx = 0
        for i, c in enumerate(self.project.approved_clips, start=1):
            if c.id == clip.id:
                idx = i
                break
        if not idx:
            irmaos = [c for c in self.project.clips
                      if c.source_path == clip.source_path]
            for i, c in enumerate(irmaos, start=1):
                if c.id == clip.id:
                    idx = i
                    break
        return settings.filename_pattern.format(
            index=idx,
            title=sanitize_name(clip.title or "corte"),
            start=f"{int(clip.start)}s",
            end=f"{int(clip.end)}s",
            dur=f"{int(clip.duration)}s",
            project=sanitize_name(self.project.name),
            score=int(clip.score.total),
        )

    def export_target(self, clip: Clip) -> Path:
        settings = self.project.export_settings
        out_dir = Path(settings.out_dir or self.config.str_("export.dir"))
        out_dir.mkdir(parents=True, exist_ok=True)
        name = sanitize_name(self._pattern_name(clip, settings), 120) + ".mp4"
        target = out_dir / name
        # colisão real: outro corte do projeto já reivindica este destino
        dono = next((c for c in self.project.clips
                     if c.id != clip.id and getattr(c, "exported_path", "") == str(target)),
                    None)
        if dono is not None or (target.exists() and not clip.exported_path):
            target = out_dir / (target.stem + f"-{clip.id[:6]}.mp4")
        return target

    def mark_export(self, clip_id: str, state: JobState, out_path: str = "") -> None:
        clip = self.clip(clip_id)
        if clip is not None and state is JobState.DONE:
            clip.exported_path = out_path
        self.save()

    def is_busy(self) -> bool:
        return bool(self._pipeline and self._pipeline.running)
    # ------------------------------------------------------------ sources ----
    def active_source(self):
        """The :class:`SourceVideo` the UI is currently looking at."""
        proj = self.project
        if not proj.sources:
            return None
        i = max(0, min(int(proj.active_source_index or 0), len(proj.sources) - 1))
        return proj.sources[i]

    def current_source_path(self) -> str:
        src = self.active_source()
        if src is not None and src.path:
            return src.path
        proj = self.project
        if proj.clips:
            return proj.clips[0].source_path
        return ""

    def source_for(self, path) -> "object | None":
        """Stored analysis of a file, so re-opening it never re-transcribes."""
        key = str(Path(path).resolve()) if Path(path).is_absolute() else str(path)
        for src in self.project.sources:
            if str(Path(src.path)) == str(path) or str(Path(src.path).resolve()) == key:
                return src
        return None

    def add_source(self, path, info=None, *, origin: str = "file"):
        """Register an imported file (idempotent) and make it the active one."""
        from .models import SourceVideo
        existing = self.source_for(path)
        if existing is not None:
            if info is not None:
                existing.info = info
            existing.origin = origin
            self.set_active_source(existing)
            return existing
        src = SourceVideo(path=str(path), info=info, origin=origin)
        self.project.sources.append(src)
        self.project.active_source_index = len(self.project.sources) - 1
        self.signals.sources_changed.emit()
        self.save()
        return src

    def set_active_source(self, source) -> None:
        try:
            self.project.active_source_index = self.project.sources.index(source)
        except ValueError:
            return
        self.signals.sources_changed.emit()

    def drop_source(self, path) -> None:
        """Forget a file's analysis (used when the user re-imports after editing)."""
        proj = self.project
        keep = [s for s in proj.sources if str(s.path) != str(path)]
        if len(keep) != len(proj.sources):
            proj.sources = keep
            proj.active_source_index = 0
            self.signals.sources_changed.emit()
            self.save()

    def transcript_for(self, path) -> "object | None":
        """The stored transcript of a file, so export never re-runs Whisper."""
        src = self.source_for(path)
        return src.transcript if src is not None else None

    # ------------------------------------------------------------- clips -----
    def replace_clips_for(self, path, clips) -> int:
        """Swap in the new suggestions for one source, keeping user approvals.

        Approved and exported clips survive a re-analysis: the user's judgement
        outranks the machine's, and losing labelled work would be the worst
        possible outcome of pressing "analyze again".
        """
        proj = self.project
        src = str(path)
        protected = [c for c in proj.clips
                     if c.source_path == src and (c.status.value in ("approved", "edited")
                                                  or c.exported_path)]
        fresh = [c for c in clips if not any(
            _overlaps(c, p) for p in protected)]
        proj.clips = [c for c in proj.clips if c.source_path != src] + protected + fresh
        proj.clips.sort(key=lambda c: (c.start, -c.score.total))
        self.signals.clips_changed.emit()
        self.save()
        return len(proj.clips)


def _overlaps(a, b) -> bool:
    inter = max(0.0, min(a.end, b.end) - max(a.start, b.start))
    union = max(1e-6, (a.end - a.start) + (b.end - b.start) - inter)
    return inter / union > 0.30
