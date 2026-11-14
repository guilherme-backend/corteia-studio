"""``SubtitleOverlay`` — prévia WYSIWYG desenhada com o mesmo texto do ``.ass``.

O widget pinta o quadro do vídeo (ou um fundo neutro) e, por cima, a legenda do
instante atual usando ``SubtitleSettings`` + as ``Cue`` produzidas por
``corteia.core.subtitles.cues``. Ou seja: **o texto que aparece aqui é o mesmo
que o FFmpeg vai queimar**, porque ambos vêm da mesma lista de cues.

Ele não importa o núcleo: a página chama ``SUB.cues(...)`` / ``SUB.build_ass``
e entrega os resultados por :meth:`set_cues`, mantém o caminho do ``.ass`` real
por :meth:`set_ass_path` (para prova visual/conferência) e avança o tempo com
:meth:`set_time` (tempo **do corte**, 0 = primeira faixa mantida).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Sequence

from PySide6.QtCore import QPoint, QRect, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import QSizePolicy, QWidget

from .. import theme
from ...i18n import t
from ...models import SubtitleSettings, SubtitleStyle

log = logging.getLogger("corteia.ui.overlay")

#: paleta do modo multicolor — idêntica a ``subtitles.VIRAL_PALETTE``
VIRAL_PALETTE = ("#FFE400", "#22E07A", "#35C8FF", "#FF5C7A", "#FFB020")
POSITION_ALIGN = {"bottom": Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignBottom,
                  "top": Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop,
                  "center": Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter}


class SubtitleOverlay(QWidget):
    """Moldura do recorte + legenda do instante corrente (palavra a palavra)."""

    #: clique em uma palavra pede edição daquele trecho (índice da palavra global)
    wordClicked = Signal(int, str)

    def __init__(self, ctx: Any = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self.setObjectName("subtitleOverlay")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMinimumSize(180, 220)
        self.setMouseTracking(True)
        self.setAccessibleName(t("Prévia das legendas"))

        self._settings = SubtitleSettings()
        self._cues: list[Any] = []
        self._out_size = (1080, 1920)
        self._frame: QPixmap | None = None
        self._time = 0.0
        self._ass_path = ""
        self._show_safe = True
        self._word_cache: list[tuple[QRect, str, str]] = []

    # ---------------------------------------------------------------------- API
    def set_settings(self, settings: SubtitleSettings | None) -> None:
        self._settings = settings or SubtitleSettings()
        self.update()

    def settings(self) -> SubtitleSettings:
        return self._settings

    def set_cues(self, cues: Sequence[Any] | None) -> None:
        """Cues vindas de ``SUB.cues`` (tempos relativos ao corte)."""
        self._cues = list(cues or [])
        self.update()

    def set_output_size(self, width: int, height: int) -> None:
        if width > 0 and height > 0:
            self._out_size = (int(width), int(height))
            self.update()

    def set_frame(self, pixmap: QPixmap | None) -> None:
        """Quadro real do vídeo (``media.thumbnail``) atrás da legenda."""
        self._frame = pixmap if (pixmap is not None and not pixmap.isNull()) else None
        self.update()

    def set_time(self, clip_seconds: float) -> None:
        self._time = max(0.0, float(clip_seconds or 0.0))
        self.update()

    def set_ass_path(self, path: Any) -> None:
        self._ass_path = str(path or "")
        self.update()

    def ass_path(self) -> str:
        return self._ass_path

    def set_safe_area(self, on: bool) -> None:
        self._show_safe = bool(on)
        self.update()

    def cue_at(self, t_clip: float | None = None) -> Any:
        """A cue visível no tempo informado (``None`` quando não há)."""
        value = self._time if t_clip is None else float(t_clip)
        for cue in self._cues:
            if getattr(cue, "start", 0.0) <= value <= getattr(cue, "end", 0.0):
                return cue
        return None

    def cue_index(self, cue: Any) -> int:
        try:
            return self._cues.index(cue)
        except ValueError:
            return -1

    def counts(self) -> dict[str, Any]:
        """Números honestos para a barra de status da página."""
        return {"cues": len(self._cues), "time": round(self._time, 2),
                "visible": self.cue_at() is not None,
                "ass": self._ass_path,
                "duration": round(sum(max(0.0, getattr(c, "end", 0.0) - getattr(c, "start", 0.0))
                                      for c in self._cues), 2)}

    # ----------------------------------------------------------------- dimensões
    def _box(self) -> QRect:
        """Retângulo do recorte dentro do widget (KeepAspectRatio, centralizado)."""
        out_w, out_h = self._out_size
        area = self.rect().adjusted(2, 2, -2, -2)
        if out_w <= 0 or out_h <= 0:
            return area
        scale = min(area.width() / float(out_w), area.height() / float(out_h))
        w, h = max(1, int(round(out_w * scale))), max(1, int(round(out_h * scale)))
        rect = QRect(QPoint(0, 0), QSize(w, h))
        rect.moveCenter(area.center())
        return rect

    def _scale(self) -> float:
        box = self._box()
        return box.height() / float(self._out_size[1] or box.height())

    # --------------------------------------------------------------------- pinta
    def paintEvent(self, event) -> None:  # noqa: N802 - nome da API Qt
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
        painter.fillRect(self.rect(), QColor(theme.color("bg")))

        box = self._box()
        if self._frame is not None and not self._frame.isNull():
            painter.drawPixmap(box, self._frame)
        else:
            painter.fillRect(box, QColor("#0B0C11"))
        painter.setPen(QPen(QColor(theme.color("border_strong")), 1))
        painter.drawRect(box.adjusted(0, 0, -1, -1))

        if self._show_safe:
            self._paint_safe_area(painter, box)

        self._word_cache = []
        cue = self.cue_at()
        if cue is not None:
            self._paint_cue(painter, box, cue)
        else:
            self._paint_placeholder(painter, box)
        painter.end()

    def _paint_safe_area(self, painter: QPainter, box: QRect) -> None:
        pen = QPen(QColor(255, 255, 255, 46))
        pen.setStyle(Qt.PenStyle.DashLine)
        painter.setPen(pen)
        inner = box.adjusted(int(box.width() * 0.06), int(box.height() * 0.18),
                             -int(box.width() * 0.06), -int(box.height() * 0.22))
        painter.drawRect(inner)

    def _paint_placeholder(self, painter: QPainter, box: QRect) -> None:
        painter.setFont(theme.font(9))
        painter.setPen(QColor(255, 255, 255, 120))
        text = (t("Sem texto neste instante") if self._cues
                else t("Nenhuma legenda gerada para este corte"))
        painter.drawText(box, Qt.AlignmentFlag.AlignCenter, text)

    def _paint_cue(self, painter: QPainter, box: QRect, cue: Any) -> None:
        settings = self._settings
        if settings.style is SubtitleStyle.NONE:
            return
        scale = self._scale()
        px_size = self._font_px(settings)
        font = QFont(settings.font or "Arial")
        font.setPixelSize(px_size)
        font.setBold(settings.style in (SubtitleStyle.VIRAL, SubtitleStyle.DYNAMIC,
                                       SubtitleStyle.PODCAST))
        painter.setFont(font)
        metrics = QFontMetrics(font)

        words = list(getattr(cue, "words", []) or [])
        if not words:
            lines = getattr(cue, "lines", []) or [getattr(cue, "text", lambda: "")()]
            self._paint_line(painter, box, metrics, " ".join(str(x) for x in lines),
                             QColor(settings.color), settings, scale)
            return

        highlight = set(getattr(cue, "highlight", set()) or set())
        pieces: list[tuple[str, QColor]] = []
        for i, word in enumerate(words):
            text = str(getattr(word, "text", ""))
            if settings.uppercase:
                text = text.upper()
            emph = i in highlight and settings.highlight_importance
            if emph:
                if (settings.style is SubtitleStyle.VIRAL
                        and settings.highlight_mode == "multicolor"):
                    color = QColor(VIRAL_PALETTE[(i // 3) % len(VIRAL_PALETTE)])
                elif settings.highlight_mode == "green":
                    color = QColor(settings.highlight_color2)
                else:
                    color = QColor(settings.highlight_color)
            else:
                color = QColor(settings.color)
            pieces.append((text, color))

        # quebra em até 2 linhas equilibradas, igual a _lines() do núcleo
        # _wrap() mede as próprias larguras (usa a fonte do widget); passar
        # `metrics` aqui era o motivo de toda pintura de legenda ao vivo subir
        # TypeError no thread da GUI
        line_groups = self._wrap(pieces, int(box.width() * 0.9))
        total_h = metrics.height() * len(line_groups)
        position = settings.position if settings.position in POSITION_ALIGN else "bottom"
        if position == "bottom":
            y = box.bottom() - int(box.height() * 0.16) - total_h
        elif position == "top":
            y = box.top() + int(box.height() * 0.11)
        else:
            y = box.center().y() - total_h // 2
        for group in line_groups:
            width = sum(metrics.horizontalAdvance(txt) + metrics.horizontalAdvance(" ")
                        for txt, _c in group)
            x = box.center().x() - width // 2
            for txt, color in group:
                rect = QRect(int(x), int(y), metrics.horizontalAdvance(txt) + 2,
                             metrics.height())
                self._paint_word(painter, rect, metrics, txt, color, settings, scale)
                self._word_cache.append((rect, txt, str(getattr(words[0], "start", 0.0))))
                x += metrics.horizontalAdvance(txt) + metrics.horizontalAdvance(" ")
            y += metrics.height()

    def _paint_word(self, painter: QPainter, rect: QRect, metrics: QFontMetrics,
                    text: str, color: QColor, settings: SubtitleSettings,
                    scale: float) -> None:
        if settings.background and settings.border_style == 3:
            back = QColor(settings.background)
            back.setAlpha(max(40, min(255, int(settings.bg_opacity))))
            painter.fillRect(rect.adjusted(-4, -2, 4, 2), back)
        outline = max(0, int(settings.outline * max(1.0, scale * 2)))
        if outline:
            path = QPainterPath()
            path.addText(float(rect.x()), float(rect.bottom() - metrics.descent()),
                        painter.font(), text)
            stroke = QPainterPathStroker()
            stroke.setWidth(float(outline))
            border = stroke.createPath(path).simplified()
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor("#000000"))
            painter.drawPath(border)
            painter.drawPath(path)
        painter.setPen(QPen(color))
        painter.drawText(rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                         text)

    def _paint_line(self, painter: QPainter, box: QRect, metrics: QFontMetrics,
                    text: str, color: QColor, settings: SubtitleSettings,
                    scale: float) -> None:
        rect = QRect(box.center().x() - metrics.horizontalAdvance(text) // 2,
                     box.bottom() - int(box.height() * 0.16) - metrics.height(),
                     metrics.horizontalAdvance(text) + 4, metrics.height())
        self._paint_word(painter, rect, metrics, text, color, settings, scale)

    def _wrap(self, pieces: list[tuple[str, QColor]], max_w: int) -> list[list[tuple[str, QColor]]]:
        """Agrupa palavras em 1..2 linhas sem estourar a largura do recorte."""
        if not pieces:
            return []
        lines: list[list[tuple[str, QColor]]] = [[]]
        width = 0.0
        for txt, color in pieces:
            fm = QFontMetrics(self._current_font())
            advance = fm.horizontalAdvance(txt + " ")
            if width + advance > max_w and lines[-1]:
                lines.append([])
                width = 0.0
            lines[-1].append((txt, color))
            width += advance
        if len(lines) > 2:            # o núcleo limita a 2 linhas; idem aqui
            tail = [item for line in lines[1:] for item in line]
            lines = [lines[0], tail]
        return [line for line in lines if line]

    def _current_font(self) -> QFont:
        settings = self._settings
        font = QFont(settings.font or "Arial")
        font.setPixelSize(self._font_px(settings))
        return font

    def _font_px(self, settings: SubtitleSettings) -> int:
        """Tamanho em pixels do widget, partindo da mesma regra do núcleo.

        O ASS usa ``Fontsize`` na grade do arquivo (``clip_h``). Para a prévia
        bater com o arquivo final, convertemos ``ass_size`` pela proporção
        ``altura_do_quadro_na_tela / altura_do_arquivo``.
        """
        _w, clip_h = self._out_size
        clip_h = max(2, int(clip_h or 1920))
        factors = {SubtitleStyle.VIRAL: 0.062, SubtitleStyle.DYNAMIC: 0.054,
                   SubtitleStyle.PODCAST: 0.050, SubtitleStyle.PROFESSIONAL: 0.040,
                   SubtitleStyle.MINIMALIST: 0.036, SubtitleStyle.NONE: 0.040}
        ass_size = int(settings.size) if settings.size else max(
            16, int(round(clip_h * factors.get(settings.style, 0.048))))
        box_h = max(1, self._box().height())
        return max(8, int(round(ass_size * box_h / float(clip_h))))

    # ---------------------------------------------------------------- interação
    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            pos = event.position().toPoint()
            for index, (rect, text, _start) in enumerate(self._word_cache):
                if rect.adjusted(-2, -2, 2, 2).contains(pos):
                    self.wordClicked.emit(index, text)
                    return
        super().mousePressEvent(event)

    def minimumSizeHint(self) -> QSize:
        return QSize(180, 240)
