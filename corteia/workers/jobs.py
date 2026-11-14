"""Offloading of every blocking core call to a dedicated thread pool.

Contract used by the rest of the app::

    runner = JobRunner(ctx)
    job = runner.run(fn, name="análise", on_done=..., on_error=..., on_progress=...)

``fn`` is called in a worker thread as ``fn(progress)``, where ``progress`` is
the callable ``progress(percent: float, label: str = "")``. Every callback is
delivered through Qt signals, so it always runs on the GUI thread and may touch
widgets safely.

Cancellation is one ``threading.Event`` per job: cooperative functions poll it
through ``progress.cancel`` / ``progress.check()`` and raise the core's
``CancelledError``. Jobs queued but not started are dropped outright.
"""

from __future__ import annotations

import inspect
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal, Slot

log = logging.getLogger("corteia")

# job states (Job.state)
PENDING = "pending"
RUNNING = "running"
DONE = "done"
FAILED = "failed"
CANCELLED = "cancelled"

CANCELLED_TEXT = "Operação cancelada pelo usuário."


def error_text(exc: BaseException) -> str:
    """User facing (Portuguese) text for any exception.

    ``corteia.core.errors.CorteiaError`` already carries a ``message`` written
    for the user (and ``ModelMissingError`` a ``hint``); anything else falls
    back to ``str(exc)`` and finally to a generic sentence, so the UI never has
    to show a traceback.
    """
    try:
        message = getattr(exc, "message", None)
        if isinstance(message, str) and message.strip():
            hint = getattr(exc, "hint", None)
            if isinstance(hint, str) and hint.strip() and hint.strip() not in message:
                return f"{message.strip()} {hint.strip()}"
            return message.strip()
        text = str(exc).strip()
        if text:
            return text
    except Exception:                                        # noqa: BLE001
        pass
    return f"Falha inesperada ({exc.__class__.__name__}). Veja o log para detalhes."


def is_cancelled_error(exc: BaseException) -> bool:
    """True for the core's ``CancelledError`` without importing the core."""
    return exc.__class__.__name__ == "CancelledError"


def _positional_count(fn: Callable) -> int:
    """How many positional parameters ``fn`` accepts (1 when unsure)."""
    try:
        all_params = list(inspect.signature(fn).parameters.values())
        if any(p.kind is p.VAR_POSITIONAL for p in all_params):
            return 1
        return len([p for p in all_params
                    if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)])
    except (TypeError, ValueError):
        return 1


def invoke_callback(cb: Callable | None, *args: Any) -> None:
    """Call ``cb`` with as many of ``args`` as its signature accepts.

    Keeps ``on_done(result)`` / ``on_error(text)`` simple while still allowing
    the richer ``on_error(text, job)`` form. Callback failures are logged and
    never propagate into the job state.
    """
    if cb is None:
        return
    count = min(len(args), _positional_count(cb))
    try:
        cb(*args[:count])
    except Exception:                                        # noqa: BLE001
        log.exception("callback do job gerou exceção")


class ProgressReporter:
    """Thread-safe progress handle handed to the worker function.

    ``progress(42.0, "legendo")`` queues a signal for the GUI thread. The
    object also exposes ``.cancel`` (the job's ``threading.Event``),
    ``.cancelled()`` and ``.check()`` (raises the core's ``CancelledError``).
    """

    __slots__ = ("key", "job", "_runner", "_event", "last_percent", "last_label")

    def __init__(self, runner: "JobRunner", job: "Job") -> None:
        self.key = job.key
        self.job = job
        self._runner = runner
        self._event = job.cancel_event
        self.last_percent = 0.0
        self.last_label = ""

    def __call__(self, percent: float = 0.0, label: str = "") -> bool:
        """Report progress; returns ``False`` once cancellation was requested.

        Cooperative loops can use ``if not progress(p): return`` instead of
        wrapping every call in a try/except.
        """
        try:
            p = float(percent)
        except (TypeError, ValueError):
            p = self.last_percent
        if p != p:                                           # NaN guard
            p = self.last_percent
        p = max(0.0, min(100.0, p))
        text = str(label or "")
        self.last_percent, self.last_label = p, text
        try:
            self._runner.report_progress(self.key, p, text)
        except RuntimeError as exc:
            # The app is shutting down and the runner object is gone: the
            # worker thread outlived the GUI.  Nothing to report, nothing to
            # tell the user -- logging a traceback here would bury real bugs.
            log.debug("progresso descartado: %s", exc)
        except Exception:                                    # noqa: BLE001
            log.exception("falha ao entregar progresso")
        return not self._event.is_set()

    # ---------------------------------------------------------- cancellation
    @property
    def cancel(self) -> threading.Event:
        """The job cancel event (pass to core functions as ``cancel=``)."""
        return self._event

    def cancelled(self) -> bool:
        return self._event.is_set()

    def check(self) -> None:
        """Raise ``corteia.core.errors.CancelledError`` if asked to stop."""
        if self._event.is_set():
            from corteia.core.errors import CancelledError

            raise CancelledError()


