"""Pipeline: one click from a video file to a list of scored clips.

Runs the real stages in order — probe, thumbnail, transcript, diarisation,
silence map, scene map, optional LLM judging, clip planning — and checkpoints
each of them into the project so a crash or a cancel half way through a 40
minute video does not throw away the work already done.

Everything here is synchronous: the UI runs it inside
``corteia.workers.jobs.JobRunner``.  Progress is reported as a fraction 0..1,
the same convention used by :mod:`corteia.core.ffmpeg_cli`.
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Callable

from .. import paths
from ..models import (AnalysisOptions, Clip, FaceTrackPoint, JobState, Stage,
                      SubtitleSettings, SubtitleStyle, VideoInfo)
from . import analyze as AN
from . import ffmpeg_cli as FC
from . import llm as LLM
from . import media, reframe, scenes as SC, silence as SIL, transcribe as TR
from .errors import CancelledError, CorteiaError, ProcessingError

log = logging.getLogger("corteia.pipeline")

#: sub-range of the global 0..1 progress bar per stage
RANGES: dict[str, tuple[float, float]] = {
    "probe": (0.00, 0.04),
    "thumb": (0.04, 0.06),
    "transcribe": (0.06, 0.58),
    "diarize": (0.58, 0.64),
    "silence": (0.64, 0.74),
    "scenes": (0.74, 0.82),
    "llm": (0.82, 0.90),
    "analyze": (0.90, 1.00),
}
STAGE_ORDER = ["probe", "thumb", "transcribe", "silence", "scenes", "analyze"]


def _frac(value: float) -> float:
    """Accept 0..1 or 0..100 from whichever core module reported it."""
    v = float(value or 0.0)
    return v / 100.0 if v > 1.5 else v


class Pipeline:
    def __init__(self, ctx) -> None:
        self.ctx = ctx
        self._cancel = threading.Event()
        self._running = False
        self._stage_states: dict[str, dict] = {}
        self._lock = threading.Lock()
        self._plan_cache: dict[tuple, list] = {}

    # ------------------------------------------------------------------ state
    @property
    def running(self) -> bool:
        return self._running

    def stages(self) -> list[dict]:
        """Per-stage status for the UI stepper (label, state, 0..1 progress)."""
        out = []
        for key in STAGE_ORDER:
            st = self._stage_states.get(key, {})
            out.append({"key": key,
                        "label": st.get("label", _label(key)),
                        "state": st.get("state", "idle"),
                        "progress": st.get("progress", 0.0),
                        "detail": st.get("detail", ""),
                        "elapsed": st.get("elapsed", 0.0)})
        return out

    def cancel(self) -> None:
        self._cancel.set()
        self._set("analyze", "cancelled", detail="cancelado pelo usuário")

    # ------------------------------------------------------------------ glue
    def _set(self, key: str, state: str, *, progress: float | None = None,
             detail: str = "") -> None:
        with self._lock:
            st = self._stage_states.setdefault(key, {"key": key, "label": _label(key)})
            st["state"] = state
            if progress is not None:
                st["progress"] = round(max(0.0, min(1.0, progress)), 4)
            if detail:
                st["detail"] = detail
            if state == "running":
                st["started"] = time.time()
            elif state in ("done", "error", "cancelled") and st.get("started"):
                st["elapsed"] = round(time.time() - st["started"], 2)
                st["progress"] = 1.0 if state == "done" else st.get("progress", 0.0)

    def _emit(self, on_progress, key: str, local: float, msg: str) -> None:
        lo, hi = RANGES.get(key, (0.0, 1.0))
        frac = lo + (hi - lo) * max(0.0, min(1.0, _frac(local)))
        # A finished stage must stay finished: several core modules fire a last
        # progress event *after* we already marked the stage done, which used to
        # leave the stepper spinning at 58 % forever.
        state = self._stage_states.get(key, {}).get("state", "idle")
        if state not in ("done", "error", "cancelled"):
            self._set(key, "running", progress=frac, detail=msg)
        else:
            self._set(key, state, progress=max(frac, state and 1.0 or frac), detail=msg)
        if on_progress:
            try:
                on_progress(frac, f"{_label(key)}: {msg}")
            except TypeError:
                on_progress(frac * 100.0, f"{_label(key)}: {msg}")

    def _checkpoint(self, source) -> None:
        """Persist after every stage so an interruption never loses work."""
        try:
            project = self.ctx.project
            if source not in project.sources:
                project.sources.append(source)
            self.ctx.store.save_current()
        except Exception as exc:                              # noqa: BLE001
            log.debug("checkpoint falhou: %s", exc)

    # ------------------------------------------------------------------ main
    def analyze(self, source_path=None, options: AnalysisOptions | None = None, *,
                on_progress: Callable[[float, str], None] | None = None,
                on_stage: Callable[[str, str], None] | None = None,
                cancel: threading.Event | None = None,
                force: bool = False) -> list[Clip]:
        """Run every stage and return freshly planned clips (also in project)."""
        if self._running:
            raise ProcessingError("pipeline", "já existe uma análise em andamento",
                                  "cancele ou aguarde a atual terminar")
        self._running = True
        self._cancel = cancel or threading.Event()
        self._stage_states = {}
        try:
            return self._analyze(source_path, options, on_progress, on_stage, force)
        except CancelledError:
            self._mark_all("cancelled")
            raise
        finally:
            self._running = False

    def _mark_all(self, state: str) -> None:
        for key in STAGE_ORDER:
            if self._stage_states.get(key, {}).get("state") not in ("done",):
                self._set(key, state)

    def _analyze(self, source_path, options, on_progress, on_stage, force) -> list[Clip]:
        ctx, cfg = self.ctx, self.ctx.config
        path = Path(source_path or ctx.current_source_path())
        if not path or not Path(path).exists():
            raise ProcessingError("probe", "escolha um arquivo de vídeo primeiro",
                                  str(path or ""))
        path = Path(path)
        opts = options or ctx.project.options or AnalysisOptions()

        def stage(key: str) -> None:
            if on_stage:
                on_stage(key, "running")

        # ---------------------------------------------------------------- probe
        stage("probe")
        self._emit(on_progress, "probe", 0.0, f"lendo {path.name}")
        info = media.probe_or_raise(path)
        self._emit(on_progress, "probe", 1.0,
                   f"{info.width}x{info.height}, {info.duration:.0f}s")
        self._set("probe", "done")
        source = ctx.source_for(path) or _new_source(path, info)
        source.info = info
        if not source.stages_done:
            source.stages_done = []

        # --------------------------------------------------------------- thumb
        stage("thumb")
        try:
            source.thumb = str(media.thumbnail(path, info.duration * 0.10, 480))
        except Exception as exc:                              # noqa: BLE001
            log.warning("miniatura falhou: %s", exc)
        self._set("thumb", "done")
        self._emit(on_progress, "thumb", 1.0, "miniatura pronta")

        # ---------------------------------------------------------- transcribe
        if not source.transcript or force:
            stage("transcribe")
            model = TR.resolve_model(opts.whisper_model or cfg.str_("subs.model", "base"),
                                     audio_seconds=info.duration)
            if not TR.is_downloaded(model):
                self._emit(on_progress, "transcribe", 0.0,
                           f"baixando modelo {model} (~{TR.MODELS.get(model, {}).get('mb', 0):.0f} MB)")
                TR.download_model(model, on_progress=lambda f, m: self._emit(
                    on_progress, "transcribe", f * 0.15, m), cancel=self._cancel)
            self._emit(on_progress, "transcribe", 0.0, f"transcrevendo com {model}")
            source.transcript = TR.transcribe(
                path, model=model, language=opts.language or cfg.str_("subs.language", "pt"),
                device=cfg.str_("proc.device", "auto"),
                compute_type=cfg.str_("proc.compute_type", "auto"),
                beam_size=int(cfg.int_("subs.beam_size", 5)),
                on_progress=lambda f, m: self._emit(on_progress, "transcribe", 0.15 + f * 0.85, m),
                cancel=self._cancel,
                cpu_threads=int(cfg.int_("proc.cpu_threads", 0)),
                word_timestamps=bool(cfg.bool_("subs.word_timestamps", True)))
            if (opts.follow_speaker or cfg.bool_("subs.diarize", False)) \
                    and source.transcript.segments:
                stage("diarize")
                try:
                    TR.diarize(source.transcript, path, cancel=self._cancel)
                except Exception as exc:                      # noqa: BLE001
                    log.info("diarização indisponível: %s", exc)
                self._set("diarize", "done")
            if "transcribe" not in source.stages_done:
                source.stages_done.append("transcribe")
            self._checkpoint(source)
            segs = len(source.transcript.segments)
            words = sum(len(s.words) for s in source.transcript.segments)
            self._set("transcribe", "done", detail=f"{segs} trechos, {words} palavras")
            self._emit(on_progress, "transcribe", 1.0, f"{segs} trechos transcritos")
        else:
            self._set("transcribe", "done", detail="reaproveitado do projeto")
            self._emit(on_progress, "transcribe", 1.0, "transcrição já existia")

        tr = source.transcript
        if tr is None or not tr.segments:
            raise ProcessingError(
                "transcribe",
                "não houve fala reconhecida neste vídeo",
                "se for gameplay ou vídeo sem diálogo, desligue a transcrição e use "
                "apenas cenas e silêncios")

        # ------------------------------------------------------------ silence
        if opts.remove_silences and (not source.silences or force):
            stage("silence")
            if info.has_audio:
                self._emit(on_progress, "silence", 0.0, "mapeando silêncios")
                source.silences = SIL.detect_silences(
                    path, db=float(cfg.float_("proc.silence_db", -35.0)),
                    min_ms=int(cfg.int_("proc.silence_min_ms", 350)),
                    cancel=self._cancel,
                    progress=lambda f, m: self._emit(on_progress, "silence", f, m))
                saved = sum(s.end - s.start for s in source.silences)
                self._set("silence", "done",
                          detail=f"{len(source.silences)} pausas, {saved:.0f}s")
            else:
                source.silences = []
                self._set("silence", "done", detail="sem trilha de áudio")
        else:
            self._set("silence", "done", detail="remoção de silêncio desligada")

        # ------------------------------------------------------------- scenes
        if not source.scenes or force:
            stage("scenes")
            try:
                source.scenes = SC.detect_scenes(
                    path, threshold=float(cfg.float_("proc.scene_threshold", 27.0)),
                    min_scene_len=int(cfg.int_("proc.scene_min_len", 15)),
                    cancel=self._cancel,
                    progress=lambda f, m: self._emit(on_progress, "scenes", f, m))
                self._set("scenes", "done", detail=f"{len(source.scenes)} planos")
            except CorteiaError as exc:
                log.warning("cenas indisponíveis: %s", exc)
                source.scenes = []
                self._set("scenes", "done", detail="detecção de cenas indisponível")
        else:
            self._set("scenes", "done", detail="reaproveitado do projeto")

        # ---------------------------------------------------------------- LLM
        llm_scores: dict[str, float] = {}
        client = LLM.get_client(cfg)
        topic_breaks: list[float] = []
        if opts.use_llm and client.usable():
            stage("llm")
            self._emit(on_progress, "llm", 0.0, "consultando o modelo de linguagem")
            try:
                windows = AN.plan_clips(tr, options=opts, source_path=str(path),
                                        duration=info.duration, silences=source.silences,
                                        scenes=source.scenes, curve=None,
                                        topic_breaks=None, progress=None)
                items = [(c.text_key(), c.text, c.duration) for c in windows[0]]
                llm_scores = client.judge(items, cancel=self._cancel,
                                          progress=lambda f, m: self._emit(on_progress, "llm", f, m))
                topic_breaks = client.topics(tr, info.duration, cancel=self._cancel)
                self._set("llm", "done",
                          detail=f"{len([k for k in llm_scores if ':' not in k])} notas do modelo")
            except Exception as exc:                          # noqa: BLE001
                log.warning("avaliação por LLM falhou: %s", exc)
                self._set("llm", "done", detail=f"indisponível: {str(exc)[:60]}")
        else:
            self._set("llm", "done", detail="modelo local (sem API)")
        self._emit(on_progress, "llm", 1.0, "")

        # ------------------------------------------------------------ analyze
        stage("analyze")
        self._emit(on_progress, "analyze", 0.10, "gerando candidatos")
        curve = None
        cands, clips = AN.plan_clips(
            tr, options=opts, source_path=str(path), duration=info.duration,
            silences=source.silences, scenes=source.scenes, curve=curve,
            llm_scores=llm_scores, topic_breaks=topic_breaks,
            progress=lambda f, m: self._emit(on_progress, "analyze", _frac(f), m))
        self._emit(on_progress, "analyze", 1.0, f"{len(clips)} cortes sugeridos")
        self._set("analyze", "done", detail=f"{len(clips)} cortes")

        for key in ("probe", "thumb", "silence", "scenes", "transcribe"):
            if key not in source.stages_done:
                source.stages_done.append(key)
        ctx.replace_clips_for(path, clips)
        self._checkpoint(source)
        return clips


# --------------------------------------------------------------------------- #
# crop planning (per clip, on demand)
# --------------------------------------------------------------------------- #
_CROP_CACHE: dict[tuple, object] = {}

def crop_plan_for(path, info: VideoInfo, t0: float, t1: float, aspect, options,
                  *, on_progress=None, cancel=None, force: bool = False):
    """Faces → crop plan for one window, cached.

    Face detection only runs for the window being edited/exported: on a 40
    minute video a full pass would cost minutes, while a 60 s clip costs
    seconds (measured: 10.2 s for 40 s at 1080p, 6 samples/s).
    """
    from ..config import get_config
    from ..models import AnalysisOptions as _AO
    from . import faces as FA
    cfg = get_config()

    opts = options or _AO()
    follow = bool(getattr(opts, "follow_speaker", True))
    key = (str(path), round(float(t0), 2), round(float(t1), 2), str(aspect),
           round(float(opts.zoom if hasattr(opts, "zoom") else 1.0), 3),
           round(float(getattr(opts, "offset_x", 0.0)), 3),
           round(float(getattr(opts, "offset_y", 0.0)), 3),
           str(getattr(opts, "manual_crop", "") or ""))
    if not force and key in _CROP_CACHE:
        return _CROP_CACHE[key]

    points: list[FaceTrackPoint] = []
    if follow:
        points = FA.analyze_clip_track(path, info, t0, t1,
                                       sample_fps=float(cfg.float_("proc.sample_fps_face", 6.0) or 6.0),
                                       on_progress=on_progress, cancel=cancel)
    plan = reframe.plan_crop(points, info, aspect, t0, t1,
                             follow_speaker=follow,
                             zoom=float(getattr(opts, "zoom", 1.0) or 1.0),
                             offset_x=float(getattr(opts, "offset_x", 0.0) or 0.0),
                             offset_y=float(getattr(opts, "offset_y", 0.0) or 0.0),
                             manual=str(getattr(opts, "manual_crop", "") or ""))
    _CROP_CACHE[key] = plan
    if len(_CROP_CACHE) > 64:
        for stale in list(_CROP_CACHE)[: len(_CROP_CACHE) - 64]:
            _CROP_CACHE.pop(stale, None)
    return plan





def crop_plan_for_clip(self, clip, aspect=None, *, on_progress=None, cancel=None,
                       force: bool = False):
    """Convenience for the editor and the exporter: uses the clip's own edit."""
    ctx = self.ctx
    source = ctx.source_for(Path(clip.source_path))
    info = (source.info if source and source.info else media.probe_or_raise(Path(clip.source_path)))
    opts = clip.edit
    return crop_plan_for(Path(clip.source_path), info, clip.start, clip.end,
                         aspect or ctx.project.export_settings.aspect, opts,
                         on_progress=on_progress, cancel=cancel, force=force)


