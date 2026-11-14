"""``BusyOverlay`` + utilitários de execução fora da thread de interface.

O overlay é **não-modal**: cobre a área útil da janela, mostra um spinner em
``QPropertyAnimation`` de rotação contínua, a etapa corrente e um botão
"Cancelar" que chama de fato ``ctx.pipeline.cancel()``, ``ctx.exporter.cancel()``
e ``runner.cancel_all()``.

Expõe também :func:`get_runner` / :func:`run_job`, a única porta de saída da UI
para o núcleo. Usa ``corteia.workers.jobs.JobRunner`` quando o módulo existir
(contrato do núcleo) e, enquanto ele não estiver no pacote, um equivalente
próprio baseado em ``QThreadPool`` com a mesma assinatura — assim nenhum botão
da interface fica decorativo.
"""

from __future__ import annotations

import threading
import time
import traceback
from typing import Any, Callable

from PySide6.QtCore import (QAbstractAnimation, QEasingCurve, QObject, QPropertyAnimation, QRectF, QSize,
                            QRunnable, QThreadPool, Qt, Signal, Property)
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (QFrame, QGraphicsOpacityEffect, QHBoxLayout, QLabel,
                               QPushButton, QVBoxLayout, QWidget)

from .. import theme
from ...i18n import t

try:  # o núcleo pode ainda não existir durante o desenvolvimento
    from ...core.errors import CancelledError as _CancelledError
except Exception:  # noqa: BLE001
    from concurrent.futures import CancelledError as _CancelledError


