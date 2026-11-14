"""``StageProgress`` e ``MiniBar`` — leitura visual do pipeline.

``StageProgress`` não inventa nada: recebe ``pipeline.stages()`` (lista de
dicionários com ``id``, ``label``, ``done``, ``percent``, ``note``) e desenha uma
linha por estágio com ícone de estado, barra, percentual em fonte mono e o tempo
decorrido medido **aqui** — o núcleo não devolve cronômetro.
"""

from __future__ import annotations

import time
from typing import Any, Iterable

from PySide6.QtCore import (QEasingCurve, QObject, QPropertyAnimation, QSize, Qt,
                            QTimer, Signal)
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (QFrame, QGraphicsOpacityEffect, QHBoxLayout, QLabel,
                               QProgressBar, QVBoxLayout, QWidget)

from .. import theme
from ...i18n import t
from ...models import format_ts


class MiniBar(QProgressBar):
    """Barra compacta com percentual 0..100, modo indeterminado e papel de cor."""

    def __init__(self, parent: QWidget | None = None, percent: float = 0.0) -> None:
        super().__init__(parent)
        self.setProperty("mini", "true")
        self.setTextVisible(False)
        self.setRange(0, 1000)
        self.setValue(int(max(0.0, min(100.0, float(percent))) * 10))
        self._indeterminate = False
        self.setMinimumHeight(6)
        self.setMaximumHeight(8)
        self.setAccessibleName(t("Barra de progresso"))
        self._anim: QPropertyAnimation | None = None

    def set_percent(self, percent: float) -> None:
        """Aceita 0..100 (ou 0..1 por tolerância) e anima até o valor."""
        value = float(percent)
        if value <= 1.0:
            value *= 100.0
        value = max(0.0, min(100.0, value))
        if self._indeterminate:
            self.set_indeterminate(False)
        if self._anim is None:
            self._anim = QPropertyAnimation(self, b"value", self)
            self._anim.setDuration(180)
            self._anim.setEasingCurve(QEasingCurve.Type.OutQuad)
        self._anim.stop()
        self._anim.setStartValue(self.value())
        self._anim.setEndValue(int(value * 10))
        self._anim.start()

    def percent(self) -> float:
        return self.value() / 10.0

    def set_indeterminate(self, on: bool) -> None:
        self._indeterminate = bool(on)
        if on:
            self.setRange(0, 0)
        else:
            self.setRange(0, 1000)

    def is_indeterminate(self) -> bool:
        return self._indeterminate

    def set_role(self, role: str) -> None:
        """``ok`` | ``warn`` | ``error`` | ``""`` (acento)."""
        self.setProperty("role", role or "")
        self.style().unpolish(self)
        self.style().polish(self)
        self.update()


