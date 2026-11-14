"""Área de arrastar-e-soltar aceita por :mod:`corteia.core.media`.

Dois tipos de entrada, cada um com seu sinal:

* arquivos de vídeo (e áudio, para a validação explicar que não serve) →
  :attr:`filesDropped`
* texto com ``http(s)://`` (link) → :attr:`urlDropped`

O clique no botão central emite :attr:`browseRequested`, e é a página que
abre o diálogo — assim o widget continua independente de ``QFileDialog``.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QMimeData, QSize, Qt, Signal
from PySide6.QtGui import QDragEnterEvent, QDragLeaveEvent, QDropEvent
from PySide6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QPushButton,
                               QSizePolicy, QVBoxLayout, QWidget)

from .. import theme
from ...i18n import t

try:  # o núcleo pode estar sendo reconstruído; o widget segue funcionando
    from ...core.media import SUPPORTED_EXTS
except Exception:  # noqa: BLE001
    SUPPORTED_EXTS = (".mp4", ".mov", ".mkv", ".webm", ".avi")


class DropArea(QFrame):
    """Zona de soltura com feedback visual real (borda sólida durante o arrasto)."""

    filesDropped = Signal(list)        # list[str]
    urlDropped = Signal(str)
    browseRequested = Signal()

    def __init__(self, parent: QWidget | None = None, *, height: int = 160) -> None:
        super().__init__(parent)
        self.setProperty("droparea", "true")
        self.setAcceptDrops(True)
        self.setMinimumHeight(height)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 18, 20, 18)
        lay.setSpacing(6)

        self.icon = QLabel(self)
        self.icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.icon.setPixmap(theme.pixmap("import", "accent", 34))
        lay.addWidget(self.icon)

        self.title = QLabel(t("Arraste vídeos para cá"), self)
        self.title.setProperty("emptyTitle", "true")
        self.title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(self.title)

        self.msg = QLabel(self._formats_text(), self)
        self.msg.setProperty("emptyMsg", "true")
        self.msg.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.msg.setWordWrap(True)
        lay.addWidget(self.msg)

        row = QHBoxLayout()
        row.setSpacing(8)
        row.addStretch(1)
        self.btn_browse = QPushButton(t("Escolher arquivo"), self)
        self.btn_browse.setProperty("variant", "outline")
        self.btn_browse.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_browse.setIcon(theme.icon("folder", "muted", 18))
        self.btn_browse.clicked.connect(self.browseRequested)
        row.addWidget(self.btn_browse)
        row.addStretch(1)
        lay.addLayout(row)

    # ------------------------------------------------------------------- API
    def _formats_text(self) -> str:
        exts = ", ".join(e.lstrip(".").upper() for e in SUPPORTED_EXTS[:8])
        return t("ou cole um link. Formatos aceitos: {exts}").format(exts=exts)

    def set_status(self, text: str, token: str = "muted") -> None:
        """Mostra o resultado da validação logo abaixo da área."""
        self.msg.setText(text)
        self.msg.setProperty("role", token if token != "muted" else "muted")
        self.msg.style().unpolish(self.msg)
        self.msg.style().polish(self.msg)

    def retranslate(self) -> None:
        self.title.setText(t("Arraste vídeos para cá"))
        self.msg.setText(self._formats_text())
        self.btn_browse.setText(t("Escolher arquivo"))

    def sizeHint(self) -> QSize:
        return QSize(520, 170)

    # ------------------------------------------------------------- arrasto
    def _parse(self, mime: QMimeData) -> tuple[list[str], str]:
        files: list[str] = []
        if mime.hasUrls():
            for url in mime.urls():
                if url.isLocalFile():
                    files.append(str(url.toLocalFile()))
                elif url.scheme().startswith("http"):
                    return [], url.toString()
        text = mime.text().strip() if mime.hasText() else ""
        if not files and text.startswith(("http://", "https://")):
            return [], text
        if text and not files and ("." in text or "/" in text):
            for chunk in text.replace("\n", ";").split(";"):
                chunk = chunk.strip().strip('"')
                if chunk:
                    files.append(chunk)
        return files, ""

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:  # noqa: N802
        files, url = self._parse(event.mimeData())
        if files or url:
            event.acceptProposedAction()
            self._set_dragging(True)
        else:
            event.ignore()

    def dragMoveEvent(self, event) -> None:  # noqa: N802
        if event.proposedAction():
            event.acceptProposedAction()

    def dragLeaveEvent(self, event: QDragLeaveEvent) -> None:  # noqa: N802
        self._set_dragging(False)
        super().dragLeaveEvent(event)

    def dropEvent(self, event: QDropEvent) -> None:  # noqa: N802
        self._set_dragging(False)
        files, url = self._parse(event.mimeData())
        if url:
            event.acceptProposedAction()
            self.urlDropped.emit(url)
            return
        valid = [f for f in files if Path(f).is_file()]
        if valid:
            event.acceptProposedAction()
            self.filesDropped.emit(valid)
        else:
            event.ignore()

    def _set_dragging(self, on: bool) -> None:
        self.setProperty("dragging", "true" if on else "")
        self.style().unpolish(self)
        self.style().polish(self)