Pipeline.crop_plan = crop_plan_for_clip


def timeline_for(self, clip, *, remove_silences: bool | None = None):
    """The kept ranges of a clip, using the source's silence map."""
    from .timeline import TimelineMap
    source = self.ctx.source_for(Path(clip.source_path))
    sil = list(source.silences) if source else []
    do = clip.edit.remove_silences if remove_silences is None else remove_silences
    if not do or not sil:
        return TimelineMap.full(clip.start, clip.end)
    cfg = self.ctx.config
    # pad/min_keep derivam do corte de silêncio que o usuário ajustou: um
    # respiro antes da fala evita palavra "mordida" na legenda.
    min_ms = int(cfg.int_("proc.silence_min_ms", 350)) / 1000.0
    return TimelineMap.from_silences(sil, clip.start, clip.end,
                                     min_keep=float(cfg.float_("silence.min_keep",
                                                               max(0.35, min_ms))),
                                     pad=float(cfg.float_("silence.pad", 0.06)))


Pipeline.timeline_for = timeline_for


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _label(key: str) -> str:
    try:
        from ..i18n import t
        return t(f"stage.{key}")
    except Exception:                                       # noqa: BLE001
        return {
            "probe": "Lendo o arquivo", "thumb": "Miniatura",
            "transcribe": "Transcrevendo fala", "diarize": "Separando falantes",
            "silence": "Caçando silêncios", "scenes": "Detectando planos",
            "llm": "Avaliando com IA", "analyze": "Montando os cortes",
        }.get(key, key)


def _new_source(path: Path, info: VideoInfo):
    from ..models import SourceVideo
    return SourceVideo(path=str(path), info=info, added_at=time.time(), origin="file")
