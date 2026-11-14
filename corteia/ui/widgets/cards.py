"""Cards reutilizáveis da interface (Painel, Importar, Cortes, Histórico).

Tudo aqui é apresentação: nenhum número é calculado "no chute" — os valores
vêm prontos de quem monta o card (:meth:`StatCard.set_value`,
:class:`ProjectCard` lendo ``corteia.project.Recent``, :class:`HistoryRow`
lendo as linhas de ``ctx.store.export_history()``).

Os cards usam os ganchos de estilo já definidos nos QSS
(``QFrame[card="..."]``, ``QLabel[pill="..."]``, ``QLabel[role="..."]``) —
por isso não há ``setStyleSheet`` com cor solta aqui dentro.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from PySide6.QtCore import QEvent, QSize, Qt, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QMenu,
                               QProgressBar, QPushButton, QSizePolicy,
                               QVBoxLayout, QWidget)

from .. import theme
from ...i18n import t
from ...models import Clip, format_ts


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _thumb_pixmap(path: str, size: tuple[int, int]) -> QPixmap:
    """Miniatura **real** do disco; se não existir, bloco honesto sem imagem."""
    pm = theme.pixmap_from_file(path, size, radius=8)
    if pm is not None:
        return pm
    return theme.placeholder_pixmap(size, "video", t("sem miniatura"))


# --------------------------------------------------------------------------- #
# StatCard
# --------------------------------------------------------------------------- #
class StatCard(QFrame):
    """Um número e seu rótulo. ``set_value`` aceita texto já formatado."""

    clicked = Signal()

    def __init__(self, label: str = "", value: str = "0", icon: str = "chart",
                 hint: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setProperty("card", "stat")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMinimumWidth(150)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(14, 12, 14, 12)
        lay.setSpacing(12)

        self.icon_widget = QLabel(self)
        self.icon_widget.setFixedSize(34, 34)
        self.icon_widget.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.icon_widget.setPixmap(theme.pixmap(icon, "accent", 22))
        lay.addWidget(self.icon_widget, 0, Qt.AlignmentFlag.AlignTop)

        col = QVBoxLayout()
        col.setSpacing(1)
        self.value_label = QLabel(str(value), self)
        self.value_label.setProperty("role", "value")
        self.value_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.caption = QLabel(label, self)
        self.caption.setProperty("role", "muted")
        self.hint_label = QLabel(hint, self)
        self.hint_label.setProperty("role", "faint")
        self.hint_label.setVisible(bool(hint))
        col.addWidget(self.value_label)
        col.addWidget(self.caption)
        col.addWidget(self.hint_label)
        lay.addLayout(col, 1)

        self._icon_name = icon
        self.set_label(label)

    # ------------------------------------------------------------------- API
    def set_value(self, value: Any, hint: str = "") -> None:
        self.value_label.setText(str(value))
        if hint:
            self.set_hint(hint)

    def set_label(self, label: str) -> None:
        self.caption.setText(label)
        self.caption.setToolTip(label)

    def set_hint(self, hint: str) -> None:
        self.hint_label.setText(hint)
        self.hint_label.setVisible(bool(hint))

    def set_icon(self, name: str, token: str = "accent") -> None:
        self._icon_name = name
        self.icon_widget.setPixmap(theme.pixmap(name, token, 22))

    def retranslate(self) -> None:  # chamado pelas páginas ao trocar idioma
        pass

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)


# --------------------------------------------------------------------------- #
# ProjectCard
# --------------------------------------------------------------------------- #
class ProjectCard(QFrame):
    """Projeto recente: miniatura real + contadores vindos de ``Recent``."""

    openRequested = Signal(str)          # caminho do .json
    showRequested = Signal(str)          # mesmo caminho, abre a pasta

    def __init__(self, recent: Any = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.recent = recent
        self.setProperty("card", "hover")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMinimumHeight(94)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 10, 12, 10)
        lay.setSpacing(12)

        self.thumb = QLabel(self)
        self.thumb.setFixedSize(140, 79)       # 16:9
        self.thumb.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(self.thumb, 0, Qt.AlignmentFlag.AlignVCenter)

        col = QVBoxLayout()
        col.setSpacing(2)
        self.title = QLabel("", self)
        self.title.setProperty("role", "page")
        self.title.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.subtitle = QLabel("", self)
        self.subtitle.setProperty("role", "muted")
        self.subtitle.setWordWrap(True)
        self.meta = QLabel("", self)
        self.meta.setProperty("role", "mono")
        col.addWidget(self.title)
        col.addWidget(self.subtitle)
        col.addWidget(self.meta)
        lay.addLayout(col, 1)

        self.actions = QVBoxLayout()
        self.actions.setSpacing(6)
        self.btn_open = QPushButton(t("Abrir"), self)
        self.btn_open.setProperty("variant", "primary")
        self.btn_open.setProperty("size", "sm")
        self.btn_open.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_open.clicked.connect(lambda: self.openRequested.emit(self.path()))
        self.btn_menu = QPushButton("", self)
        self.btn_menu.setProperty("variant", "ghost")
        self.btn_menu.setProperty("size", "sm")
        self.btn_menu.setIcon(theme.icon("chevron_down", "muted", 16))
        self.btn_menu.setFixedWidth(30)
        self.btn_menu.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_menu.clicked.connect(self._context_menu)
        self.actions.addWidget(self.btn_open)
        self.actions.addWidget(self.btn_menu)
        lay.addLayout(self.actions, 0)

        if recent is not None:
            self.set_recent(recent)

    # ------------------------------------------------------------------- API
    def path(self) -> str:
        return str(getattr(self.recent, "path", "") or "")

    def set_recent(self, recent: Any) -> None:
        self.recent = recent
        duration = float(getattr(recent, "duration", 0.0) or 0.0)
        self.title.setText(str(getattr(recent, "name", "") or t("Sem título")))
        self.subtitle.setText(t("{videos} vídeo(s) · {clips} corte(s) · {exports} exportado(s)").format(
            videos=int(getattr(recent, "sources", 0) or 0),
            clips=int(getattr(recent, "clips", 0) or 0),
            exports=int(getattr(recent, "exported", 0) or 0),
        ))
        self.meta.setText(f"{theme.format_duration_long(duration)} · "
                          f"{theme.relative_time(float(getattr(recent, 'updated_at', 0.0) or 0.0))}")
        self.apply_thumb(str(getattr(recent, "thumb", "") or ""))

    def is_alive(self) -> bool:
        """True enquanto o objeto C++ existe (miniaturas chegam por job)."""
        try:
            from shiboken6 import isValid

            return bool(isValid(self))
        except Exception:  # noqa: BLE001
            return True

    def apply_thumb(self, thumb_path: str) -> None:
        """Usa a miniatura real se o arquivo existe; senão, diz que não há.

        O painel regenera miniaturas em thread de trabalho: quando o resultado
        chega, o card pode já ter sido destruído por um refresh — ignorar é o
        comportamento correto (nenhum dado se perde).
        """
        try:
            self.thumb.setPixmap(_thumb_pixmap(thumb_path, (140, 79)))
        except RuntimeError:
            return
        self.thumb.setToolTip(t("Miniatura real do vídeo") if thumb_path and Path(thumb_path).is_file()
                              else t("Nenhuma miniatura gerada ainda"))

    def retranslate(self) -> None:
        self.btn_open.setText(t("Abrir"))
        if self.recent is not None:
            self.set_recent(self.recent)

    # ------------------------------------------------------------- interações
    def _context_menu(self) -> None:
        menu = QMenu(self)
        act_open = menu.addAction(t("Abrir projeto"))
        act_folder = menu.addAction(t("Abrir pasta do projeto"))
        act = menu.exec(self.btn_menu.mapToGlobal(self.btn_menu.rect().bottomLeft()))
        if act is act_folder:
            self.showRequested.emit(self.path())
        elif act is act_open:
            self.openRequested.emit(self.path())

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802
        self.openRequested.emit(self.path())
        super().mouseDoubleClickEvent(event)

    def contextMenuEvent(self, event) -> None:  # noqa: N802
        menu = QMenu(self)
        act_open = menu.addAction(t("Abrir projeto"))
        act_folder = menu.addAction(t("Abrir pasta do projeto"))
        act = menu.exec(event.globalPos())
        if act is act_folder:
            self.showRequested.emit(self.path())
        elif act is act_open:
            self.openRequested.emit(self.path())


# --------------------------------------------------------------------------- #
# EmptyState
# --------------------------------------------------------------------------- #
class EmptyState(QFrame):
    """Estado vazio honesto: diz o que falta e oferece a única saída possível."""

    actionRequested = Signal()

    def __init__(self, title: str = "", message: str = "", icon: str = "video",
                 action_text: str = "", parent: QWidget | None = None) -> None:
        # Older call sites passed the parent where the button label belongs.
        # A state widget must never be the reason a page fails to open.
        if not isinstance(action_text, str):
            parent = action_text if isinstance(action_text, QWidget) else parent
            action_text = ""
        if not isinstance(icon, str):
            icon = "video"
        super().__init__(parent)
        self.setProperty("empty", "true")
        self.setMinimumHeight(150)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 26, 24, 26)
        lay.setSpacing(8)

        self.icon_label = QLabel(self)
        self.icon_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.icon_label.setPixmap(theme.pixmap(icon, "faint", 40))
        lay.addWidget(self.icon_label)

        self.title_label = QLabel(title, self)
        self.title_label.setProperty("emptyTitle", "true")
        self.title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(self.title_label)

        self.msg_label = QLabel(message, self)
        self.msg_label.setProperty("emptyMsg", "true")
        self.msg_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.msg_label.setWordWrap(True)
        lay.addWidget(self.msg_label)

        self.btn = QPushButton("", self)
        self.btn.setProperty("variant", "outline")
        self.btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn.clicked.connect(self.actionRequested)
        self.set_action(action_text)
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(self.btn)
        row.addStretch(1)
        lay.addLayout(row)

    def set_text(self, title: str, message: str = "") -> None:
        self.title_label.setText(title)
        self.msg_label.setText(message)

    def set_action(self, text: str, slot: Callable[[], None] | None = None) -> None:
        if not isinstance(text, str):
            return
        self.btn.setText(text)
        self.btn.setVisible(bool(text))
        if slot is not None:
            try:
                self.btn.clicked.disconnect()
            except (RuntimeError, TypeError):
                pass
            self.btn.clicked.connect(slot)

    def retranslate(self) -> None:
        pass


# --------------------------------------------------------------------------- #
# HistoryRow
# --------------------------------------------------------------------------- #
class HistoryRow(QFrame):
    """Linha de exportação: estado, tamanho real do arquivo e erro quando houver."""

    openRequested = Signal(str)

    def __init__(self, row: dict | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setProperty("history", "true")
        self.row = row or {}
        self._path = ""

        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 8, 10, 8)
        lay.setSpacing(10)

        self.icon = QLabel(self)
        self.icon.setFixedWidth(22)
        self.icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(self.icon)

        col = QVBoxLayout()
        col.setSpacing(0)
        self.title = QLabel("", self)
        self.title.setProperty("role", "sub")
        self.detail = QLabel("", self)
        self.detail.setProperty("role", "mono")
        col.addWidget(self.title)
        col.addWidget(self.detail)
        lay.addLayout(col, 1)

        self.state = QLabel("", self)
        self.state.setProperty("pill", "idle")
        self.state.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(self.state, 0, Qt.AlignmentFlag.AlignVCenter)

        self.btn = QPushButton(t("Abrir"), self)
        self.btn.setProperty("variant", "ghost")
        self.btn.setProperty("size", "sm")
        self.btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn.clicked.connect(self._open)
        lay.addWidget(self.btn, 0, Qt.AlignmentFlag.AlignVCenter)

        if row:
            self.set_row(row)

    # ------------------------------------------------------------------- API
    def set_row(self, row: dict) -> None:
        self.row = row or {}
        state = str(self.row.get("state", "done") or "done")
        self._path = str(self.row.get("path", "") or "")
        name = Path(self._path).name if self._path else str(self.row.get("clip_id", "") or "")
        project = str(self.row.get("project", "") or "")
        self.title.setText(name or t("Exportação"))
        bits: list[str] = []
        if project:
            bits.append(project)
        when = float(self.row.get("finished_at", 0.0) or 0.0)
        if when:
            bits.append(theme.date_time(when))
        secs = float(self.row.get("seconds", 0.0) or 0.0)
        if secs:
            bits.append(t("levou {dur}").format(dur=theme.format_duration_long(secs)))
        if self._path and Path(self._path).is_file():
            try:
                from ... import paths as _paths
                bits.append(_paths.human_bytes(Path(self._path).stat().st_size))
            except OSError:
                pass
        self.detail.setText(" · ".join(bits))

        token, pill = "success", "ok"
        if state in ("failed", "error"):
            token, pill = "error", "error"
        elif state == "cancelled":
            token, pill = "warn", "warn"
        elif state in ("running", "pending"):
            token, pill = "accent", "busy"
        self.icon.setPixmap(theme.pixmap(
            "check" if pill == "ok" else "close" if pill == "error"
            else "clock" if pill == "busy" else "warning", token, 18))
        self.state.setProperty("pill", pill)
        self.state.setText(t({"ok": "concluído", "error": "falhou", "busy": "em andamento",
                              "warn": "cancelado"}.get(pill, pill)))
        self._repolish(self.state)
        error = str(self.row.get("error", "") or "")
        tip = error or self._path or name
        self.setToolTip(tip)
        self.btn.setEnabled(bool(self._path) and Path(self._path).is_file())
        self.btn.setText(t("Abrir"))
        self.btn.setVisible(self.state.property("pill") == "ok")

    @staticmethod
    def _repolish(widget: QWidget) -> None:
        widget.style().unpolish(widget)
        widget.style().polish(widget)
        widget.update()

    def retranslate(self) -> None:
        if self.row:
            self.set_row(self.row)

    def _open(self) -> None:
        if self._path:
            self.openRequested.emit(self._path)

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802
        self._open()
        super().mouseDoubleClickEvent(event)


# --------------------------------------------------------------------------- #
# ClipCard
# --------------------------------------------------------------------------- #
class ClipCard(QFrame):
    """Corte sugerido: miniatura real, tempo original, score 0–100 e ações."""

    approved = Signal(str)
    rejected = Signal(str)
    edited = Signal(str)
    exported = Signal(str)
    selected = Signal(str)
    previewRequested = Signal(str)

    def __init__(self, clip: Clip | None = None, thumb_path: str = "",
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.clip = clip
        self.setProperty("card", "true")
        self.setMinimumHeight(112)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 10, 12, 10)
        lay.setSpacing(12)

        self.thumb = QLabel(self)
        self.thumb.setFixedSize(90, 160)       # 9:16
        self.thumb.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(self.thumb, 0, Qt.AlignmentFlag.AlignVCenter)

        col = QVBoxLayout()
        col.setSpacing(3)
        self.title = QLabel("", self)
        self.title.setProperty("role", "page")
        self.title.setWordWrap(True)
        self.times = QLabel("", self)
        self.times.setProperty("role", "mono")
        self.desc = QLabel("", self)
        self.desc.setProperty("role", "muted")
        self.desc.setWordWrap(True)
        self.desc.setMaximumHeight(52)
        col.addWidget(self.title)
        col.addWidget(self.times)
        col.addWidget(self.desc)

        score_row = QHBoxLayout()
        score_row.setSpacing(8)
        self.score_caption = QLabel("", self)
        self.score_caption.setProperty("role", "faint")
        self.score_bar = QProgressBar(self)
        self.score_bar.setRange(0, 100)
        self.score_bar.setTextVisible(False)
        self.score_bar.setFixedHeight(6)
        self.score_bar.setProperty("mini", "true")
        self.score_value = QLabel("", self)
        self.score_value.setProperty("role", "mono")
        self.score_value.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        score_row.addWidget(self.score_caption, 0)
        score_row.addWidget(self.score_bar, 1)
        score_row.addWidget(self.score_value, 0)
        col.addLayout(score_row)
        lay.addLayout(col, 1)

        self.actions = QVBoxLayout()
        self.actions.setSpacing(5)
        self.btn_ok = QPushButton(t("Aprovar"), self)
        self.btn_ok.setProperty("variant", "primary")
        self.btn_ok.setProperty("size", "sm")
        self.btn_no = QPushButton(t("Recusar"), self)
        self.btn_no.setProperty("variant", "danger")
        self.btn_no.setProperty("size", "sm")
        self.btn_edit = QPushButton(t("Editar"), self)
        self.btn_edit.setProperty("variant", "outline")
        self.btn_edit.setProperty("size", "sm")
        self.btn_exp = QPushButton(t("Exportar"), self)
        self.btn_exp.setProperty("variant", "outline")
        self.btn_exp.setProperty("size", "sm")
        for b in (self.btn_ok, self.btn_no, self.btn_edit, self.btn_exp):
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            self.actions.addWidget(b)
        lay.addLayout(self.actions, 0)

        self.btn_ok.clicked.connect(lambda: self.approved.emit(self.clip_id()))
        self.btn_no.clicked.connect(lambda: self.rejected.emit(self.clip_id()))
        self.btn_edit.clicked.connect(lambda: self.edited.emit(self.clip_id()))
        self.btn_exp.clicked.connect(lambda: self.exported.emit(self.clip_id()))

        if clip is not None:
            self.set_clip(clip, thumb_path)

    # ------------------------------------------------------------------- API
    def clip_id(self) -> str:
        return str(getattr(self.clip, "id", "") or "")

    def set_clip(self, clip: Clip, thumb_path: str = "") -> None:
        self.clip = clip
        if thumb_path:
            self.thumb.setPixmap(_thumb_pixmap(thumb_path, (90, 160)))
        elif getattr(clip, "exported_path", "") and Path(clip.exported_path).is_file():
            self.thumb.setPixmap(_thumb_pixmap(clip.exported_path, (90, 160)))
        else:
            self.thumb.setPixmap(_thumb_pixmap("", (90, 160)))
        self.title.setText(clip.title or t("(sem título)"))
        self.times.setText(f"{format_ts(clip.start)} → {format_ts(clip.end)}"
                           f" · {t('recorte de {dur}').format(dur=theme.format_duration_long(clip.duration))}")
        self.desc.setText(clip.description or clip.reason or "")
        total = float(clip.score.total or 0.0)
        self.score_bar.setValue(int(max(0.0, min(100.0, total))))
        self.score_value.setText(f"{total:.0f}")
        self.score_caption.setText(t("score"))
        signals = clip.score.signals or {}
        weights = clip.score.weights or {}
        if signals:
            lines = [t("Como o score foi calculado")]
            for key, value in sorted(signals.items(), key=lambda kv: -abs(kv[1])):
                w = float(weights.get(key, 0.0) or 0.0)
                lines.append(f"· {key}: {value:.2f}" + (f" (peso {w:.2f})" if w else ""))
            self.score_bar.setToolTip("\n".join(lines))
            self.score_value.setToolTip(self.score_bar.toolTip())
        status = str(getattr(clip.status, "value", clip.status) or "suggested")
        pill = {"approved": "ok", "exported": "ok", "rejected": "error"}.get(status, "idle")
        self.setProperty("status", status)
        self._repolish(self)
        self.setToolTip(self.title.text() + (f"\n{self.times.text()}" if self.times.text() else ""))
        self.btn_exp.setEnabled(bool(clip.exported_path == "") or True)
        self.retranslate(status=status, pill=pill)

    def retranslate(self, status: str = "", pill: str = "") -> None:
        for btn, key in ((self.btn_ok, "Aprovar"), (self.btn_no, "Recusar"),
                         (self.btn_edit, "Editar"), (self.btn_exp, "Exportar")):
            btn.setText(t(key))
        if status == "approved":
            self.btn_ok.setText(t("Aprovado"))
        elif status == "exported":
            self.btn_ok.setText(t("Exportado"))

    @staticmethod
    def _repolish(widget: QWidget) -> None:
        widget.style().unpolish(widget)
        widget.style().polish(widget)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self.selected.emit(self.clip_id())
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802
        self.previewRequested.emit(self.clip_id())
        super().mouseDoubleClickEvent(event)

    def sizeHint(self) -> QSize:
        return QSize(420, 176)
