"""``ClipTimeline`` — régua de edição desenhada à mão para o editor de cortes.

A timeline é o instrumento fino do editor: ela mostra **falas** (blocos por
segmento com um tick por palavra), **silêncios removidos** (as lacunas entre os
trechos que o :class:`corteia.core.timeline.TimelineMap` manteve) e **marcações
de plano** (cenas). Sobre isso ficam o cursor de reprodução, a faixa do corte e
as duas alças de entrada/saída, que podem ser arrastadas.

Tudo aqui é pintura + eventos: nenhum trabalho de núcleo acontece neste módulo.
A página alimenta o widget com dados já lidos do ``ctx`` e escuta os sinais
``seekRequested`` / ``trimChanged`` / ``loopChanged`` para reagir.

Convenções de tempo (importantes para não confundir quem mexer depois):

* ``set_bounds``/``set_window``/``_t_start``/``_t_end`` são **segundos da
  fonte** (o mesmo referencial de ``clip.start``/``clip.end``).
* O cursor também é em segundos da fonte; ``clip_time_mode`` apenas troca a
  *legenda* mostrada na régua para tempo do corte.
* Progresso de núcleo é fração 0..1; aqui não há progresso, só tempo.
"""

from __future__ import annotations

import logging
from typing import Any, Iterable, Sequence

from PySide6.QtCore import QPoint, QRect, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import QMenu, QSizePolicy, QWidget, QToolTip

from .. import theme
from ...i18n import t
from ...models import format_ts

log = logging.getLogger("corteia.timeline")

#: cores por falante (ciclo) — propositalmente fora da paleta de UI para não
#: confundir "cor de fala" com "cor de estado"
SPEAKER_COLORS = ("#7C5CFF", "#4EC5FF", "#22E07A", "#FFB020", "#FF5C7A", "#C77DFF")

RULER_H = 20
ROW_GAP = 6
SPEECH_H = 44
REMOVAL_H = 20
SCENE_H = 16
TRIM_H = 30
MARGIN_L = 10
MARGIN_R = 10

MIN_WINDOW = 1.5          # menor janela visível (s) — abaixo disso o zoom trava
HANDLE_W = 12             # zona de captura das alças


def _c(token: str, fallback: str = "#888888") -> str:
    """Cor do tema com proteção contra chamada antes do ``QApplication``."""
    try:
        return theme.color(token)
    except Exception:  # noqa: BLE001 - widgets podem ser construídos em testes crus
        return fallback


