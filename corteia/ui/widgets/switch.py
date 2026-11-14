"""``SwitchButton`` — interruptor animado no lugar de um checkbox.

Usado em todas as opções booleanas (Configurações, Importar). Mantém o contrato
de ``QCheckBox`` (``isChecked``, ``setChecked``, ``toggled``) mas desenhado com
as cores do tema e animação de 180 ms no botão.
"""

from __future__ import annotations

from PySide6.QtCore import QEasingCurve, Property, QPropertyAnimation, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QWidget

from .. import theme


class SwitchButton(QWidget):
    """Interruptor on/off com trilha e cursor que desliza."""

    #: disparado por interação do usuário (ou quando ``notify=True``)
    checkedChanged = Signal(bool)
    toggled = Signal(bool)

    TRACK_W = 42
    TRACK_H = 22

    def __init__(self, text: str = "", checked: bool = False,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._text = text
        self._checked = bool(checked)
        self._knob = 1.0 if self._checked else 0.0
        self._hover = False
        self._use_success = False
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAccessibleName(text or "Interruptor")
        self.setAccessibleDescription("Pressione Espaço para ligar ou desligar")
        self._anim = QPropertyAnimation(self, b"knob", self)
        self._anim.setDuration(180)
        self._anim.setEasingCurve(QEasingCurve.Type.OutQuad)

    # ---------------------------------------------------------------- estado
    def isChecked(self) -> bool:  # compatível com QCheckBox
        return self._checked

    def checked(self) -> bool:
        return self._checked

    def setChecked(self, value: bool, notify: bool = False) -> None:
        """Define o estado; ``notify`` controla se o sinal é disparado."""
        value = bool(value)
        if value == self._checked:
            self._anim.stop()
            self._knob = 1.0 if value else 0.0
            self.update()
            return
        self._checked = value
        self._anim.stop()
        self._anim.setStartValue(self._knob)
        self._anim.setEndValue(1.0 if value else 0.0)
        self._anim.start()
        self.update()
        if notify:
            self.checkedChanged.emit(value)
            self.toggled.emit(value)

    #: forma explícita usada pelas páginas para carregar valores sem eco
    def set_checked(self, value: bool, notify: bool = False) -> None:
        self.setChecked(value, notify)

    def text(self) -> str:
        return self._text

    def setText(self, text: str) -> None:
        self._text = text or ""
        self.setAccessibleName(self._text or "Interruptor")
        self.updateGeometry()
        self.update()

    # ------------------------------------------------------------- animação
    def _get_knob(self) -> float:
        return self._knob

    def _set_knob(self, value: float) -> None:
        self._knob = float(value)
        self.update()

    knob = Property(float, _get_knob, _set_knob)

    # --------------------------------------------------------------- tamanho
    def sizeHint(self) -> QSize:
        text_w = self.fontMetrics().horizontalAdvance(self._text) if self._text else 0
        height = max(self.TRACK_H, int(self.font().pointSize() * 1.9))
        return QSize(self.TRACK_W + (10 + text_w if text_w else 0) + 4, height)

    def minimumSizeHint(self) -> QSize:
        return self.sizeHint()

    # ------------------------------------------------------------------ pinta
    def paintEvent(self, event) -> None:  # noqa: N802 - nome da API Qt
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        h = self.height()
        y = (h - self.TRACK_H) / 2.0
        enabled = self.isEnabled()
        accent = QColor(theme.color("accent"))
        on_bg = QColor(theme.color("success")) if self._use_success else accent
        off_bg = QColor(theme.color("surface3"))
        for c in (accent, on_bg, off_bg):
            if not enabled:
                c.setAlpha(90)
        border = QColor(theme.color("border_strong"))
        if self.hasFocus():
            border = accent
        elif self._hover:
            border = QColor(theme.color("muted"))

        p.setPen(QPen(border, 1.0))
        p.setBrush(on_bg if self._checked else off_bg)
        radius = (self.TRACK_H - 2.0) / 2.0
        p.drawRoundedRect(QRectF(1.0, y, self.TRACK_W - 2.0, self.TRACK_H - 2.0),
                          radius, radius)

        r = (self.TRACK_H - 8.0) / 2.0
        cx = 4.0 + r + self._knob * (self.TRACK_W - 8.0 - 2 * r)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(theme.color("on_accent")) if self._checked
                   else QColor(theme.color("muted")))
        p.drawEllipse(QRectF(cx - r, y + 4.0, 2 * r, 2 * r))

        if self._text:
            p.setPen(QPen(QColor(theme.color("text") if enabled else theme.color("faint"))))
            p.drawText(QRectF(self.TRACK_W + 9, 0, max(0.0, self.width() - self.TRACK_W - 9), h),
                       Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, self._text)
        p.end()

    def enterEvent(self, event) -> None:  # noqa: N802
        self._hover = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802
        self._hover = False
        self.update()
        super().leaveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton and self.isEnabled():
            self.setChecked(not self._checked, notify=True)
            event.accept()
        else:
            super().mouseReleaseEvent(event)

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.key() in (Qt.Key.Key_Space, Qt.Key.Key_Return, Qt.Key.Key_Enter) and self.isEnabled():
            self.setChecked(not self._checked, notify=True)
            event.accept()
        else:
            super().keyPressEvent(event)

    def focusInEvent(self, event) -> None:  # noqa: N802
        super().focusInEvent(event)
        self.update()

    def focusOutEvent(self, event) -> None:  # noqa: N802
        super().focusOutEvent(event)
        self.update()

    # -------------------------------------------------------------- variantes
    def set_success_style(self, value: bool) -> None:
        """Trilha verde quando o switch afirma algo (ex.: "legenda queimada")."""
        self._use_success = bool(value)
        self.update()
