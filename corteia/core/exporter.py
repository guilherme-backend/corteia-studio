"""Export queue: turn approved clips into finished MP4 files.

The queue is deliberately simple - a list of :class:`ExportJob` with a state
machine - because the hard work lives in :mod:`corteia.core.render`.  What this
layer adds is what a real batch needs: per-clip progress on the event bus, a
history row for the dashboard, honest error text in Portuguese, cancellation
that stops between clips *and* inside FFmpeg, and a post-render verification
with ffprobe, so a green tick always means the file exists with the duration we
asked for.
"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from ..models import Clip, ExportJob, JobState, SubtitleStyle
from . import media, render as RD, subtitles as SUB
from .errors import CancelledError, CorteiaError, RenderError
from .timeline import TimelineMap

log = logging.getLogger("corteia.export")


class Exporter:
    def __init__(self, ctx) -> None:
        self.ctx = ctx
        self.queue: list[ExportJob] = []
        self._cancel = threading.Event()
        self._running = False

    @property
    def running(self) -> bool:
        return self._running

    def cancel(self) -> None:
        self._cancel.set()
        for job in self.queue:
            if job.state is JobState.PENDING:
                job.state = JobState.CANCELLED
                job.message = "cancelado antes de começar"
                self._emit_state(job)

    # ------------------------------------------------------------------ entry
    def export_all(self, clips=None, settings=None, *, on_progress=None) -> dict:
        """Export every approved clip (or the ones given).  Blocking by design."""
        ctx = self.ctx
        if self._running:
            raise CorteiaError("já existe uma exportação em andamento")
        self._running = True
        self._cancel = threading.Event()
        ok = failed = 0
        outputs: list[str] = []
        try:
            chosen = self._resolve(clips)
            if not chosen:
                ctx.toast("Nenhum corte aprovado para exportar.", "warn")
                return {"ok": 0, "failed": 0, "outputs": []}
            settings = settings or ctx.project.export_settings
            self.queue = [ExportJob(clip_id=c.id, source_path=c.source_path,
                                    out_path=str(ctx.export_target(c)),
                                    settings=settings) for c in chosen]
            workers = max(1, min(int(getattr(settings, "max_parallel", 1) or 1),
                                 4, len(self.queue)))
            jobs = list(self.queue)

            def _go(job):
                return self._run(job, settings, on_progress)

            with ThreadPoolExecutor(max_workers=workers,
                                    thread_name_prefix="export") as pool:
                for r in pool.map(_go, jobs):
                    ok += r[0]
                    failed += r[1]
                    outputs += r[2]
            ctx.signals.exports_finished.emit(ok, failed)
            if ok:
                extra = f", {failed} com falha" if failed else ""
                ctx.toast(f"{ok} corte(s) exportado(s){extra}",
                          "ok" if not failed else "warn")
            elif failed:
                ctx.toast(f"Nenhum corte exportou ({failed} falhas).", "error")
            return {"ok": ok, "failed": failed, "outputs": outputs}
        finally:
            self._running = False

    def export_one(self, clip_id: str, settings=None, *, on_progress=None) -> str:
        """The editor's "export this one".  Returns the file path or raises."""
        clip = self.ctx.clip(clip_id)
        if clip is None:
            raise CorteiaError("este corte não existe mais no projeto")
        settings = settings or self.ctx.project.export_settings
        job = ExportJob(clip_id=clip.id, source_path=clip.source_path,
                        out_path=str(self.ctx.export_target(clip)), settings=settings)
        self.queue = [job]
        self._running = True
        self._cancel = threading.Event()
        try:
            self._run(job, settings, on_progress)
        finally:
            self._running = False
        if job.state is not JobState.DONE:
            raise CorteiaError(job.error or job.message or "a exportação falhou")
        return job.out_path

    def _resolve(self, clips):
        if clips:
            items = [self.ctx.clip(c) if isinstance(c, str) else c for c in clips]
            return [c for c in items if c is not None]
        proj = self.ctx.project
        picked = [c for c in proj.clips if c.status.value in ("approved", "edited")]
        return picked or list(getattr(proj, "approved_clips", None) or [])

    def _emit_state(self, job: ExportJob) -> None:
        self.ctx.signals.export_state.emit(job.clip_id, job.state.value)

    def _emit_progress(self, job: ExportJob, frac: float, msg: str) -> None:
        self.ctx.signals.export_progress.emit(job.clip_id, float(frac), msg)

    # ------------------------------------------------------------------ per clip
    def _run(self, job: ExportJob, settings, on_progress) -> tuple[int, int, list[str]]:
        ctx = self.ctx
        clip = ctx.clip(job.clip_id)
        if clip is None:
            job.state, job.error = JobState.FAILED, "o corte sumiu do projeto"
            self._emit_state(job)
            return 0, 1, []
        job.state = JobState.RUNNING
        job.started_at, job.progress, job.message, job.error = time.time(), 0.0, "preparando", ""
        self._emit_state(job)
        self._emit_progress(job, 0.0, "preparando")

        def cb(frac: float, msg: str) -> None:
            if self._cancel.is_set():
                raise CancelledError("Exportação cancelada pelo usuário.")
            job.progress = max(job.progress, min(0.99, float(frac)))
            job.message = msg
            self._emit_progress(job, job.progress, msg)
            if on_progress:
                on_progress(job.clip_id, job.progress, msg)

        try:
            source = Path(clip.source_path)
            info = media.probe_or_raise(source)
            pipe = ctx.pipeline
            tmap = pipe.timeline_for(clip, remove_silences=clip.edit.remove_silences)
            aspect = clip.edit.aspect or settings.aspect
            self._emit_progress(job, 0.02, "calculando o enquadramento")
            plan = pipe.crop_plan(clip, aspect, cancel=self._cancel)
            out = Path(job.out_path)
            out.parent.mkdir(parents=True, exist_ok=True)
            ass = self._ass_for(clip, source, tmap, out, aspect)
            RD.render_clip(clip, source=source, info=info, transcript=None,
                           settings=settings, plan=plan, tmap=tmap, out=out,
                           progress=cb, cancel=self._cancel, ass_path=ass)
            check = self._verify(out, tmap.duration, settings, aspect)
            job.state = JobState.DONE
            job.finished_at, job.progress = time.time(), 1.0
            job.message = (f"{check['width']}x{check['height']}, "
                           f"{check['duration']:.1f}s, {check['mb']:.1f} MB")
            clip.exported_path = str(out)
            self._history(clip, job, check)
            self._emit_state(job)
            self._emit_progress(job, 1.0, job.message)
            ctx.signals.clip_updated.emit(clip.id)
            return 1, 0, [str(out)]
        except CancelledError as exc:
            job.state = JobState.CANCELLED
            job.message = str(exc) or "cancelado"
            self._emit_state(job)
            return 0, 0, []
        except Exception as exc:                              # noqa: BLE001
            job.state = JobState.FAILED
            job.error = _humanise(exc)
            job.message = job.error
            log.exception("export falhou para %s", clip.title)
            self._history(clip, job, None)
            self._emit_state(job)
            return 0, 1, []

    # ------------------------------------------------------------------ assists
    def _ass_for(self, clip: Clip, source: Path, tmap: TimelineMap, out: Path,
                 aspect=None):
        """Generate the ASS for burned captions, or ``None`` when not wanted."""
        settings = clip.edit.subtitles if clip.edit.subtitles is not None \
            else _defaults_for(self.ctx)
        if settings is None or settings.style is SubtitleStyle.NONE:
            return None
        # A escolha por projeto (ExportPage/EditorPage) manda; a configuração é
        # apenas o padrão global que o editor grava.
        proj_on = bool(getattr(self.ctx.project.export_settings, "burn_subtitles", True))
        if not (proj_on and self.ctx.config.bool_("export.burn_subtitles", True)):
            return None
        tr = self.ctx.transcript_for(source)
        if tr is None or not tr.segments:
            return None
        from .render import output_size
        w, h = output_size(self.ctx.project.export_settings, aspect)
        dest = out.with_suffix(".ass")
        try:
            return SUB.build_ass(tr, clip.start, clip.end, settings, dest,
                                 clip_w=w, clip_h=h,
                                 edits=clip.edit.manual_text_edits, tmap=tmap)
        except CorteiaError as exc:
            log.warning("legendas não geradas: %s", exc)
            return None

    def _verify(self, out: Path, expected: float, settings, aspect=None) -> dict:
        """Re-probe the finished file: never report success on faith."""
        info = media.probe_or_raise(out)
        want_w, want_h = settings.target_size if aspect is None else \
            RD.output_size(settings, aspect)
        drift = abs(info.duration - expected)
        if drift > max(0.75, expected * 0.06):
            raise RenderError("verificação",
                              f"o arquivo final tem {info.duration:.1f}s, esperado "
                              f"{expected:.1f}s", str(out))
        if abs(info.width - want_w) > 4 or abs(info.height - want_h) > 4:
            log.warning("resolução final %sx%s difere do alvo %sx%s",
                        info.width, info.height, want_w, want_h)
        return {"duration": info.duration, "width": info.width, "height": info.height,
                "mb": out.stat().st_size / 1e6, "fps": info.fps}

    def _history(self, clip: Clip, job: ExportJob, check: dict | None) -> None:
        proj = self.ctx.project
        proj.exports = [e for e in proj.exports if e.get("clip_id") != clip.id]
        elapsed = (job.finished_at or time.time()) - (job.started_at or time.time())
        proj.exports.insert(0, {
            "clip_id": clip.id, "title": clip.title, "out": job.out_path,
            "state": job.state.value, "error": job.error,
            "seconds": round(max(0.0, elapsed), 1),
            "size_mb": round(check["mb"], 1) if check else 0.0,
            "duration": round(check["duration"], 2) if check else 0.0,
            "at": time.time(),
            "codec": getattr(job.settings.codec, "value", str(job.settings.codec))
                     if job.settings else "",
            "resolution": f"{check['width']}x{check['height']}" if check else "",
        })
        proj.exports = proj.exports[:200]
        try:
            self.ctx.save()
        except Exception:                                     # noqa: BLE001
            pass


