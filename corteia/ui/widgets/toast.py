"""``ToastStack`` — avisos passageiros no canto inferior direito.

Cada aviso é um ``QFrame`` filho de uma camada transparente que cobre a janela:
entra com fade + slide (``QPropertyAnimation`` em opacidade e posição), empilha
os anteriores para cima e se auto-remove. Passar o mouse pausa o cronômetro para
dar tempo de ler. Níveis: ``info``, ``ok``, ``warn``, ``error``.
"""

from __future__ import annotations

from PySide6.QtCore import (QEasingCurve, QObject, QPoint, QPropertyAnimation, QSize,
                            Qt, QTimer, Signal)
from PySide6.QtWidgets import (QFrame, QGraphicsOpacityEffect, QHBoxLayout, QLabel,
                               QToolButton, QVBoxLayout, QWidget)

from .. import theme
from ...i18n import t

_LEVEL_ICON = {"info": "info", "ok": "check", "warn": "warning", "error": "close"}
_LEVEL_TOKEN = {"info": "info", "ok": "success", "warn": "warn", "error": "error"}
_DEFAULT_MS = {"info": 4200, "ok": 4200, "warn": 6500, "error": 9000}

WIDTH = 380
MARGIN = 14
GAP = 8


def _level_title(level: str) -> str:
    return {
        "info": t("Aviso"),
        "ok": t("Concluído"),
        "warn": t("Atenção"),
        "error": t("Erro"),
    }.get(level, t("Aviso"))


class _Toast(QFrame):
    """Um aviso. Vive pouco e morre animado."""

    dismissed = Signal(object)

    def __init__(self, message: str, level: str = "info", title: str = "",
                 timeout_ms: int | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        level = level if level in _LEVEL_ICON else "info"
        self._level = level
        self.setProperty("toast", "true")
        self.setProperty("level", level)
        self.setAccessibleName(_level_title(level))
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        self._effect = QGraphicsOpacityEffect(self)
        self._effect.setOpacity(0.0)
        self.setGraphicsEffect(self._effect)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 10, 6, 10)
        lay.setSpacing(10)

        self.icon = QLabel(self)
        self.icon.setFixedSize(20, 20)
        self.icon.setPixmap(theme.pixmap(_LEVEL_ICON[level], _LEVEL_TOKEN[level], 20))
        self.icon.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignCenter)

        col = QVBoxLayout()
        col.setSpacing(1)
        self.title = QLabel(title or _level_title(level), self)
        self.title.setProperty("toastTitle", "true")
        self.msg = QLabel(message, self)
        self.msg.setProperty("toastMsg", "true")
        self.msg.setWordWrap(True)
        self.msg.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        col.addWidget(self.title)
        col.addWidget(self.msg)

        self.close_btn = QToolButton(self)
        self.close_btn.setIcon(theme.icon("close", "muted", 16))
        self.close_btn.setAccessibleName(t("Fechar aviso"))
        self.close_btn.setToolTip(t("Fechar"))
        self.close_btn.setAutoRaise(True)
        self.close_btn.clicked.connect(self.dismiss)

        lay.addWidget(self.icon, 0, Qt.AlignmentFlag.AlignTop)
        lay.addLayout(col, 1)
        lay.addWidget(self.close_btn, 0, Qt.AlignmentFlag.AlignTop)

        self._timeout = int(timeout_ms if timeout_ms is not None else _DEFAULT_MS[level])
        self._remaining = self._timeout
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.dismiss)
        if self._timeout > 0:
            self._timer.start(self._timeout)

        self._pos_anim: QPropertyAnimation | None = None
        self._fade: QPropertyAnimation | None = None
        self._going = False

    # ------------------------------------------------------------------ tempo
    def enterEvent(self, event) -> None:  # noqa: N802
        if self._timer.isActive():
            self._remaining = max(800, self._timer.remainingTime())
            self._timer.stop()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802
        if not self._going and self._timeout > 0:
            self._timer.start(max(800, self._remaining))
        super().leaveEvent(event)

    # ------------------------------------------------------------------ mover
    def slide_to(self, pos: QPoint, animate: bool = True, duration: int = 200) -> None:
        if not animate:
            self.move(pos)
            return
        if self._pos_anim is None:
            self._pos_anim = QPropertyAnimation(self, b"pos", self)
            self._pos_anim.setEasingCurve(QEasingCurve.Type.OutQuad)
        self._pos_anim.stop()
        self._pos_anim.setDuration(duration)
        self._pos_anim.setStartValue(self.pos())
        self._pos_anim.setEndValue(pos)
        self._pos_anim.start()

    def fade_to(self, value: float, duration: int = 200, *, ease_in: bool = False) -> None:
        if self._fade is None:
            self._fade = QPropertyAnimation(self._effect, b"opacity", self)
        self._fade.stop()
        self._fade.setDuration(duration)
        self._fade.setEasingCurve(QEasingCurve.Type.InQuad if ease_in
                                  else QEasingCurve.Type.OutQuad)
        self._fade.setStartValue(self._effect.opacity())
        self._fade.setEndValue(value)
        self._fade.start()

    # ----------------------------------------------------------------- fechar
    def dismiss(self) -> None:
        if self._going:
            return
        self._going = True
        self._timer.stop()
        self.fade_to(0.0, 170, ease_in=True)
        self.slide_to(self.pos() + QPoint(0, 14), animate=True, duration=170)
        QTimer.singleShot(190, self._finish)

    def _finish(self) -> None:
        self.dismissed.emit(self)
        self.deleteLater()

    def is_going(self) -> bool:
        return self._going

    def level(self) -> str:
        return self._level

    def preferred_size(self) -> QSize:
        self.ensurePolished()
        self.layout().activate()
        return QSize(WIDTH, max(52, self.heightForWidth(WIDTH) or self.sizeHint().height()))