@dataclass(slots=True)
class Job:
    """Handle returned by :meth:`JobRunner.run`."""

    key: str
    name: str
    cancel_event: threading.Event = field(default_factory=threading.Event, repr=False)
    state: str = PENDING
    result: Any = None
    error: str = ""
    percent: float = 0.0
    label: str = ""
    started_at: float = 0.0
    finished_at: float = 0.0

    def cancel(self) -> bool:
        """Ask for cancellation; ``True`` when the request is new."""
        if self.cancel_event.is_set():
            return False
        self.cancel_event.set()
        if self.state == PENDING:
            self.state = CANCELLED
            self.finished_at = time.time()
        return True

    @property
    def cancelled(self) -> bool:
        return self.cancel_event.is_set()

    @property
    def finished(self) -> bool:
        return self.state in (DONE, FAILED, CANCELLED)

    @property
    def running(self) -> bool:
        return self.state == RUNNING

    def elapsed(self) -> float:
        """Wall clock seconds spent; 0.0 when the job never started."""
        if not self.started_at:
            return 0.0
        return (self.finished_at or time.time()) - self.started_at

    def state_text(self) -> str:
        return {
            PENDING: "na fila", RUNNING: "em execução", DONE: "concluído",
            FAILED: "falhou", CANCELLED: "cancelado",
        }.get(self.state, self.state)

    def __repr__(self) -> str:                                   # pragma: no cover
        return f"<Job {self.key} {self.state} {self.percent:.0f}%>"


class FunctionJob(QRunnable):
    """QRunnable that executes one function and never lets an exception escape.

    A crash inside a worker thread would silently kill the pool thread, so
    everything is caught and converted into a readable message emitted through
    the runner's signals.
    """

    def __init__(self, runner: "JobRunner", fn: Callable[..., Any], job: Job) -> None:
        super().__init__()
        self.setAutoDelete(True)
        self._runner = runner
        self._fn = fn
        self._job = job
        self._progress = ProgressReporter(runner, job)

    @property
    def job(self) -> Job:
        return self._job

    @property
    def progress(self) -> ProgressReporter:
        return self._progress

    def _safe_emit(self, signal_name: str, *args: Any) -> None:
        """Emit a runner signal, tolerating a widget that was already deleted.

        A ``QRunnable`` outlives the page that started it: if that page is
        closed while the job runs, Qt raises ``RuntimeError: Signal source has
        been deleted`` from inside ``run()`` and prints a traceback no user can
        act on.  The result is dropped in that case, which is the right answer.
        """
        try:
            getattr(self._runner, signal_name).emit(*args)
        except RuntimeError as exc:
            log.debug("resultado do job perdido (%s): %s", signal_name, exc)

    def run(self) -> None:                                       # worker thread
        job = self._job
        job.state = RUNNING
        job.started_at = time.time()
        try:
            self._runner.notify_started(job.key)
        except RuntimeError as exc:
            log.debug("aviso de início perdido: %s", exc)
        try:
            result = self._invoke()
            if job.cancel_event.is_set():
                self._safe_emit("job_cancelled", job.key)
            else:
                self._safe_emit("job_finished", job.key, result)
        except BaseException as exc:                             # noqa: BLE001
            if is_cancelled_error(exc) or job.cancel_event.is_set():
                self._safe_emit("job_cancelled", job.key)
            else:
                log.warning("job %s falhou: %s", job.key, exc, exc_info=True)
                self._safe_emit("job_failed", job.key, error_text(exc))

    def _invoke(self) -> Any:
        """Call the payload with whatever arguments its signature accepts."""
        count = _positional_count(self._fn)
        if count == 0:
            return self._fn()
        if count >= 2:
            return self._fn(self._progress, self._job.cancel_event)
        return self._fn(self._progress)