class ClipTimeline(QWidget):
    """Régua de tempo com falas, lacunas de silêncio, planos e alças de corte."""

    #: pedido de seek (segundos da fonte)
    seekRequested = Signal(float)
    #: cursor movido pelo usuário (sem tocar no player)
    playheadChanged = Signal(float)
    #: nova entrada/saída do corte arrastando as alças
    trimChanged = Signal(float, float)
    #: faixa de repetição escolhida (ou ``None`` para limpar)
    loopChanged = Signal(object)
    #: janela visível mudou (zoom/rolagem) — a página usa para recarregar dados
    windowChanged = Signal(float, float)

    def __init__(self, ctx: Any = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self.setObjectName("clipTimeline")
        self.setProperty("timeline", "true")
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setMinimumHeight(self.total_height())
        self.setAccessibleName(t("Linha do tempo do corte"))

        # ---------------------------------------------------------------- janela
        self._src_len = 0.0
        self._t_start = 0.0
        self._t_end = 1.0
        # ------------------------------------------------------------------ corte
        self._clip_in = 0.0
        self._clip_out = 1.0
        self._playhead = 0.0
        self._loop: tuple[float, float] | None = None
        # ------------------------------------------------------------------- dados
        self._segments: list[dict[str, Any]] = []
        self._removals: list[tuple[float, float]] = []
        self._scenes: list[float] = []
        self._marks: list[tuple[float, str]] = []
        self._thumb: QPixmap | None = None
        self._thumb_at: float | None = None
        # ------------------------------------------------------------- interação
        self._drag: str = ""            # "seek" | "in" | "out" | "loop"
        self._drag_from = 0.0
        self._sel_a: float | None = None
        self._hover = -1                # índice do segmento sob o mouse
        self._pressed_x = 0
        self._clip_time_mode = False    # régua mostra tempo do corte, não da fonte

    # ------------------------------------------------------------------ alturas
    @staticmethod
    def total_height() -> int:
        return (RULER_H + ROW_GAP + SPEECH_H + ROW_GAP + REMOVAL_H
                + ROW_GAP + SCENE_H + ROW_GAP + TRIM_H + 8)

    def sizeHint(self) -> QSize:
        return QSize(600, self.total_height())

    # ----------------------------------------------------------------- API de dados
    def set_source_length(self, seconds: float) -> None:
        self._src_len = max(0.0, float(seconds or 0.0))

    def set_bounds(self, t0: float, t1: float) -> None:
        """Define a janela visível (segundos da fonte)."""
        t0, t1 = float(t0), float(t1)
        if t1 <= t0:
            t1 = t0 + 1.0
        self._t_start, self._t_end = t0, t1
        self.windowChanged.emit(t0, t1)
        self.update()

    def bounds(self) -> tuple[float, float]:
        return self._t_start, self._t_end

    def fit_clip(self, *, padding: float = 0.35, min_window: float = 3.0) -> None:
        """Enquadra o corte com um respiro de contexto dos dois lados."""
        span = max(0.1, self._clip_out - self._clip_in)
        pad = max(0.5, span * padding)
        t0 = max(0.0, self._clip_in - pad)
        t1 = self._t_end if self._src_len <= 0 else min(self._src_len, self._clip_out + pad)
        if t1 - t0 < min_window:
            extra = (min_window - (t1 - t0)) / 2.0
            t0 = max(0.0, t0 - extra)
            t1 = t1 + extra
        self.set_bounds(t0, t1)

    def set_clip(self, t_in: float, t_out: float) -> None:
        """Entrada/saída atuais do corte (sem emitir sinal)."""
        self._clip_in = float(t_in)
        self._clip_out = max(float(t_in) + 0.05, float(t_out))
        self.update()

    def clip_range(self) -> tuple[float, float]:
        return self._clip_in, self._clip_out

    def set_playhead(self, seconds: float, *, repaint_only: bool = True) -> None:
        self._playhead = max(0.0, float(seconds or 0.0))
        if repaint_only:
            self.update()

    def playhead(self) -> float:
        return self._playhead

    def set_loop(self, span: tuple[float, float] | None) -> None:
        if span is None:
            self._loop = None
        else:
            a, b = float(span[0]), float(span[1])
            self._loop = (a, b) if b > a + 0.05 else None
        self.update()

    def loop(self) -> tuple[float, float] | None:
        return self._loop

    def set_segments(self, segments: Iterable[Any] | None) -> None:
        """Aceita ``models.Segment``, dicts ou tuplas ``(start, end, texto, falante, palavras)``."""
        out: list[dict[str, Any]] = []
        for seg in segments or []:
            if isinstance(seg, (tuple, list)) and len(seg) >= 3:
                words = seg[4] if len(seg) > 4 and seg[4] else []
                seg = {"start": seg[0], "end": seg[1], "text": seg[2],
                       "speaker": seg[3] if len(seg) > 3 else None, "words": words}
            if isinstance(seg, dict):
                words = seg.get("words") or []
                item = {
                    "start": float(seg.get("start", 0.0)),
                    "end": float(seg.get("end", 0.0)),
                    "text": str(seg.get("text", "")),
                    "speaker": seg.get("speaker"),
                    "words": [self._word_tuple(w) for w in words],
                }
            else:
                item = {
                    "start": float(getattr(seg, "start", 0.0)),
                    "end": float(getattr(seg, "end", 0.0)),
                    "text": str(getattr(seg, "text", "")),
                    "speaker": getattr(seg, "speaker", None),
                    "words": [self._word_tuple(w) for w in getattr(seg, "words", []) or []],
                }
            if item["end"] > item["start"]:
                out.append(item)
        self._segments = out
        self.update()

    @staticmethod
    def _word_tuple(word: Any) -> tuple[float, float, str]:
        """Normaliza ``models.Word``/dict/tupla em ``(início, fim, texto)``."""
        if isinstance(word, dict):
            # o JSON do projeto guarda {"s","e","t"}: aceita as duas grafias
            return (float(word.get("start", word.get("s", 0.0))),
                    float(word.get("end", word.get("e", 0.0))),
                    str(word.get("text", word.get("t", ""))))
        if isinstance(word, (tuple, list)):
            return (float(word[0]), float(word[1]), str(word[2]) if len(word) > 2 else "")
        return (float(getattr(word, "start", 0.0)), float(getattr(word, "end", 0.0)),
                str(getattr(word, "text", "")))

    def set_removals(self, spans: Sequence[tuple[float, float]] | None) -> None:
        """Lacunas removidas (silêncios cortados) — segundos da fonte."""
        clean: list[tuple[float, float]] = []
        for span in spans or []:
            a, b = float(span[0]), float(span[1])
            if b > a + 0.005:
                clean.append((a, b))
        self._removals = clean
        self.update()

    def set_scenes(self, scenes: Iterable[Any] | None) -> None:
        """Marcações de plano. Aceita ``models.Scene``, dicts ``{s|start}`` ou números."""
        marks: list[float] = []
        for scene in scenes or []:
            if isinstance(scene, dict):
                marks.append(float(scene.get("start", scene.get("s", 0.0))))
            elif isinstance(scene, (tuple, list)) and scene:
                marks.append(float(scene[0]))
            else:
                marks.append(float(getattr(scene, "start", scene)))
        self._scenes = sorted(m for m in marks if m > 0.0)
        self.update()

    def set_marks(self, marks: Iterable[tuple[float, str]] | None) -> None:
        self._marks = [(float(a), str(b)) for a, b in (marks or [])]
        self.update()

    def set_thumbnail(self, pixmap: QPixmap | None) -> None:
        """Fotograma real de referência (``media.thumbnail``); ``None`` desliga."""
        self._thumb = pixmap if (pixmap is not None and not pixmap.isNull()) else None
        self._thumb_at = None
        self.update()

    def set_clip_time_mode(self, on: bool) -> None:
        self._clip_time_mode = bool(on)
        self.update()

    # ------------------------------------------------------------------ conversão
    def _plot_rect(self) -> QRect:
        return QRect(MARGIN_L, 0, max(10, self.width() - MARGIN_L - MARGIN_R), self.height())

    def _x_of(self, seconds: float) -> int:
        plot = self._plot_rect()
        span = self._t_end - self._t_start
        if span <= 0:
            return plot.left()
        frac = (float(seconds) - self._t_start) / span
        return int(round(plot.left() + frac * plot.width()))

    def _t_of(self, x: int | float) -> float:
        plot = self._plot_rect()
        if plot.width() <= 0:
            return self._t_start
        frac = (float(x) - plot.left()) / plot.width()
        value = self._t_start + frac * (self._t_end - self._t_start)
        return self._clamp(value)

    def _clamp(self, value: float) -> float:
        lo = 0.0 if self._src_len <= 0 else 0.0
        hi = self._src_len if self._src_len > 0 else self._t_end
        return max(lo, min(hi, float(value)))

    def _visible(self, a: float, b: float) -> tuple[int, int] | None:
        """Reto angular visível de ``[a,b]``; ``None`` quando fora da janela."""
        if b <= self._t_start or a >= self._t_end or b <= a:
            return None
        x0, x1 = self._x_of(max(a, self._t_start)), self._x_of(min(b, self._t_end))
        if x1 - x0 < 1:
            x1 = x0 + 1
        return x0, x1

    # --------------------------------------------------------------------- zoom
    def zoom_by(self, factor: float, anchor: float | None = None) -> None:
        span = self._t_end - self._t_start
        anchor = self._playhead if anchor is None else float(anchor)
        new_span = max(MIN_WINDOW, min(max(span, self._src_len or span), span * float(factor)))
        if abs(new_span - span) < 1e-3:
            return
        frac = 0.5 if span <= 0 else (anchor - self._t_start) / span
        t0 = anchor - frac * new_span
        t1 = t0 + new_span
        if self._src_len > 0:
            if t0 < 0:
                t0, t1 = 0.0, new_span
            if t1 > self._src_len:
                t1, t0 = self._src_len, max(0.0, self._src_len - new_span)
        self.set_bounds(t0, t1)

    def scroll_by(self, seconds: float) -> None:
        span = self._t_end - self._t_start
        t0 = self._t_start + float(seconds)
        if self._src_len > 0:
            t0 = max(0.0, min(self._src_len - span, t0))
        self.set_bounds(t0, t0 + span)

    # ------------------------------------------------------------------ geometria
    def _row_y(self, row: int) -> int:
        """Topo da faixa ``row`` (0=régua, 1=falas, 2=removidos, 3=planos, 4=alças)."""
        heights = (RULER_H, SPEECH_H, REMOVAL_H, SCENE_H, TRIM_H)
        y = 4
        for i in range(row):
            y += heights[i] + ROW_GAP
        return y

    def _trim_lane(self) -> QRect:
        return QRect(self._plot_rect().left(), self._row_y(4),
                     self._plot_rect().width(), TRIM_H)

    def _handle_rect(self, which: str) -> QRect:
        lane = self._trim_lane()
        x = self._x_of(self._clip_in if which == "in" else self._clip_out)
        return QRect(x - HANDLE_W // 2, lane.top(), HANDLE_W, lane.height())

    def _handle_at(self, pos: QPoint) -> str:
        if not self._trim_lane().adjusted(-2, -6, 2, 6).contains(pos):
            return ""
        for which in ("in", "out"):
            if self._handle_rect(which).adjusted(-4, 0, 4, 0).contains(pos):
                return which
        return ""

    # --------------------------------------------------------------------- pintura
    def paintEvent(self, event) -> None:  # noqa: N802 - nome da API Qt
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)

        plot = self._plot_rect()
        bg = QColor(_c("surface2"))
        painter.fillRect(self.rect(), bg)
        painter.setPen(QPen(QColor(_c("border"))))
        painter.drawRect(plot.adjusted(0, 0, -1, -1))

        self._paint_thumbnail(painter, plot)
        self._paint_grid(painter, plot)
        self._paint_clip_band(painter, plot)
        self._paint_ruler(painter, plot)
        self._paint_speech(painter)
        self._paint_removals(painter)
        self._paint_scenes(painter, plot)
        self._paint_loop(painter, plot)
        self._paint_trim_lane(painter)
        self._paint_marks(painter, plot)
        self._paint_playhead(painter, plot)
        painter.end()

    def _paint_thumbnail(self, painter: QPainter, plot: QRect) -> None:
        """Fotograma de referência ancorado no instante que ele representa.

        Não é uma película frame a frame (isso exigiria decodificar o vídeo na
        thread da GUI): é o quadro real devolvido por ``media.thumbnail``,
        desenhado no tempo correspondente da régua. A imagem em movimento
        continua sendo do ``VideoPlayer``.
        """
        if self._thumb is None:
            return
        lane = QRect(plot.left(), self._row_y(1), plot.width(), SPEECH_H)
        at = self._thumb_at if self._thumb_at is not None else self._clip_in
        size = lane.height() - 8
        scaled = self._thumb.scaled(size, size, Qt.AspectRatioMode.KeepAspectRatio,
                                    Qt.TransformationMode.SmoothTransformation)
        x = self._x_of(at) - scaled.width() // 2
        y = lane.top() + (lane.height() - scaled.height()) // 2
        painter.setOpacity(0.85)
        painter.drawPixmap(QRect(x, y, scaled.width(), scaled.height()), self._thumb)
        painter.setOpacity(1.0)

    def _paint_grid(self, painter: QPainter, plot: QRect) -> None:
        """Divisórias de tempo com intervalo "bonito" (0,1,2,5,10,15,30,60 s)."""
        span = max(0.001, self._t_end - self._t_start)
        target = plot.width() / 96.0
        step = next((s for s in (0.1, 0.25, 0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300)
                     if span / s <= max(3.0, target)), 600.0)
        start = (self._t_start // step) * step
        painter.setFont(theme.mono_font(9))
        minor = QPen(QColor(_c("border")), 1)
        major = QPen(QColor(_c("border_strong")), 1)
        value = start
        while value <= self._t_end + step:
            x = self._x_of(value)
            if plot.left() <= x <= plot.right():
                painter.setPen(minor if step < 1 else major)
                painter.drawLine(x, self._row_y(1), x, self._row_y(4) + TRIM_H)
                painter.setPen(QColor(_c("faint")))
                label = f"{value:.1f}s" if step < 1 else f"{int(round(value))}s"
                painter.drawText(QRect(x + 3, self._row_y(0), 60, RULER_H),
                                 Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                                 label)
            value += step

    def _paint_clip_band(self, painter: QPainter, plot: QRect) -> None:
        """Faixa do corte fora da janela visível (o que não entra no recorte)."""
        outside = QColor(_c("bg"))
        outside.setAlpha(150)
        painter.fillRect(QRect(plot.left(), self._row_y(1),
                               max(0, self._x_of(self._clip_in) - plot.left()),
                               SPEECH_H + ROW_GAP + REMOVAL_H + ROW_GAP + SCENE_H), outside)
        x_out = self._x_of(self._clip_out)
        painter.fillRect(QRect(x_out, self._row_y(1),
                               max(0, plot.right() - x_out),
                               SPEECH_H + ROW_GAP + REMOVAL_H + ROW_GAP + SCENE_H), outside)

    def _paint_ruler(self, painter: QPainter, plot: QRect) -> None:
        rect = QRect(plot.left(), self._row_y(0), plot.width(), RULER_H)
        painter.fillRect(rect, QColor(_c("surface3")))
        painter.setPen(QPen(QColor(_c("border"))))
        painter.drawLine(rect.bottomLeft(), rect.bottomRight())
        if self._clip_time_mode:
            label = t("tempo do corte")
        else:
            label = t("tempo da fonte")
        painter.setFont(theme.font(9, "medium"))
        painter.setPen(QColor(_c("muted")))
        painter.drawText(rect.adjusted(4, 0, -4, 0),
                         Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, label)

    def _paint_speech(self, painter: QPainter) -> None:
        top = self._row_y(1)
        if not self._segments:
            painter.setFont(theme.font(10))
            painter.setPen(QColor(_c("faint")))
            painter.drawText(QRect(self._plot_rect().left() + 6, top,
                                   self._plot_rect().width() - 12, SPEECH_H),
                             Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                             t("Sem transcrição: nada para exibir nesta faixa"))
            return
        for i, seg in enumerate(self._segments):
            vis = self._visible(seg["start"], seg["end"])
            if vis is None:
                continue
            x0, x1 = vis
            speaker = seg.get("speaker")
            color = QColor(SPEAKER_COLORS[(int(speaker) if speaker is not None else 0)
                                          % len(SPEAKER_COLORS)])
            if i == self._hover:
                color.setAlpha(235)
            else:
                color.setAlpha(180)
            rect = QRect(x0, top, max(2, x1 - x0), SPEECH_H)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawRoundedRect(QRectF(rect).adjusted(0.5, 0.5, -0.5, -0.5), 4, 4)
            # tick por palavra: leitura de ritmo, não decoração
            words = seg.get("words") or []
            if words and rect.width() > 14:
                painter.setPen(QPen(QColor(255, 255, 255, 90), 1))
                for w_start, _w_end, _text in words[1:]:
                    wx = self._x_of(w_start)
                    if rect.left() + 2 < wx < rect.right() - 1:
                        painter.drawLine(wx, rect.top() + 2, wx, rect.top() + 8)
            painter.setPen(QPen(QColor(_c("text") if color.lightness() < 140
                                       else _c("bg"))))
            painter.setFont(theme.font(9, "medium"))
            label = seg["text"].strip() or t("(sem texto)")
            fm = painter.fontMetrics()
            if rect.width() > 2 * fm.horizontalAdvance("...") and fm.horizontalAdvance(label) > rect.width() - 10:
                while label and fm.horizontalAdvance(label + "…") > rect.width() - 10:
                    label = label[:-1]
                label += "…"
            painter.drawText(rect.adjusted(6, 0, -6, 0),
                             Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, label)
            if speaker is not None and rect.width() > 40:
                painter.setFont(theme.mono_font(8))
                painter.setPen(QColor(_c("text") if color.lightness() < 140 else _c("bg")))
                painter.drawText(rect.adjusted(6, 1, -6, -1),
                                 Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop,
                                 f"F{int(speaker) + 1}")

    def _paint_removals(self, painter: QPainter) -> None:
        top = self._row_y(2)
        plot = self._plot_rect()
        lane = QRect(plot.left(), top, plot.width(), REMOVAL_H)
        painter.fillRect(lane, QColor(_c("surface")))
        if not self._removals:
            painter.setFont(theme.font(8))
            painter.setPen(QColor(_c("faint")))
            painter.drawText(lane.adjusted(6, 0, -6, 0),
                             Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                             t("nenhum silêncio removido"))
        total = 0.0
        for a, b in self._removals:
            total += max(0.0, b - a)
            vis = self._visible(a, b)
            if vis is None:
                continue
            x0, x1 = vis
            rect = QRect(x0, top + 2, max(2, x1 - x0), REMOVAL_H - 4)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(_c("error")))
            color = QColor(_c("error"))
            color.setAlpha(120)
            painter.setBrush(color)
            painter.drawRect(rect)
            # hachura: lacuna cortada, não "espaço vazio"
            painter.setClipRect(rect)
            painter.setPen(QPen(QColor(0, 0, 0, 90), 1))
            x = rect.left() - rect.height()
            while x < rect.right():
                painter.drawLine(x, rect.bottom(), x + rect.height(), rect.top())
                x += 6
            painter.setClipping(False)
        if total > 0.05:
            painter.setFont(theme.mono_font(8))
            painter.setPen(QColor(_c("muted")))
            painter.drawText(lane.adjusted(6, 0, -6, 0),
                             Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                             t("{n} trecho(s) cortados · {s}").format(
                                 n=len(self._removals), s=f"{total:.1f} s"))

    def _paint_scenes(self, painter: QPainter, plot: QRect) -> None:
        top = self._row_y(3)
        lane = QRect(plot.left(), top, plot.width(), SCENE_H)
        painter.setPen(QPen(QColor(_c("info")), 2))
        if not self._scenes:
            painter.setFont(theme.font(8))
            painter.setPen(QColor(_c("faint")))
            painter.drawText(lane.adjusted(6, 0, -6, 0),
                             Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                             t("nenhum plano detectado"))
            return
        for t_scene in self._scenes:
            vis = self._visible(t_scene, t_scene + 0.001)
            if vis is None:
                continue
            x = vis[0]
            painter.drawLine(x, lane.top(), x, lane.bottom())
            painter.setPen(QPen(QColor(_c("info"))))
            path = QPainterPath()
            path.moveTo(x - 3, lane.top())
            path.lineTo(x + 3, lane.top())
            path.lineTo(x, lane.top() + 5)
            path.closeSubpath()
            painter.fillPath(path, QColor(_c("info")))
            painter.setPen(QPen(QColor(_c("info")), 2))

    def _paint_loop(self, painter: QPainter, plot: QRect) -> None:
        if self._loop is None:
            return
        vis = self._visible(*self._loop)
        if vis is None:
            return
        x0, x1 = vis
        rect = QRect(x0, self._row_y(1), max(2, x1 - x0),
                     SPEECH_H + ROW_GAP + REMOVAL_H + ROW_GAP + SCENE_H)
        color = QColor(_c("accent"))
        color.setAlpha(28)
        painter.fillRect(rect, color)
        pen = QPen(QColor(_c("accent")))
        pen.setStyle(Qt.PenStyle.DashLine)
        painter.setPen(pen)
        painter.drawRect(rect.adjusted(0, 0, -1, -1))
        painter.setFont(theme.font(8, "medium"))
        painter.setPen(QColor(_c("accent")))
        painter.drawText(rect.adjusted(4, 2, -4, 0),
                         Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop, t("repetir"))

    def _paint_trim_lane(self, painter: QPainter) -> None:
        lane = self._trim_lane()
        x_in, x_out = self._x_of(self._clip_in), self._x_of(self._clip_out)
        painter.setPen(QPen(QColor(_c("border_strong")), 2))
        painter.drawLine(lane.left(), lane.center().y(), x_in, lane.center().y())
        painter.drawLine(x_out, lane.center().y(), lane.right(), lane.center().y())
        painter.setPen(QPen(QColor(_c("accent")), 3))
        painter.drawLine(x_in, lane.center().y(), x_out, lane.center().y())
        accent = QColor(_c("accent"))
        warn = QColor(_c("warn"))
        for which, x, color in (("in", x_in, warn), ("out", x_out, warn)):
            rect = self._handle_rect(which)
            painter.setPen(QPen(color.darker(115), 1))
            painter.setBrush(color)
            painter.drawRoundedRect(QRectF(rect.adjusted(0.5, 2.5, -0.5, -2.5)), 3, 3)
            painter.setPen(QPen(QColor(_c("bg"))))
            painter.setFont(theme.mono_font(8, "bold"))
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter,
                             "IN" if which == "in" else "OUT")
        painter.setPen(QPen(accent))
        painter.setFont(theme.mono_font(8))
        mid = QRect(min(x_in, x_out), lane.bottom() - 12, max(2, abs(x_out - x_in)), 12)
        label = f"{self._clip_out - self._clip_in:.1f} s"
        if mid.width() > painter.fontMetrics().horizontalAdvance(label) + 6:
            painter.drawText(mid, Qt.AlignmentFlag.AlignCenter, label)

    def _paint_marks(self, painter: QPainter, plot: QRect) -> None:
        for value, label in self._marks:
            vis = self._visible(value, value + 0.001)
            if vis is None:
                continue
            x = vis[0]
            pen = QPen(QColor(_c("success")))
            pen.setStyle(Qt.PenStyle.DotLine)
            painter.setPen(pen)
            painter.drawLine(x, self._row_y(1), x, self._row_y(4))
            painter.setFont(theme.font(8))
            painter.setPen(QColor(_c("success")))
            painter.drawText(x + 3, self._row_y(3) + 2, 120, 12,
                             Qt.AlignmentFlag.AlignLeft, label)

    def _paint_playhead(self, painter: QPainter, plot: QRect) -> None:
        x = self._x_of(self._playhead)
        if not (plot.left() - 1 <= x <= plot.right() + 1):
            return
        top = self._row_y(0)
        bottom = self._row_y(4) + TRIM_H
        painter.setPen(QPen(QColor(_c("text")), 1))
        painter.drawLine(x, top, x, bottom)
        painter.setBrush(QColor(_c("text")))
        painter.setPen(Qt.PenStyle.NoPen)
        path = QPainterPath()
        path.moveTo(x - 5, top)
        path.lineTo(x + 5, top)
        path.lineTo(x, top + 7)
        path.closeSubpath()
        painter.drawPath(path)
        # etiqueta com o tempo: fonte ou corte, conforme o modo
        shown = self._playhead
        if self._clip_time_mode:
            shown = max(0.0, self._playhead - self._clip_in)
        label = format_ts(shown)
        painter.setFont(theme.mono_font(9, "bold"))
        fm = painter.fontMetrics()
        box = QRect(x - 24, bottom + 1, 48, 14)
        if box.bottom() > self.height() - 1:
            box.moveTop(bottom - 15)
        painter.setBrush(QColor(_c("surface3")))
        painter.setPen(QPen(QColor(_c("border_strong"))))
        painter.drawRoundedRect(box, 3, 3)
        painter.setPen(QColor(_c("text")))
        painter.drawText(box, Qt.AlignmentFlag.AlignCenter, label)

    # ------------------------------------------------------------------- eventos
    def mousePressEvent(self, event) -> None:  # noqa: N802
        pos = event.position().toPoint()
        self._pressed_x = pos.x()
        if event.button() == Qt.MouseButton.RightButton:
            self._show_menu(event.mapTo(self, pos) if hasattr(event, "mapTo") else pos)
            event.accept()
            return
        if event.button() != Qt.MouseButton.LeftButton:
            super().mousePressEvent(event)
            return
        handle = self._handle_at(pos)
        if handle:
            self._drag = handle
            self.update()
            event.accept()
            return
        seconds = self._t_of(pos.x())
        modifiers = event.modifiers()
        if bool(modifiers & Qt.KeyboardModifier.ShiftModifier):
            self._drag = "loop"
            self._sel_a = seconds
            self.set_loop((seconds, seconds))
        else:
            self._drag = "seek"
            self._playhead = seconds
            self.playheadChanged.emit(seconds)
            self.seekRequested.emit(seconds)
        self.update()
        event.accept()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        pos = event.position().toPoint()
        seconds = self._t_of(pos.x())
        if self._drag == "seek":
            self._playhead = seconds
            self.playheadChanged.emit(seconds)
            self.seekRequested.emit(seconds)
            self.update()
            return
        if self._drag in ("in", "out"):
            value = self._clamp(seconds)
            if self._drag == "in":
                new_in = min(value, self._clip_out - 0.2)
                new_in = max(0.0, new_in)
                self._clip_in = new_in
            else:
                new_out = max(value, self._clip_in + 0.2)
                if self._src_len > 0:
                    new_out = min(new_out, self._src_len)
                self._clip_out = new_out
            self.trimChanged.emit(self._clip_in, self._clip_out)
            self.update()
            return
        if self._drag == "loop" and self._sel_a is not None:
            a, b = sorted((self._sel_a, seconds))
            self._loop = (a, b)
            self.update()
            return
        # hover dos blocos de fala
        index = self._segment_at(pos)
        if index != self._hover:
            self._hover = index
            if index >= 0:
                seg = self._segments[index]
                speaker = seg.get("speaker")
                who = (t("Falante {n}").format(n=int(speaker) + 1)
                       if speaker is not None else t("falante único"))
                QToolTip.showText(event.globalPosition().toPoint() if hasattr(event, "globalPosition")
                                  else pos,
                                  f"{format_ts(seg['start'])} → {format_ts(seg['end'])} · {who}\n"
                                  f"{seg['text']}", self)
            else:
                QToolTip.hideText()
            self.update()
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if self._drag == "loop" and self._loop is not None:
            a, b = self._loop
            if b - a >= 0.3:
                self.loopChanged.emit((a, b))
            else:
                self._loop = None
                self.loopChanged.emit(None)
            self._sel_a = None
        self._drag = ""
        self.update()
        super().mouseReleaseEvent(event)

    def wheelEvent(self, event) -> None:  # noqa: N802
        delta = event.angleDelta().y()
        mods = event.modifiers()
        if bool(mods & Qt.KeyboardModifier.ControlModifier) or bool(
                mods & Qt.KeyboardModifier.MetaModifier):
            self.zoom_by(0.85 if delta > 0 else 1.18, anchor=self._t_of(event.position().x()))
        else:
            step = (self._t_end - self._t_start) * (0.12 if delta > 0 else -0.12)
            self.scroll_by(step)
        event.accept()

    def keyPressEvent(self, event) -> None:  # noqa: N802
        key = event.key()
        mods = event.modifiers()
        frame = 1.0 / max(1.0, (getattr(self.ctx, "project", None)
                                and 25.0) or 25.0)
        if key == Qt.Key.Key_Left:
            self._nudge(-(1.0 if mods & Qt.KeyboardModifier.ShiftModifier else frame))
        elif key == Qt.Key.Key_Right:
            self._nudge(1.0 if mods & Qt.KeyboardModifier.ShiftModifier else frame)
        elif key in (Qt.Key.Key_I,):
            self._set_handle("in")
        elif key in (Qt.Key.Key_O,):
            self._set_handle("out")
        elif key == Qt.Key.Key_L:
            self.loopChanged.emit((self._clip_in, self._clip_out))
        elif key == Qt.Key.Key_F:
            self.fit_clip()
        elif key in (Qt.Key.Key_Plus, Qt.Key.Key_Equal):
            self.zoom_by(0.8)
        elif key in (Qt.Key.Key_Minus, Qt.Key.Key_Underscore):
            self.zoom_by(1.25)
        else:
            super().keyPressEvent(event)
            return
        event.accept()

    def _nudge(self, delta: float) -> None:
        value = self._clamp(self._playhead + delta)
        self._playhead = value
        self.playheadChanged.emit(value)
        self.seekRequested.emit(value)
        self.update()

    def _set_handle(self, which: str) -> None:
        if which == "in":
            self._clip_in = min(self._playhead, self._clip_out - 0.2)
        else:
            self._clip_out = max(self._playhead, self._clip_in + 0.2)
        self.trimChanged.emit(self._clip_in, self._clip_out)
        self.update()

    def _segment_at(self, pos: QPoint) -> int:
        top = self._row_y(1)
        if not (top <= pos.y() <= top + SPEECH_H):
            return -1
        seconds = self._t_of(pos.x())
        for i, seg in enumerate(self._segments):
            if seg["start"] <= seconds <= seg["end"]:
                return i
        return -1

    # --------------------------------------------------------------------- menu
    def _show_menu(self, pos: QPoint) -> None:
        seconds = self._t_of(pos.x())
        menu = QMenu(self)
        menu.addAction(theme.icon("play", "muted", 16), t("Ir para aqui"),
                       lambda: (self.seekRequested.emit(seconds),
                                self.set_playhead(seconds)))
        menu.addAction(theme.icon("crop", "muted", 16), t("Definir entrada aqui"),
                       lambda: self._apply_in(seconds))
        menu.addAction(theme.icon("crop", "muted", 16), t("Definir saída aqui"),
                       lambda: self._apply_out(seconds))
        menu.addSeparator()
        menu.addAction(theme.icon("refresh", "muted", 16), t("Repetir este trecho…"),
                       lambda: self._pick_loop(seconds))
        menu.addAction(theme.icon("close", "muted", 16), t("Limpar repetição"),
                       lambda: (self.set_loop(None), self.loopChanged.emit(None)))
        menu.addSeparator()
        menu.addAction(theme.icon("search", "muted", 16), t("Enquadrar o corte (F)"), self.fit_clip)
        menu.addAction(t("Ampliar"), lambda: self.zoom_by(0.75))
        menu.addAction(t("Reduzir"), lambda: self.zoom_by(1.33))
        menu.exec(self.mapToGlobal(pos))

    def _apply_in(self, seconds: float) -> None:
        self._clip_in = max(0.0, min(seconds, self._clip_out - 0.2))
        self.trimChanged.emit(self._clip_in, self._clip_out)
        self.update()

    def _apply_out(self, seconds: float) -> None:
        limit = self._src_len if self._src_len > 0 else seconds
        self._clip_out = min(max(seconds, self._clip_in + 0.2), max(limit, seconds))
        self.trimChanged.emit(self._clip_in, self._clip_out)
        self.update()

    def _pick_loop(self, seconds: float) -> None:
        """Loop a partir de ``seconds`` até a saída do corte (ou 4 s)."""
        end = self._clip_out if self._clip_out > seconds + 0.5 else seconds + 4.0
        span = (seconds, min(end, seconds + 15.0))
        self.set_loop(span)
        self.loopChanged.emit(span)

    # ---------------------------------------------------------------- utilitários
    def removed_spans(self, t0: float, t1: float, kept: Sequence[tuple[float, float]],
                      ) -> list[tuple[float, float]]:
        """Lacunas entre os trechos mantidos (``TimelineMap.ranges``) de ``[t0,t1]``.

        ``TimelineMap`` não expõe as remoções explicitamente, então a UI deriva:
        tudo que está dentro do corte e não está em ``kept`` foi cortado.
        """
        gaps: list[tuple[float, float]] = []
        cursor = float(t0)
        for a, b in sorted(kept):
            if a > cursor + 0.005:
                gaps.append((cursor, min(a, float(t1))))
            cursor = max(cursor, float(b))
        if cursor < float(t1) - 0.005:
            gaps.append((cursor, float(t1)))
        return [(a, b) for a, b in gaps if b - a > 0.01]

    def state(self) -> dict[str, Any]:
        """Instantâneo para logs/testes (nada aqui é segredo de estado)."""
        return {
            "window": (round(self._t_start, 3), round(self._t_end, 3)),
            "clip": (round(self._clip_in, 3), round(self._clip_out, 3)),
            "playhead": round(self._playhead, 3),
            "loop": None if self._loop is None else (round(self._loop[0], 3),
                                                     round(self._loop[1], 3)),
            "segments": len(self._segments),
            "removals": len(self._removals),
            "scenes": len(self._scenes),
        }