def _defaults_for(ctx):
    """Subtitle settings for a clip that never customised them.

    The style chosen on the clips page is the default; the per-clip editor wins
    over it, and ``NONE`` means nothing is burned at all.
    """
    opts = ctx.project.options
    style = getattr(opts, "subtitle_style", None) or SubtitleStyle.VIRAL
    if style is SubtitleStyle.NONE:
        return None
    preset = SUB.preset(style)
    preset.uppercase = bool(ctx.config.bool_("subs.uppercase", True))
    return preset


def estimate(ctx, clips=None) -> dict:
    """Pre-flight numbers for the export page: megabytes and minutes.

    The encode speed is measured from this machine's own export history, not a
    made-up constant; with no history yet we say so instead of pretending.
    """
    from .media import estimate_export_size
    proj = ctx.project
    chosen = [c for c in (clips if clips is not None else proj.clips)
              if c.status.value in ("approved", "edited")]
    if not chosen:
        chosen = list(proj.clips[:1])
    settings = proj.export_settings
    total_mb = 0.0
    total_s = 0.0
    measured = False
    speed = 1.6
    done_rows = [e for e in proj.exports
                 if e.get("state") == "done" and e.get("seconds") and e.get("duration")]
    if done_rows:
        speed = sum(e["seconds"] for e in done_rows) / max(0.1, sum(e["duration"] for e in done_rows))
        measured = True
    for c in chosen:
        try:
            info = media.probe(Path(c.source_path))
        except CorteiaError:
            continue
        tmap = ctx.pipeline.timeline_for(c)
        total_mb += estimate_export_size(info, tmap.duration, settings.quality_crf,
                                         getattr(settings.resolution, "value",
                                                 str(settings.resolution))) / 1e6
        total_s += tmap.duration * speed
    return {"clips": len(chosen), "mb": round(total_mb, 1),
            "minutes": round(total_s / 60.0, 1), "measured": measured,
            "out_dir": settings.out_dir or "", "speed": round(speed, 2)}


def _humanise(exc: BaseException) -> str:
    """One sentence the user can act on, never a traceback."""
    if isinstance(exc, RenderError):
        return exc.message
    if isinstance(exc, CorteiaError):
        return str(exc.message if hasattr(exc, "message") else exc)
    text = str(exc).strip()
    if not text:
        return "falha desconhecida ao exportar"
    return text[:280]