class JobRunner(QObject):
    """Queue, thread pool and signal bridge for blocking work started by the UI.

    Parallelism comes from ``proc.max_parallel_jobs``: extra jobs wait in an
    internal queue instead of being dumped on the pool, which keeps memory
    predictable and lets :meth:`cancel` drop work that never started.
    """

    # internal cross-thread bridge (emitted from worker threads, consumed here)
    progress = Signal(str, float, str)
    done = Signal(str, object)
    error = Signal(str, str)

    # bookkeeping signals for pages that want queue level detail
    job_started = Signal(str)
    job_cancelled = Signal(str)
    job_failed = Signal(str, str)
    job_finished = Signal(str, object)
    queue_changed = Signal()

    def __init__(self, ctx: Any = None, parent: QObject | None = None, *,
                 max_parallel: int | None = None) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self._pool = QThreadPool(self)
        self._limit = max(1, int(max_parallel if max_parallel is not None
                                 else self._limit_from_config(ctx)))
        self._pool.setMaxThreadCount(self._limit)
        self._lock = threading.Lock()
        self._jobs: dict[str, Job] = {}
        self._fns: dict[str, Callable[..., Any]] = {}
        self._callbacks: dict[str, dict[str, Callable | None]] = {}
        self._active: set[str] = set()
        self._pending: list[str] = []
        self._seq = 0

        # the three bridge signals are the ones worker threads may emit;
        # job_finished/job_failed/job_cancelled reuse the same slots.
        self.progress.connect(self._on_progress)
        self.done.connect(self._on_done)
        self.error.connect(self._on_error)
        self.job_finished.connect(self._on_done)
        self.job_failed.connect(self._on_error)
        self.job_cancelled.connect(self._on_cancelled)

    @staticmethod
    def _limit_from_config(ctx: Any) -> int:
        try:
            cfg = getattr(ctx, "config", None)
            if cfg is not None:
                return max(1, cfg.int_("proc.max_parallel_jobs"))
        except Exception:                                        # noqa: BLE001
            log.debug("proc.max_parallel_jobs ilegível", exc_info=True)
        try:
            from corteia.config import get_config

            return max(1, get_config().int_("proc.max_parallel_jobs") or 2)
        except Exception:                                        # noqa: BLE001
            return 2

    @property
    def max_parallel(self) -> int:
        return self._limit

    def set_max_parallel(self, n: int) -> None:
        """Change concurrency live (queued jobs start immediately)."""
        self._limit = max(1, int(n))
        self._pool.setMaxThreadCount(self._limit)
        self._drain()

    # -------------------------------------------------------------------- api
    def run(self, fn: Callable[..., Any], *, name: str = "",
            on_done: Callable | None = None, on_error: Callable | None = None,
            on_progress: Callable | None = None) -> Job:
        """Start (or queue) ``fn`` and return its :class:`Job` handle."""
        if not callable(fn):
            raise TypeError("JobRunner.run precisa de uma função chamável")
        with self._lock:
            self._seq += 1
            base = (name or "job").strip() or "job"
            key = f"{base}#{self._seq}"
            job = Job(key=key, name=base)
            self._jobs[key] = job
            self._fns[key] = fn
            self._callbacks[key] = {"done": on_done, "error": on_error,
                                    "progress": on_progress}
            start_now = len(self._active) < self._limit
            if start_now:
                self._active.add(key)
            else:
                self._pending.append(key)
        self._emit_busy()
        if start_now:
            self._pool.start(FunctionJob(self, fn, job))
        else:
            log.debug("job %s na fila (%d na frente)", key, len(self._pending))
        self.queue_changed.emit()
        return job

    def report_progress(self, key: str, percent: float, label: str) -> None:
        """Called from worker threads; marshals to the GUI thread via a signal."""
        self.progress.emit(key, float(percent), str(label or ""))

    def cancel(self, name: str | None = None) -> int:
        """Cancel the job(s) matching ``name`` (all when ``None``).

        Returns how many jobs received the request. Queued jobs are dropped
        outright; running ones are asked to stop through their event.
        """
        cancelled = 0
        dropped: list[str] = []
        with self._lock:
            targets = [
                j for j in self._jobs.values()
                if not j.finished and (
                    name is None or j.name == name or j.key.startswith(f"{name}#"))
            ]
            for job in targets:
                if job.state == PENDING:
                    dropped.append(job.key)
                job.cancel_event.set()
                cancelled += 1
            for key in dropped:
                if key in self._pending:
                    self._pending.remove(key)
                job = self._jobs.get(key)
                if job is not None:
                    job.state = CANCELLED
                    job.finished_at = time.time()
                    job.error = CANCELLED_TEXT
                self._release(key)
        if cancelled:
            self._emit_busy()
            self.queue_changed.emit()
        return cancelled

    def cancel_all(self) -> int:
        """Cancel every running and queued job."""
        return self.cancel(None)

    def is_busy(self) -> bool:
        with self._lock:
            return bool(self._active or self._pending)

    @property
    def busy(self) -> bool:
        return self.is_busy()

    def active_count(self) -> int:
        with self._lock:
            return len(self._active)

    def queued_count(self) -> int:
        with self._lock:
            return len(self._pending)

    def job(self, key: str) -> Job | None:
        with self._lock:
            return self._jobs.get(key)

    def latest(self, name: str | None = None) -> Job | None:
        """Most recent job (optionally filtered by name)."""
        with self._lock:
            items = [j for j in self._jobs.values() if name is None or j.name == name]
        return items[-1] if items else None

    def names_busy(self) -> list[str]:
        with self._lock:
            return [self._jobs[k].name for k in self._active if k in self._jobs]

    def wait_idle(self, timeout_ms: int = 10000, interval_ms: int = 15) -> bool:
        """Pump the event loop until nothing is running/queued (test helper)."""
        from PySide6.QtWidgets import QApplication

        deadline = time.time() + timeout_ms / 1000.0
        while self.is_busy() and time.time() < deadline:
            QApplication.processEvents()
            time.sleep(interval_ms / 1000.0)
        QApplication.processEvents()
        return not self.is_busy()

    def shutdown(self, wait_ms: int = 2000) -> None:
        """Cancel everything and wait for the pool (app exit / tests)."""
        self.cancel_all()
        self._pool.waitForDone(wait_ms)

    # -------------------------------------------------------------- internals
    def _release(self, key: str) -> None:
        """Forget bookkeeping for a finished key (call with the lock held)."""
        self._active.discard(key)
        self._fns.pop(key, None)
        if len(self._jobs) > 80:
            for old in list(self._jobs):
                if old not in self._active and old not in self._pending \
                        and self._jobs[old].finished:
                    self._jobs.pop(old, None)
                    self._callbacks.pop(old, None)
                    break

    def _drain(self) -> None:
        """Start queued jobs while a slot is free."""
        to_start: list[tuple[Job, Callable[..., Any]]] = []
        with self._lock:
            while self._pending and len(self._active) < self._limit:
                key = self._pending.pop(0)
                job = self._jobs.get(key)
                fn = self._fns.get(key)
                if job is None or fn is None or job.cancel_event.is_set():
                    self._active.discard(key)
                    continue
                self._active.add(key)
                to_start.append((job, fn))
        for job, fn in to_start:
            self._pool.start(FunctionJob(self, fn, job))
        if to_start:
            self._emit_busy()
            self.queue_changed.emit()

    def _emit_busy(self) -> None:
        """Publish the busy state on the app signal bus (never a fake percent)."""
        sigs = getattr(self.ctx, "signals", None) if self.ctx is not None else None
        if sigs is None:
            return
        names = self.names_busy()
        try:
            sigs.busy_changed.emit(bool(self.is_busy()), names[0] if names else "")
        except Exception:                                        # noqa: BLE001
            log.debug("busy_changed não pôde ser emitido", exc_info=True)

    # ------------------------------------------------------------------ slots
    @Slot(str)
    def notify_started(self, key: str) -> None:
        self.job_started.emit(key)

    @Slot(str, float, str)
    def _on_progress(self, key: str, percent: float, label: str) -> None:
        with self._lock:
            job = self._jobs.get(key)
            cb = (self._callbacks.get(key) or {}).get("progress")
        if job is not None:
            job.percent, job.label = percent, label
        invoke_callback(cb, percent, label, key)

    @Slot(str, object)
    def _on_done(self, key: str, result: Any) -> None:
        with self._lock:
            job = self._jobs.get(key)
            cb = (self._callbacks.get(key) or {}).get("done")
            if job is not None and not job.finished:
                job.state = DONE
                job.result = result
                job.finished_at = time.time()
                if not job.percent:
                    job.percent = 100.0
                self._release(key)
        invoke_callback(cb, result, job, key)
        self._drain()
        self._emit_busy()

    @Slot(str, str)
    def _on_error(self, key: str, text: str) -> None:
        with self._lock:
            job = self._jobs.get(key)
            cb = (self._callbacks.get(key) or {}).get("error")
            if job is not None and not job.finished:
                job.state = FAILED
                job.error = text
                job.finished_at = time.time()
                self._release(key)
        invoke_callback(cb, text, job, key)
        self._drain()
        self._emit_busy()

    @Slot(str)
    def _on_cancelled(self, key: str) -> None:
        with self._lock:
            job = self._jobs.get(key)
            cb = (self._callbacks.get(key) or {}).get("error")
            if job is not None and not job.finished:
                job.state = CANCELLED
                job.error = CANCELLED_TEXT
                job.finished_at = time.time()
                self._release(key)
        invoke_callback(cb, CANCELLED_TEXT, job, key)
        self._drain()
        self._emit_busy()