class ToastStack(QObject):
    """Camada de avisos ancorada no canto inferior direito de ``host``."""

    #: disparado quando um aviso sai de cena (inspeção/testes)
    toast_closed = Signal(str)

    def __init__(self, host: QWidget | None = None, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._layer: QWidget | None = None
        self._host: QWidget | None = None
        self._toasts: list[_Toast] = []          # 0 = mais recente (embaixo)
        self.max_visible = 4
        if host is not None:
            self.attach(host)

    # ---------------------------------------------------------------- anexar
    def attach(self, host: QWidget) -> None:
        """Prende a camada ao widget informado (o ``centralWidget`` da janela)."""
        if self._host is host and self._layer is not None:
            self._sync_layer()
            return
        self._host = host
        if self._layer is None:
            self._layer = QWidget(host)
            self._layer.setObjectName("toastLayer")
            self._layer.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        else:
            self._layer.setParent(host)
        host.installEventFilter(self)
        self._sync_layer()
        self.relayout(animate=False)

    def _sync_layer(self) -> None:
        if self._layer is not None and self._host is not None:
            self._layer.setGeometry(0, 0, self._host.width(), self._host.height())
            self._layer.show()
            self._layer.raise_()

    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        from PySide6.QtCore import QEvent

        if self._host is not None and obj is self._host:
            etype = event.type()
            if etype in (QEvent.Type.Resize, QEvent.Type.Show, QEvent.Type.LayoutRequest):
                self._sync_layer()
                self.relayout(animate=etype == QEvent.Type.Resize)
        return super().eventFilter(obj, event)

    # -------------------------------------------------------------------- API
    def show(self, message: str, level: str = "info", ms: int | None = None,
             title: str = "") -> _Toast | None:
        """Publica um aviso. ``ms=0`` deixa visível até o usuário fechar."""
        if self._layer is None:
            if self._host is None:
                return None
            self.attach(self._host)
        if self._layer is None:
            return None

        toast = _Toast(message, level=level, title=title, timeout_ms=ms, parent=self._layer)
        toast.dismissed.connect(self._on_dismissed)
        size = toast.preferred_size()
        toast.setFixedWidth(size.width())
        toast.resize(size.width(), size.height())

        self._toasts.insert(0, toast)
        while len(self._toasts) > self.max_visible:
            oldest = self._toasts.pop()
            oldest.dismiss()

        toast.move(self._position(0) + QPoint(0, 18))
        toast.show()
        toast.raise_()
        toast.fade_to(1.0, 200)
        self.relayout(animate=True)
        return toast

    def show_info(self, message: str, title: str = "") -> _Toast | None:
        return self.show(message, "info", title=title)

    def show_ok(self, message: str, title: str = "") -> _Toast | None:
        return self.show(message, "ok", title=title)

    def show_warn(self, message: str, title: str = "") -> _Toast | None:
        return self.show(message, "warn", title=title)

    def show_error(self, message: str, title: str = "") -> _Toast | None:
        return self.show(message, "error", title=title)

    def on_signal(self, message: str, level: str) -> None:
        """Conector direto de ``Signals.toast(str, str)``."""
        self.show(message, level)

    def clear(self) -> None:
        for toast in list(self._toasts):
            toast.dismiss()
        self._toasts.clear()

    def count(self) -> int:
        return len([toast for toast in self._toasts if not toast.is_going()])

    def visible_messages(self) -> list[str]:
        return [toast.msg.text() for toast in self._toasts if not toast.is_going()]

    # --------------------------------------------------------------- posição
    def _position(self, index: int) -> QPoint:
        """Slot do item ``index`` (0 = encostado no canto inferior direito)."""
        if self._layer is None:
            return QPoint(MARGIN, MARGIN)
        y = self._layer.height() - MARGIN
        for i in range(index + 1):
            y -= self._toasts[i].height()
            if i < index:
                y -= GAP
        x = self._layer.width() - WIDTH - MARGIN
        return QPoint(max(MARGIN, x), max(MARGIN, y))

    def relayout(self, animate: bool = True) -> None:
        for i, toast in enumerate(self._toasts):
            toast.slide_to(self._position(i), animate=animate)

    def _on_dismissed(self, toast: _Toast) -> None:
        if toast in self._toasts:
            self._toasts.remove(toast)
        self.toast_closed.emit(toast.msg.text())
        self.relayout(animate=True)