class _StageRow(QFrame):
    """Linha de um estágio: ícone, rótulo, barra, %, tempo e nota."""

    clicked = Signal(str)

    def __init__(self, stage_id: str, label: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.stage_id = stage_id
        self.setProperty("stage", "true")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._icon = QLabel(self)
        self._icon.setFixedSize(22, 22)
        self._icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._icon.setScaledContents(False)
        self._label = QLabel(label, self)
        self._label.setProperty("stageLabel", "true")
        self._bar = MiniBar(self)
        self._bar.setMinimumWidth(90)
        self._pct = QLabel("0%", self)
        self._pct.setProperty("stagePct", "true")
        self._pct.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self._pct.setMinimumWidth(46)
        self._time = QLabel("", self)
        self._time.setProperty("stagePct", "true")
        self._time.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self._time.setMinimumWidth(50)
        self._note = QLabel("", self)
        self._note.setProperty("stageNote", "true")
        self._note.setContentsMargins(37, 0, 0, 0)
        self._note.setVisible(False)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(6, 3, 6, 3)
        outer.setSpacing(1)
        row = QHBoxLayout()
        row.setSpacing(9)
        row.addWidget(self._icon)
        row.addWidget(self._label)
        row.addWidget(self._bar, 1)
        row.addWidget(self._pct)
        row.addWidget(self._time)
        outer.addLayout(row)
        outer.addWidget(self._note)

        self._spinner: Any = None
        self._fade = QGraphicsOpacityEffect(self)
        self._fade.setOpacity(0.0)
        self.setGraphicsEffect(self._fade)
        self._appear = QPropertyAnimation(self._fade, b"opacity", self)
        self._appear.setDuration(220)
        self._appear.setStartValue(0.0)
        self._appear.setEndValue(1.0)
        self._appear.setEasingCurve(QEasingCurve.Type.OutQuad)
        self._appear.start()

    # ------------------------------------------------------------------ pinta
    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self.stage_id)
        super().mousePressEvent(event)

    def set_percent_text(self, text: str) -> None:
        self._pct.setText(text)

    def show_note(self, note: str) -> None:
        if note:
            self._note.setText(note)
            self._note.setVisible(True)
        else:
            self._note.clear()
            self._note.setVisible(False)

    # ----------------------------------------------------------------- estado
    def update_state(self, *, done: bool, percent: float, note: str, label: str) -> None:
        if label:
            self._label.setText(label)
        self._label.setProperty("done", "true" if done else "false")
        for widget in (self._label,):
            widget.style().unpolish(widget)
            widget.style().polish(widget)
        self.show_note(note)
        self.setToolTip(note or label)

        value = float(percent)
        if 0.0 < value <= 1.0:
            value *= 100.0
        value = max(0.0, min(100.0, value))

        if done:
            self._set_spinner(False)
            self._icon.setPixmap(theme.pixmap("check", "success", 18))
            self._bar.set_indeterminate(False)
            self._bar.set_percent(100.0)
            self._bar.set_role("ok")
            self._pct.setText("100%")
        elif value <= 0.0:
            self._set_spinner(False)
            self._icon.setPixmap(theme.pixmap("dot", "faint", 18))
            self._bar.set_indeterminate(False)
            self._bar.setValue(0)
            self._bar.set_role("")
            self._pct.setText(t("na fila"))
        else:
            self._set_spinner(True)
            self._icon.setPixmap(QPixmap())
            self._bar.set_indeterminate(False)
            self._bar.set_role("warn" if value >= 100.0 else "")
            self._bar.set_percent(value)
            self._pct.setText(f"{value:.0f}%")

    def set_spinner_visible(self, visible: bool) -> None:
        if self._spinner is not None:
            self._spinner.setVisible(visible)
            if visible:
                self._spinner.start()
            else:
                self._spinner.stop()

    def _set_spinner(self, visible: bool) -> None:
        if visible:
            if self._spinner is None:
                from .busy import Spinner  # tardio: evita import circular

                lay = QHBoxLayout(self._icon)
                lay.setContentsMargins(0, 0, 0, 0)
                self._icon.setLayout(lay)
                self._spinner = Spinner(20, "accent", self._icon)
                lay.addWidget(self._spinner)
            self.set_spinner_visible(True)
        else:
            self.set_spinner_visible(False)

    def set_elapsed(self, seconds: float | None, *, running: bool = False) -> None:
        if seconds is None or seconds <= 0.0:
            self._time.setText("")
            return
        self._time.setText(format_ts(seconds))
        self._time.setToolTip(t("Tempo decorrido desta etapa") if running
                              else t("Tempo total desta etapa"))