# --------------------------------------------------------------------- runners
_RUNNERS: dict[int, JobRunner] = {}
_RUNNERS_LOCK = threading.Lock()


def runner_for(ctx: Any = None, *, max_parallel: int | None = None) -> JobRunner:
    """The shared :class:`JobRunner` of ``ctx`` (created on first use)."""
    if ctx is not None:
        existing = getattr(ctx, "_job_runner", None)
        if isinstance(existing, JobRunner):
            return existing
    with _RUNNERS_LOCK:
        token = id(ctx)
        runner = _RUNNERS.get(token)
        if runner is None:
            runner = JobRunner(ctx, max_parallel=max_parallel)
            _RUNNERS[token] = runner
        if ctx is not None:
            try:
                setattr(ctx, "_job_runner", runner)
            except Exception:                                    # noqa: BLE001
                pass
        return runner


def run_blocking_in_thread(ctx: Any, fn: Callable[..., Any], *, name: str = "",
                           on_done: Callable | None = None, on_error: Callable | None = None,
                           on_progress: Callable | None = None) -> Job:
    """Run ``fn`` in the shared worker pool of ``ctx`` (contract helper)."""
    return runner_for(ctx).run(fn, name=name, on_done=on_done, on_error=on_error,
                               on_progress=on_progress)


def drop_runner(ctx: Any) -> None:
    """Forget the runner attached to ``ctx`` (tests / app shutdown)."""
    with _RUNNERS_LOCK:
        _RUNNERS.pop(id(ctx), None)
    if ctx is not None:
        try:
            delattr(ctx, "_job_runner")
        except Exception:                                        # noqa: BLE001
            pass