# --------------------------------------------------------------------------- #
# spinner
# --------------------------------------------------------------------------- #
class Spinner(QWidget):
    """Arco girando em loop (usado no overlay e nas etapas em execução)."""

    def __init__(self, size: int = 28, color_token: str = "accent",
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._angle = 0.0
        self._color = color_token
        self.setFixedSize(size, size)
        self.setAccessibleName(t("Indicador de atividade"))
        self._anim = QPropertyAnimation(self, b"angle", self)
        self._anim.setDuration(900)
        self._anim.setStartValue(0.0)
        self._anim.setEndValue(360.0)
        self._anim.setLoopCount(-1)
        self._anim.setEasingCurve(QEasingCurve.Type.Linear)

    def _get_angle(self) -> float:
        return self._angle

    def _set_angle(self, value: float) -> None:
        self._angle = float(value) % 360.0
        self.update()

    angle = Property(float, _get_angle, _set_angle)

    def start(self) -> None:
        if self._anim.state() == QAbstractAnimation.State.Stopped:
            self._anim.start()

    def stop(self) -> None:
        self._anim.stop()

    def is_spinning(self) -> bool:
        return self._anim.state() != QAbstractAnimation.State.Stopped

    def set_color_token(self, token: str) -> None:
        self._color = token
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802 - nome da API Qt
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        margin = max(2.0, self.width() * 0.12)
        rect = QRectF(margin, margin, self.width() - 2 * margin, self.height() - 2 * margin)
        pen = QPen(QColor(theme.color(self._color)))
        pen.setWidthF(max(2.0, self.width() * 0.10))
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        p.drawArc(rect, int(self._angle * 16), 290 * 16)
        p.end()

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self.start()

    def hideEvent(self, event) -> None:  # noqa: N802
        self.stop()
        super().hideEvent(event)


# --------------------------------------------------------------------------- #
# job executado no QThreadPool
# --------------------------------------------------------------------------- #
class _JobSignals(QObject):
    """Ponte thread→UI: os sinais são entregues por fila no thread da GUI."""

    progress = Signal(float, str)
    done = Signal(object)
    failed = Signal(str, str)


class FunctionJob(QRunnable):
    """Executa ``fn(progress_cb)`` em thread de trabalho e reporta de volta.

    Mesma forma do ``corteia.workers.jobs.FunctionJob`` do contrato: o único
    argumento recebido é o callback de progresso (``percent`` 0..100, ``label``).
    """

    def __init__(self, fn: Callable[..., Any], name: str = "") -> None:
        super().__init__()
        self.fn = fn
        self.name = name
        self.signals = _JobSignals()
        self.cancel_event = threading.Event()
        self.setAutoDelete(True)
        self.started_at = 0.0
        self.finished_at = 0.0
        self.result: Any = None
        self.error: str = ""
        self._finished = False

    def cancel(self) -> None:
        """Marca o cancelamento; funções do núcleo observam ``ctx.pipeline``."""
        self.cancel_event.set()

    @property
    def cancelled(self) -> bool:
        return self.cancel_event.is_set()

    @property
    def running(self) -> bool:
        return bool(self.started_at and not self._finished)

    def progress_cb(self, percent: float, label: str = "") -> None:
        if self.cancel_event.is_set():
            raise _CancelledError(self.name or "job")
        self.signals.progress.emit(float(percent), str(label))

    def run(self) -> None:  # noqa: D102 - corpo do QRunnable
        self.started_at = time.time()
        try:
            self.result = self.fn(self.progress_cb)
        except Exception as exc:  # noqa: BLE001 - a UI precisa mostrar o erro
            self.error = ("".join(traceback.format_exception_only(type(exc), exc)).strip()
                          or str(exc) or exc.__class__.__name__)
            self._finished = True
            self.finished_at = time.time()
            if self.cancel_event.is_set():
                self.signals.done.emit(None)
            else:
                self.signals.failed.emit(self.error, self.name)
            return
        self._finished = True
        self.finished_at = time.time()
        if not self.cancel_event.is_set():
            self.signals.done.emit(self.result)


# --------------------------------------------------------------------------- #
# runner
# --------------------------------------------------------------------------- #
class SimpleJobRunner(QObject):
    """Equivalente local de ``corteia.workers.jobs.JobRunner``.

    Só entra em ação enquanto o módulo do núcleo não existe; mantém a mesma
    assinatura ``run(fn, *, name, on_done, on_error, on_progress)`` além de
    ``cancel_all()`` e ``is_busy()``.
    """

    job_started = Signal(str)
    job_finished = Signal(str)

    def __init__(self, ctx: Any = None, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self._jobs: list[FunctionJob] = []
        self._pool = QThreadPool.globalInstance()
        self._is_fallback = True

    def run(self, fn: Callable[..., Any], *, name: str = "",
            on_done: Callable[[Any], None] | None = None,
            on_error: Callable[[str], None] | None = None,
            on_progress: Callable[[float, str], None] | None = None) -> FunctionJob:
        job = FunctionJob(fn, name)
        if on_progress is not None:
            job.signals.progress.connect(on_progress)
        if on_done is not None:
            job.signals.done.connect(on_done)
        if on_error is not None:
            job.signals.failed.connect(lambda msg, _n: on_error(msg))
        job.signals.done.connect(lambda _r, j=job: self._forget(j))
        job.signals.failed.connect(lambda _m, _t, j=job: self._forget(j))
        self._jobs.append(job)
        self.job_started.emit(name)
        self._pool.start(job)
        return job

    def _forget(self, job: FunctionJob) -> None:
        if job in self._jobs:
            self._jobs.remove(job)
        self.job_finished.emit(job.name)

    def cancel_all(self) -> int:
        n = 0
        for job in list(self._jobs):
            job.cancel()
            n += 1
        return n

    def is_busy(self) -> bool:
        return bool(self._jobs)

    def jobs(self) -> list[FunctionJob]:
        return list(self._jobs)


_RUNNERS: dict[int, Any] = {}


def get_runner(ctx: Any = None) -> Any:
    """Devolve o ``JobRunner`` do contrato (ou o equivalente local), memoizado."""
    key = id(ctx) if ctx is not None else 0
    runner = _RUNNERS.get(key)
    if runner is not None:
        return runner
    try:
        from corteia.workers.jobs import JobRunner  # núcleo, por contrato

        runner = JobRunner(ctx) if ctx is not None else JobRunner()
    except Exception:  # noqa: BLE001 - ainda não instalado -> equivalente local
        runner = SimpleJobRunner(ctx)
    _RUNNERS[key] = runner
    return runner


def runner_is_fallback(ctx: Any = None) -> bool:
    return bool(getattr(get_runner(ctx), "_is_fallback", False))


def reset_runner_cache() -> None:
    """Esquece os runners (usado em testes e ao trocar de contexto)."""
    _RUNNERS.clear()


class JobHandle:
    """Retorno de :func:`run_job` com ``cancel()`` e ``is_running()``."""

    def __init__(self, job: Any = None, runner: Any = None) -> None:
        self.job = job
        self.runner = runner

    def cancel(self) -> None:
        job = self.job
        if job is not None and hasattr(job, "cancel"):
            job.cancel()

    def is_running(self) -> bool:
        return bool(getattr(self.job, "running", False))


def run_job(ctx: Any, fn: Callable[..., Any], *, name: str = "",
           on_done: Callable[[Any], None] | None = None,
           on_error: Callable[[str], None] | None = None,
           on_progress: Callable[[float, str], None] | None = None,
           busy: bool = True, busy_label: str = "",
           overlay: "BusyOverlay | None" = None) -> JobHandle:
    """Executa ``fn(progress_cb)`` fora da GUI com feedback honesto.

    ``overlay`` informado -> mostrar/ocultar automático. Sem overlay, o estado
    é anunciado por ``ctx.signals.busy_changed`` (a janela principal cuida do
    resto). Erros viram texto em português, nunca traceback na tela.
    """
    runner = get_runner(ctx)
    use_overlay = overlay is not None

    def _begin() -> None:
        if use_overlay:
            assert overlay is not None
            overlay.show_busy(busy_label or name or t("Processando"))
        elif busy and ctx is not None:
            ctx.signals.busy_changed.emit(True, busy_label or name)

    def _end() -> None:
        if use_overlay:
            assert overlay is not None
            overlay.hide_busy()
        elif busy and ctx is not None:
            ctx.signals.busy_changed.emit(False, "")

    def _done(result: Any) -> None:
        _end()
        if on_done is not None:
            on_done(result)

    def _fail(message: str) -> None:
        _end()
        if on_error is not None:
            on_error(message)
        elif ctx is not None:
            ctx.toast(f"{name or t('Operação')}: {message}", "error")

    def _progress(percent: float, label: str) -> None:
        if use_overlay:
            assert overlay is not None
            if label:
                overlay.set_detail(label, percent if 0.0 <= percent <= 100.0 else None)
        if on_progress is not None:
            on_progress(percent, label)

    _begin()
    try:
        job = runner.run(fn, name=name, on_done=_done, on_error=_fail, on_progress=_progress)
    except Exception as exc:  # noqa: BLE001 - o runner falhou, avisa e segue
        _end()
        _fail(str(exc))
        return JobHandle(None, runner)
    return JobHandle(job, runner)


# --------------------------------------------------------------------------- #
# overlay
# --------------------------------------------------------------------------- #
class BusyOverlay(QWidget):
    """Camada translúcida (não-modal) com spinner, etapa corrente e Cancelar."""

    cancelRequested = Signal()

    def __init__(self, host: QWidget | None = None, ctx: Any = None,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent or host)
        self.setObjectName("busyOverlay")
        self.ctx = ctx
        self._host: QWidget | None = None
        self._effect = QGraphicsOpacityEffect(self)
        self._effect.setOpacity(0.0)
        self.setGraphicsEffect(self._effect)
        self._anim = QPropertyAnimation(self._effect, b"opacity", self)
        self._anim.setDuration(180)
        self._anim.setEasingCurve(QEasingCurve.Type.OutQuad)
        self.setVisible(False)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)

        card = QFrame(self)
        card.setObjectName("busyCard")
        card.setMinimumWidth(340)
        card.setMaximumWidth(460)
        lay = QVBoxLayout(card)
        lay.setContentsMargins(24, 20, 24, 16)
        lay.setSpacing(12)

        top = QHBoxLayout()
        top.setSpacing(14)
        self.spinner = Spinner(34, "accent", card)
        top.addWidget(self.spinner, 0, Qt.AlignmentFlag.AlignTop)
        col = QVBoxLayout()
        col.setSpacing(3)
        self.label = QLabel(t("Processando"), card)
        self.label.setObjectName("busyLabel")
        self.detail = QLabel("", card)
        self.detail.setObjectName("busyDetail")
        self.detail.setWordWrap(True)
        col.addWidget(self.label)
        col.addWidget(self.detail)
        top.addLayout(col, 1)
        lay.addLayout(top)

        from .progress import MiniBar  # tardio: evita import circular

        self.bar = MiniBar(card)
        self.bar.set_indeterminate(True)
        self.bar.setVisible(False)
        lay.addWidget(self.bar)

        row = QHBoxLayout()
        row.addStretch(1)
        self.btn_cancel = QPushButton(t("Cancelar"), card)
        self.btn_cancel.setProperty("variant", "danger")
        self.btn_cancel.setProperty("size", "sm")
        self.btn_cancel.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_cancel.setAccessibleName(t("Cancelar processamento"))
        self.btn_cancel.setToolTip(t("Interrompe a etapa atual (Esc)"))
        self.btn_cancel.clicked.connect(self.request_cancel)
        row.addWidget(self.btn_cancel)
        lay.addLayout(row)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addStretch(1)
        outer.addWidget(card, 0, Qt.AlignmentFlag.AlignHCenter)
        outer.addStretch(1)

        target = host or parent
        if target is not None:
            self.attach(target)

    # --------------------------------------------------------------- anexar
    def attach(self, host: QWidget) -> None:
        """Passa a cobrir ``host`` (normalmente o ``centralWidget``)."""
        self._host = host
        if self.parent() is not host:
            self.setParent(host)
        host.installEventFilter(self)
        self.sync_geometry()
        self.raise_()

    def sync_geometry(self) -> None:
        if self._host is not None:
            self.setGeometry(0, 0, self._host.width(), self._host.height())

    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        from PySide6.QtCore import QEvent

        if obj is self._host and event.type() == QEvent.Type.Resize:
            self.sync_geometry()
        return super().eventFilter(obj, event)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self.sync_geometry()

    # ------------------------------------------------------------------- API
    def show_busy(self, label: str = "", detail: str = "") -> None:
        self.sync_geometry()
        self.label.setText(label or t("Processando"))
        self.detail.setText(detail)
        self.btn_cancel.setEnabled(True)
        self.btn_cancel.setText(t("Cancelar"))
        self.bar.set_indeterminate(True)
        self.setVisible(True)
        self.raise_()
        self.spinner.start()
        self._fade_to(1.0)

    def hide_busy(self) -> None:
        if not self.isVisible():
            return
        self._fade_to(0.0, hide=True)

    def _fade_to(self, value: float, hide: bool = False) -> None:
        self._anim.stop()
        self._anim.setStartValue(self._effect.opacity())
        self._anim.setEndValue(value)
        try:
            self._anim.finished.disconnect()
        except (RuntimeError, TypeError):
            pass
        if hide:
            self._anim.finished.connect(self._finished_hide)
        self._anim.start()

    def _finished_hide(self) -> None:
        self.setVisible(False)
        self.spinner.stop()
        self.bar.setVisible(False)

    def set_label(self, label: str) -> None:
        self.label.setText(label or t("Processando"))

    def set_detail(self, detail: str, percent: float | None = None) -> None:
        self.detail.setText(detail)
        self.bar.setVisible(bool(detail) or percent is not None)
        if percent is not None:
            self.bar.set_indeterminate(False)
            self.bar.set_percent(percent)
        else:
            self.bar.set_indeterminate(True)

    def is_showing(self) -> bool:
        return self.isVisible()

    # ---------------------------------------------------------- cancelamento
    def request_cancel(self) -> None:
        """Aciona o cancelamento real em todas as frentes conhecidas."""
        self.btn_cancel.setEnabled(False)
        self.btn_cancel.setText(t("Cancelando…"))
        self.cancelRequested.emit()
        ctx = self.ctx
        stopped = 0
        runner = _RUNNERS.get(id(ctx)) if ctx is not None else None
        if runner is not None:
            try:
                stopped += int(runner.cancel_all() or 0)
            except Exception:  # noqa: BLE001
                pass
        for attr in ("pipeline", "exporter"):
            # objeto já instanciado: nunca criar o núcleo só para cancelar
            obj = getattr(ctx, f"_{attr}", None) if ctx is not None else None
            if obj is None:
                continue
            try:
                obj.cancel()
                stopped += 1
            except Exception:  # noqa: BLE001
                pass
        if not stopped and ctx is not None:
            ctx.toast(t("Nada em andamento para cancelar."), "info")

    def minimumSizeHint(self) -> QSize:
        return QSize(160, 90)
