"""``SegmentedControl`` — seletor de opções exclusivas com indicador deslizante.

Substitui uma linha de rádios quando as alternativas são curtas e mutuamente
exclusivas (duração do corte, aspecto, modo de destaque). O indicador é um
``QFrame`` filho reposicionado com ``QPropertyAnimation`` sobre ``geometry``.
"""

from __future__ import annotations

from typing import Iterable, Sequence

from PySide6.QtCore import QEasingCurve, QObject, QPropertyAnimation, QRect, QSize, Qt, Signal
from PySide6.QtWidgets import QFrame, QHBoxLayout, QPushButton, QWidget

from .. import theme
from ...i18n import t


class SegmentedControl(QFrame):
    """Barra de botões exclusivos; o valor atual é uma chave ``str``."""

    changed = Signal(str)

    def __init__(self, options: Sequence[tuple[str, str] | str] | Iterable[str] = (),
                 value: str = "", parent: QWidget | None = None,
                 size: str = "md") -> None:
        super().__init__(parent)
        self.setProperty("segmented", "true")
        self.setAccessibleName(t("Seletor de opção"))
        self._values: list[str] = []
        self._buttons: list[QPushButton] = []
        self._value = ""
        self._size = size
        self._blocked = False

        self._thumb = QFrame(self)
        self._thumb.setProperty("segmentThumb", "true")
        self._thumb.setObjectName("segmentThumb")
        self._thumb.lower()
        self._thumb_anim = QPropertyAnimation(self._thumb, b"geometry", self)
        self._thumb_anim.setDuration(190)
        self._thumb_anim.setEasingCurve(QEasingCurve.Type.OutQuad)

        self._row = QHBoxLayout(self)
        h = 4 if size == "sm" else 5
        self._row.setContentsMargins(h, h, h, h)
        self._row.setSpacing(2)

        for opt in options:
            if isinstance(opt, tuple):
                key, label = opt[0], opt[1]
            else:
                key = label = str(opt)
            self._add_option(str(key), label)
        if self._values:
            self._value = value if value in self._values else self._values[0]
        self._sync(active=True)

    # ------------------------------------------------------------- montagem
    def _add_option(self, key: str, label: str) -> None:
        btn = QPushButton(t(label), self)
        btn.setProperty("segment", "true")
        btn.setProperty("active", "false")
        btn.setCheckable(False)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        btn.setAccessibleName(t(label))
        btn.clicked.connect(lambda _=False, k=key: self._on_click(k))
        self._row.addWidget(btn)
        self._values.append(key)
        self._buttons.append(btn)

    def set_options(self, options: Sequence[tuple[str, str] | str] | Iterable[str]) -> None:
        """Troca as alternativas mantendo o valor quando ele ainda existir."""
        for btn in self._buttons:
            self._row.removeWidget(btn)
            btn.deleteLater()
        self._buttons.clear()
        self._values.clear()
        for opt in options:
            key, label = (opt[0], opt[1]) if isinstance(opt, tuple) else (str(opt), str(opt))
            self._add_option(str(key), str(label))
        if self._values and self._value not in self._values:
            self._value = self._values[0]
        self._sync(active=True)

    def add_option(self, key: str, label: str) -> None:
        self._add_option(str(key), str(label))
        self._sync(active=True)

    def setToolTipFor(self, key: str, tip: str) -> None:
        if key in self._values:
            self._buttons[self._values.index(key)].setToolTip(tip)

    # ---------------------------------------------------------------- estado
    def current(self) -> str:
        return self._value

    def currentValue(self) -> str:
        return self._value

    def values(self) -> list[str]:
        return list(self._values)

    def labels(self) -> list[str]:
        return [b.text() for b in self._buttons]

    def setValue(self, value: str, notify: bool = False) -> None:
        """Seleciona ``value``; ``notify`` emite ``changed`` mesmo sem clique."""
        if value not in self._values:
            return
        if value == self._value:
            self._sync(active=True)
            return
        self._value = value
        self._sync(active=True, animate=True)
        if notify and not self._blocked:
            self.changed.emit(value)

    def set_value(self, value: str, notify: bool = False) -> None:
        self.setValue(value, notify)

    def set_enabled_value(self, key: str, enabled: bool) -> None:
        if key in self._values:
            self._buttons[self._values.index(key)].setEnabled(enabled)

    def blockSignals(self, block: bool = True) -> None:  # noqa: D102
        self._blocked = bool(block)
        super().blockSignals(block)

    # --------------------------------------------------------------- eventos
    def _on_click(self, key: str) -> None:
        if self._blocked or key == self._value:
            return
        self._value = key
        self._sync(active=True, animate=True)
        self.changed.emit(key)

    def _sync(self, active: bool = False, animate: bool = False) -> None:
        for i, btn in enumerate(self._buttons):
            is_active = self._values[i] == self._value
            btn.setProperty("active", "true" if (is_active and active) else "false")
            btn.style().unpolish(btn)
            btn.style().polish(btn)
            btn.update()
        self._move_thumb(animate)

    def _move_thumb(self, animate: bool = False) -> None:
        if not self._buttons:
            self._thumb.hide()
            return
        idx = self._values.index(self._value) if self._value in self._values else -1
        if idx < 0:
            self._thumb.hide()
            return
        btn = self._buttons[idx]
        geom = QRect(btn.mapTo(self, btn.rect().topLeft()), btn.size())
        self._thumb.raise_()
        if animate and self._thumb.isVisible():
            self._thumb_anim.stop()
            self._thumb_anim.setStartValue(self._thumb.geometry())
            self._thumb_anim.setEndValue(geom)
            self._thumb_anim.start()
        else:
            self._thumb_anim.stop()
            self._thumb.setGeometry(geom)
        self._thumb.show()

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._move_thumb(animate=False)

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self._move_thumb(animate=False)

    # --------------------------------------------------------------- tamanho
    def minimumSizeHint(self) -> QSize:
        width = sum(b.sizeHint().width() for b in self._buttons) + 4 * max(1, len(self._buttons))
        height = max((b.sizeHint().height() for b in self._buttons), default=28) + 12
        return QSize(max(120, width), height)