class StageProgress(QWidget):
    """Lista de etapas lida de ``pipeline.stages()`` (ou injetada nos testes)."""

    stageClicked = Signal(str)

    def __init__(self, ctx: Any = None, parent: QWidget | None = None,
                 title: str = "") -> None:
        super().__init__(parent)
        self.ctx = ctx
        self._pipeline: Any = None
        self._rows: dict[str, _StageRow] = {}
        self._clock: dict[str, dict[str, float]] = {}
        self._order: list[str] = []

        self._lay = QVBoxLayout(self)
        self._lay.setContentsMargins(0, 0, 0, 0)
        self._lay.setSpacing(4)
        self._title = QLabel(title, self)
        self._title.setProperty("role", "section")
        self._title.setVisible(bool(title))
        self._summary = QLabel("", self)
        self._summary.setProperty("role", "muted")
        self._summary.setWordWrap(True)
        self._rows_box = QWidget(self)
        self._rows_box.setProperty("transparent", "true")
        self._rows_lay = QVBoxLayout(self._rows_box)
        self._rows_lay.setContentsMargins(0, 0, 0, 0)
        self._rows_lay.setSpacing(1)
        self._lay.addWidget(self._title)
        self._lay.addWidget(self._summary)
        self._lay.addWidget(self._rows_box)
        self._lay.addStretch(1)

        self._tick = QTimer(self)
        self._tick.setInterval(1000)
        self._tick.timeout.connect(self._retime)

    # --------------------------------------------------------------------- API
    def set_title(self, title: str) -> None:
        self._title.setText(title)
        self._title.setVisible(bool(title))

    def set_pipeline(self, pipeline: Any) -> None:
        """Usa o ``pipeline`` informado no próximo :meth:`refresh`."""
        self._pipeline = pipeline

    def set_ctx(self, ctx: Any) -> None:
        self.ctx = ctx

    def _pipeline_obj(self) -> Any:
        if self._pipeline is not None:
            return self._pipeline
        if self.ctx is not None:
            try:
                return self.ctx.pipeline
            except Exception:  # noqa: BLE001 - núcleo ainda não instalado
                return None
        return None

    def refresh(self) -> None:
        """Relê ``pipeline.stages()``; se o núcleo não existir, avisa de frente."""
        pipeline = self._pipeline_obj()
        if pipeline is None:
            self.set_message(t("Processador indisponível: instale os módulos de núcleo."))
            return
        try:
            stages = list(pipeline.stages())
        except Exception as exc:  # noqa: BLE001
            self.set_message(f"{t('Não foi possível ler as etapas')}: {exc}")
            return
        self.set_stages(stages)

    def set_message(self, message: str) -> None:
        for row in self._rows.values():
            row.setVisible(False)
        self._order = []
        self._summary.setText(message)
        self._tick.stop()

    def set_stages(self, stages: Iterable[dict] | None) -> None:
        now = time.time()
        order: list[str] = []
        done_count = 0
        running_count = 0
        for st in stages or []:
            sid = str(st.get("id", "") or f"etapa{len(order)}")
            label = str(st.get("label", sid))
            done = bool(st.get("done", False))
            percent = float(st.get("percent", 0.0) or 0.0)
            note = str(st.get("note", "") or "")

            clock = self._clock.setdefault(sid, {})
            if done:
                clock.setdefault("start", now)
                clock.setdefault("end", now)
                done_count += 1
            elif percent > 0.0:
                clock.setdefault("start", now)
                running_count += 1

            row = self._rows.get(sid)
            if row is None:
                row = _StageRow(sid, label, self._rows_box)
                row.clicked.connect(self.stageClicked)
                self._rows[sid] = row
                self._rows_lay.addWidget(row)
            row.setVisible(True)
            row.update_state(done=done, percent=percent, note=note, label=label)
            row.set_elapsed(self._elapsed(sid, now), running=not done)
            order.append(sid)

        self._order = order
        for sid, row in self._rows.items():
            if sid not in order:
                row.setVisible(False)
                self.set_spinner_off(row)

        total = len(order)
        if total:
            parts = [t("{done} de {total} etapas concluídas").format(done=done_count, total=total)]
            if running_count:
                parts.append(f"{running_count} {t('etapa(s) em andamento')}")
            self._summary.setText(" · ".join(parts))
            if not self._tick.isActive():
                self._tick.start()
        else:
            self._summary.setText(t("Nenhuma etapa executada ainda."))
            self._tick.stop()

    @staticmethod
    def set_spinner_off(row: _StageRow) -> None:
        row.set_spinner_visible(False)

    def _elapsed(self, sid: str, now: float) -> float | None:
        clock = self._clock.get(sid) or {}
        start = clock.get("start")
        if not start:
            return None
        end = clock.get("end") or now
        return max(0.0, end - start)

    def _retime(self) -> None:
        now = time.time()
        for sid in self._order:
            row = self._rows.get(sid)
            if row is None or not row.isVisible():
                continue
            row.set_elapsed(self._elapsed(sid, now), running="end" not in (self._clock.get(sid) or {}))

    def reset_clocks(self) -> None:
        """Esquece os tempos medidos (nova análise)."""
        self._clock.clear()

    def rows(self) -> list[_StageRow]:
        return [self._rows[s] for s in self._order if s in self._rows]

    def sizeHint(self) -> QSize:
        return QSize(340, 27 * max(1, len(self._order)) + 40)
