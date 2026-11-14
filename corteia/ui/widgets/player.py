"""Video player widget: real ``QMediaPlayer`` playback plus a crop overlay.

The player is the visual half of the editor: it shows the *source* video and
darkens everything outside the current 9:16 (or 16:9 / 1:1 / 4:5) crop, so the
user sees the framing before rendering.

Environment honesty (measured on the headless Linux sandbox)
------------------------------------------------------------
* Creating a ``QAudioOutput`` with no usable audio server **segfaults** the
  process (pipewire without configuration), so audio is attached only on a real
  desktop platform or when ``CORTEIA_MEDIA_AUDIO=1`` forces it; otherwise the
  player runs silent and says so in its status line.
* ``QMediaPlayer.videoSize()`` blocks the GUI thread here, so the frame size is
  taken from ``set_frame_size`` (the probe) or from decoded frames on the sink.
* If decoding fails the area falls back to a **real** FFmpeg frame.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from PySide6.QtCore import QPoint, QRect, QSize, Qt, QUrl, Signal
from PySide6.QtGui import QColor, QPainter, QPen, QPixmap
from PySide6.QtMultimedia import QMediaPlayer
from PySide6.QtWidgets import (
    QComboBox, QHBoxLayout, QLabel, QPushButton, QSizePolicy, QSlider,
    QVBoxLayout, QWidget,
)

try:
    from PySide6.QtMultimediaWidgets import QVideoWidget
except Exception:                                  # pragma: no cover
    QVideoWidget = None                            # type: ignore[assignment]

log = logging.getLogger("corteia")

SPEEDS = (0.5, 0.75, 1.0, 1.25, 1.5, 2.0)

_FALLBACK_COLORS = {
    "surface": "#15171E", "surface2": "#1B1E27", "text": "#E8EAF2",
    "text_dim": "#9AA0B5", "accent": "#7C5CFF", "ok": "#22E07A",
    "warn": "#FFB020", "err": "#FF5C7A", "border": "#2A2E3B",
}


def _hex(name: str) -> str:
    """Theme colour with a hard fallback so widgets work without ``theme.py``."""
    try:
        from corteia.ui.theme import color as _theme_color

        got = _theme_color(name)
        if isinstance(got, str) and got:
            return got
    except Exception:                                  # noqa: BLE001
        pass
    return _FALLBACK_COLORS.get(name, "#E8EAF2")


def ts_short(seconds: float) -> str:
    """``3.25`` -> ``00:03.2`` (the player needs tenths, ``format_ts`` has none)."""
    seconds = max(0.0, float(seconds or 0.0))
    minutes, rest = divmod(seconds, 60.0)
    return f"{int(minutes):02d}:{rest:04.1f}"


def audio_backend_allowed() -> bool:
    """Whether touching the Qt audio backend is safe in this environment."""
    flag = os.environ.get("CORTEIA_MEDIA_AUDIO", "").strip().lower()
    if flag in ("0", "off", "no", "false"):
        return False
    if flag in ("1", "on", "yes", "true"):
        return True
    platform = os.environ.get("QT_QPA_PLATFORM", "").split(";")[0].strip().lower()
    return platform not in ("", "offscreen", "minimal")


def normalise_rect(rect: Any) -> tuple[int, int, int, int] | None:
    """Accept ``None`` / ``QRect`` / ``(x, y, w, h)`` and return a 4-tuple."""
    if rect is None:
        return None
    if isinstance(rect, QRect):
        return rect.x(), rect.y(), rect.width(), rect.height()
    if isinstance(rect, (tuple, list)) and len(rect) == 4:
        x, y, w, h = (float(v) for v in rect)
        return int(round(x)), int(round(y)), int(round(w)), int(round(h))
    raise TypeError("esperado QRect ou (x, y, w, h)")


def frame_to_image(frame: Any):
    """Convert a ``QVideoFrame`` to a ``QImage`` without blocking the GUI."""
    try:
        if frame is None or not frame.isValid():
            return None
        image = frame.toImage()
        if image is None or image.isNull():
            return None
        return image
    except Exception:                                  # noqa: BLE001
        log.debug("conversão de quadro falhou", exc_info=True)
        return None


class PosterLabel(QWidget):
    """Real frame used when the multimedia backend cannot show anything."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self._pixmap: QPixmap | None = None
        self._caption = ""
        self.setVisible(False)

    def pixmap(self) -> QPixmap | None:
        """The real frame currently shown as a fallback (``None`` when hidden)."""
        return self._pixmap

    def set_pixmap(self, pixmap: QPixmap | None) -> None:
        self._pixmap = pixmap
        self.setVisible(pixmap is not None or bool(self._caption))
        self.update()

    def set_caption(self, text: str) -> None:
        self._caption = text or ""
        self.setVisible(bool(self._pixmap is not None or self._caption))
        self.update()

    def clear(self) -> None:
        self._pixmap = None
        self._caption = ""
        self.setVisible(False)
        self.update()

    def paintEvent(self, event) -> None:              # noqa: N802 - Qt override
        if self._pixmap is None and not self._caption:
            return
        painter = QPainter(self)
        if self._pixmap is not None and not self._pixmap.isNull():
            size = self._pixmap.size()
            size.scale(self.size(), Qt.KeepAspectRatio)
            rect = QRect(QPoint(0, 0), size)
            rect.moveCenter(self.rect().center())
            painter.drawPixmap(rect, self._pixmap)
        if self._caption:
            painter.setPen(QPen(QColor(_hex("text"))))
            painter.drawText(self.rect().adjusted(8, 8, -8, -8),
                             Qt.AlignHCenter | Qt.AlignTop, self._caption)
        painter.end()


class CropOverlay(QWidget):
    """Darkens everything outside the crop rect and draws framing guides.

    Rectangles handed to :meth:`set_crop_rect` are in **source pixels**: they
    are mapped onto the letterboxed picture, so what the user sees is what
    FFmpeg will cut (same ``(x, y, w, h)`` as ``CropPlan.points``).
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self._crop: tuple[int, int, int, int] | None = None
        self._source = QSize(0, 0)
        self._label = ""
        self._thirds = True

    def set_crop_rect(self, rect: Any) -> None:
        self._crop = normalise_rect(rect)
        self.update()

    def crop_rect(self) -> tuple[int, int, int, int] | None:
        return self._crop

    def set_source_size(self, width: int, height: int) -> None:
        self._source = QSize(int(width), int(height))
        self.update()

    def source_size(self) -> QSize:
        return self._source

    def set_label(self, text: str) -> None:
        self._label = text or ""
        self.update()

    def set_thirds_visible(self, visible: bool) -> None:
        self._thirds = bool(visible)
        self.update()

    def video_rect(self) -> QRect:
        """Letterboxed rect of the picture inside this widget."""
        if self._source.width() > 0 and self._source.height() > 0:
            size = self._source.scaled(self.size(), Qt.KeepAspectRatio)
        else:
            size = QSize(self.size())
        rect = QRect(QPoint(0, 0), size)
        rect.moveCenter(self.rect().center())
        return rect

    def crop_widget_rect(self) -> QRect | None:
        """Crop rect mapped to widget coordinates (``None`` when unset)."""
        if self._crop is None:
            return None
        x, y, w, h = self._crop
        src_w, src_h = self._source.width(), self._source.height()
        if src_w <= 0 or src_h <= 0 or w <= 0 or h <= 0:
            return None
        area = self.video_rect()
        sx = area.width() / float(src_w)
        sy = area.height() / float(src_h)
        return QRect(area.topLeft() + QPoint(int(round(x * sx)), int(round(y * sy))),
                     QSize(max(2, int(round(w * sx))), max(2, int(round(h * sy)))))

    def paintEvent(self, event) -> None:              # noqa: N802 - Qt override
        area = self.video_rect()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        crop = self.crop_widget_rect()
        if crop is None:
            painter.setPen(QPen(QColor(_hex("border")), 1))
            painter.drawRect(area.adjusted(0, 0, -1, -1))
            self._paint_label(painter, area)
            painter.end()
            return
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(0, 0, 0, 168))
        bands = (
            QRect(0, 0, self.width(), crop.top()),
            QRect(0, crop.bottom() + 1, self.width(),
                  max(0, self.height() - crop.bottom() - 1)),
            QRect(0, crop.top(), crop.left(), crop.height()),
            QRect(crop.right() + 1, crop.top(),
                  max(0, self.width() - crop.right() - 1), crop.height()),
        )
        for band in bands:
            if band.width() > 0 and band.height() > 0:
                painter.drawRect(band)
        painter.setBrush(Qt.NoBrush)
        painter.setPen(QPen(QColor(_hex("accent")), 2))
        painter.drawRect(crop.adjusted(0, 0, -1, -1))
        if self._thirds:
            painter.setPen(QPen(QColor(255, 255, 255, 55), 1, Qt.DashLine))
            for i in (1, 2):
                gx = crop.left() + crop.width() * i // 3
                gy = crop.top() + crop.height() * i // 3
                painter.drawLine(gx, crop.top(), gx, crop.bottom())
                painter.drawLine(crop.left(), gy, crop.right(), gy)
        self._paint_label(painter, crop)
        painter.end()

    def _paint_label(self, painter: QPainter, near: QRect) -> None:
        if not self._label:
            return
        painter.setPen(QPen(QColor(_hex("text"))))
        painter.drawText(QRect(near.left() - 4, max(0, near.top() - 22),
                               max(160, near.width() + 8), 20),
                         Qt.AlignLeft | Qt.AlignVCenter, self._label)


class FrameArea(QWidget):
    """Keeps the video surface, the poster fallback and the crop overlay."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumHeight(200)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.video_widget: QVideoWidget | None = None
        if QVideoWidget is not None:
            self.video_widget = QVideoWidget(self)
            self.video_widget.setStyleSheet("background: #000000;")
        self.poster = PosterLabel(self)
        self.overlay = CropOverlay(self)
        self._sink = None
        self._sync_children()

    def resizeEvent(self, event) -> None:             # noqa: N802 - Qt override
        super().resizeEvent(event)
        self._sync_children()

    def _sync_children(self) -> None:
        if self.video_widget is not None:
            self.video_widget.setGeometry(self.rect())
        self.poster.setGeometry(self.rect())
        self.overlay.setGeometry(self.video_widget.geometry()
                                  if self.video_widget else self.rect())

    def surface_for(self, player: QMediaPlayer) -> None:
        """Attach ``player``'s video to the surface actually available."""
        if self.video_widget is not None:
            player.setVideoOutput(self.video_widget)
            return
        from PySide6.QtMultimedia import QVideoSink

        self._sink = QVideoSink(self)
        self._sink.videoFrameChanged.connect(self._on_frame)
        player.setVideoSink(self._sink)

    def video_sink(self) -> Any:
        if self.video_widget is not None:
            try:
                return self.video_widget.videoSink()
            except Exception:                              # noqa: BLE001
                return None
        return self._sink

    def _on_frame(self, frame: Any) -> None:           # manual painting path only
        image = frame_to_image(frame)
        if image is not None:
            self.poster.set_pixmap(QPixmap.fromImage(image))
            self.poster.set_caption("")

    def paintEvent(self, event) -> None:              # noqa: N802 - Qt override
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#05060A"))
        painter.end()


class VideoPlayer(QWidget):
    """Transport controls + video surface + crop overlay for one source file."""

    timeChanged = Signal(float)              # absolute seconds inside the media
    mediaLoaded = Signal(bool)
    ended = Signal(float)                    # playback reached the range end

    def __init__(self, ctx: Any = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self._path: Path | None = None
        self._duration = 0.0
        self._fps = 30.0
        self._range: tuple[float, float] | None = None
        self._loop: tuple[float, float] | None = None
        self._slider_down = False
        self._pending_start = 0.0
        self._poster_pending = False
        self._audio_ok = False
        self._last_crop: tuple[int, int, int, int] | None = None

        self.player = QMediaPlayer(self)
        self.audio_output = None
        if audio_backend_allowed():
            try:
                from PySide6.QtMultimedia import QAudioOutput

                self.audio_output = QAudioOutput(self)
                self.audio_output.setVolume(0.8)
                self.player.setAudioOutput(self.audio_output)
                self._audio_ok = True
            except Exception:                                  # noqa: BLE001
                log.warning("saida de audio indisponivel", exc_info=True)
                self.audio_output = None

        self.area = FrameArea(self)
        self.area.surface_for(self.player)
        sink = self.area.video_sink()
        if sink is not None:
            try:
                sink.videoFrameChanged.connect(self._on_video_frame)
            except Exception:                                  # noqa: BLE001
                log.debug("sink sem videoFrameChanged", exc_info=True)

        self._build_controls()
        self.player.positionChanged.connect(self._on_position)
        self.player.durationChanged.connect(self._on_duration)
        self.player.mediaStatusChanged.connect(self._on_status)
        self.player.playbackStateChanged.connect(self._on_state)
        self.player.errorOccurred.connect(self._on_error)

    def _build_controls(self) -> None:
        self.btn_play = QPushButton("Reproduzir")
        self.btn_play.setObjectName("playerPlay")
        self.btn_play.clicked.connect(self.toggle_play)
        self.btn_stop = QPushButton("Parar")
        self.btn_stop.setToolTip("Parar e voltar ao início do trecho")
        self.btn_stop.clicked.connect(self.stop)
        self.btn_prev_sec = QPushButton("−1s")
        self.btn_prev_sec.setToolTip("Um segundo antes (←)")
        self.btn_prev_sec.clicked.connect(lambda: self.seek_relative(-1.0))
        self.btn_next_sec = QPushButton("+1s")
        self.btn_next_sec.setToolTip("Um segundo à frente (→)")
        self.btn_next_sec.clicked.connect(lambda: self.seek_relative(1.0))
        self.btn_prev_frame = QPushButton("−frame")
        self.btn_prev_frame.setToolTip("Um frame antes (Ctrl+←)")
        self.btn_prev_frame.clicked.connect(lambda: self.step_frame(-1))
        self.btn_next_frame = QPushButton("+frame")
        self.btn_next_frame.setToolTip("Um frame à frente (Ctrl+→)")
        self.btn_next_frame.clicked.connect(lambda: self.step_frame(1))

        self.slider = QSlider(Qt.Horizontal)
        self.slider.setRange(0, 0)
        self.slider.setSingleStep(100)
        self.slider.sliderPressed.connect(lambda: setattr(self, "_slider_down", True))
        self.slider.sliderReleased.connect(self._on_slider_released)
        self.slider.sliderMoved.connect(self._on_slider_moved)

        self.lbl_time = QLabel(f"{ts_short(0.0)} / —")
        self.lbl_time.setMinimumWidth(120)

        self.combo_speed = QComboBox()
        for value in SPEEDS:
            self.combo_speed.addItem(f"{value:g}×", value)
        self.combo_speed.setToolTip("Velocidade de reprodução (0,5× a 2×)")
        self.combo_speed.currentIndexChanged.connect(
            lambda _i: self.set_speed(self.combo_speed.currentData()))

        self.btn_mute = QPushButton("Mudo")
        self.btn_mute.setCheckable(True)
        self.btn_mute.setToolTip("Silenciar a trilha de áudio")
        self.btn_mute.toggled.connect(self.set_muted)
        self.slider_vol = QSlider(Qt.Horizontal)
        self.slider_vol.setRange(0, 100)
        self.slider_vol.setValue(80)
        self.slider_vol.setFixedWidth(84)
        self.slider_vol.valueChanged.connect(lambda v: self.set_volume(v / 100.0))

        self.chk_loop = QPushButton("Repetir trecho")
        self.chk_loop.setCheckable(True)
        self.chk_loop.setToolTip("Mantém a reprodução dentro do trecho do corte")
        self.chk_loop.toggled.connect(self._toggle_loop)

        self.btn_overlay = QPushButton("Guia 9:16")
        self.btn_overlay.setCheckable(True)
        self.btn_overlay.setChecked(True)
        self.btn_overlay.setToolTip("Mostrar/ocultar o guia de enquadramento")
        self.btn_overlay.toggled.connect(
            lambda on: self.area.overlay.setVisible(bool(on)))

        self.lbl_status = QLabel("")
        self.lbl_status.setWordWrap(True)
        self.lbl_status.setObjectName("playerStatus")

        transport = QHBoxLayout()
        transport.setContentsMargins(0, 0, 0, 0)
        for widget in (self.btn_play, self.btn_stop, self.btn_prev_sec,
                       self.btn_next_sec, self.btn_prev_frame, self.btn_next_frame):
            transport.addWidget(widget)
        transport.addSpacing(8)
        transport.addWidget(self.slider, 1)
        transport.addWidget(self.lbl_time)

        options = QHBoxLayout()
        options.setContentsMargins(0, 0, 0, 0)
        options.addWidget(QLabel("Velocidade"))
        options.addWidget(self.combo_speed)
        options.addSpacing(10)
        options.addWidget(self.btn_mute)
        options.addWidget(self.slider_vol)
        options.addSpacing(10)
        options.addWidget(self.chk_loop)
        options.addWidget(self.btn_overlay)
        options.addStretch(1)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        layout.addWidget(self.area, 1)
        layout.addLayout(transport)
        layout.addLayout(options)
        layout.addWidget(self.lbl_status)

        if not self._audio_ok:
            self.btn_mute.setEnabled(False)
            self.slider_vol.setEnabled(False)
            self.btn_mute.setToolTip("Sem backend de áudio neste ambiente")
            self.set_status("Sem saída de áudio neste ambiente — reprodução muda "
                            "(vídeo, seek e velocidade continuam reais).", "warn")


    # ------------------------------------------------------------------- state
    def set_status(self, text: str, level: str = "info") -> None:
        colour = {"info": _hex("text_dim"), "warn": _hex("warn"),
                  "error": _hex("err"), "ok": _hex("ok")}.get(level, _hex("text_dim"))
        self.lbl_status.setText(text or "")
        self.lbl_status.setStyleSheet(f"color: {colour};")

    def set_fps(self, fps: float) -> None:
        if fps and fps > 0:
            self._fps = float(fps)

    def fps(self) -> float:
        return self._fps

    def set_frame_size(self, width: int, height: int) -> None:
        """Source frame size, used to map crop rects onto the widget."""
        if width and height:
            self.area.overlay.set_source_size(int(width), int(height))
            self.area.overlay.update()

    def frame_size(self) -> QSize:
        return self.area.overlay.source_size()

    def set_source(self, path: Any, start: float = 0.0,
                   end: float | None = None) -> bool:
        """Load ``path`` and jump to ``start`` (optionally clamped at ``end``).

        Returns ``False`` when the file is missing; the caller can rely on the
        status line to have an honest explanation in Portuguese.
        """
        target = Path(str(path)) if path else None
        self._path = target
        self._pending_start = float(start or 0.0)
        self._last_crop = None
        self.area.poster.clear()
        self._poster_pending = False
        if target is None or not target.exists():
            self.set_status("Arquivo de vídeo não encontrado: "
                            f"{target.name if target else '(vazio)'}.", "error")
            self.mediaLoaded.emit(False)
            return False
        self._range = None if end is None else (float(start), float(end))
        if self._range is not None and self.chk_loop.isChecked():
            self.set_loop_range(self._range[0], self._range[1])
        else:
            self.clear_loop_range()
        self.set_status(f"Carregando {target.name}…", "info")
        self.player.setSource(QUrl.fromLocalFile(str(target)))
        return True

    def set_range(self, start: float, end: float) -> None:
        """Update the clip range without reloading the media."""
        self._range = (float(start), float(end))
        if self.chk_loop.isChecked():
            self.set_loop_range(start, end)

    def source_path(self) -> Path | None:
        return self._path

    def duration(self) -> float:
        """Real media duration in seconds (0.0 while nothing is loaded)."""
        return self._duration

    def position(self) -> float:
        return self.player.position() / 1000.0

    def range(self) -> tuple[float, float] | None:
        return self._range

    def is_playing(self) -> bool:
        return self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState

    def is_loaded(self) -> bool:
        return self._duration > 0


    # --------------------------------------------------------------- transport
    def play(self) -> None:
        if self._path is None:
            self.set_status("Nenhum vídeo carregado.", "warn")
            return
        self.player.play()

    def pause(self) -> None:
        self.player.pause()

    def stop(self) -> None:
        self.player.stop()
        self.seek(self._range[0] if self._range else 0.0)

    def toggle_play(self) -> None:
        if self.is_playing():
            self.pause()
        else:
            self.play()

    def seek(self, seconds: float) -> None:
        """Jump to an absolute source second, clamped to the real duration."""
        seconds = max(0.0, float(seconds))
        if self._duration > 0:
            seconds = min(self._duration, seconds)
        self.player.setPosition(int(round(seconds * 1000.0)))
        self._update_time(seconds)
        self.timeChanged.emit(seconds)

    def seek_relative(self, delta: float) -> None:
        self.seek(self.position() + float(delta))

    def step_frame(self, direction: int) -> None:
        """Move exactly one frame using the real fps of the source."""
        self.seek(self.position() + float(direction) / max(1.0, self._fps))

    def set_speed(self, factor: float) -> None:
        factor = max(0.25, min(4.0, float(factor)))
        self.player.setPlaybackRate(factor)
        index = self.combo_speed.findData(factor)
        if index >= 0 and self.combo_speed.currentData() != factor:
            self.combo_speed.setCurrentIndex(index)

    def speed(self) -> float:
        return float(self.player.playbackRate())

    def set_muted(self, muted: bool) -> None:
        self.btn_mute.blockSignals(True)
        self.btn_mute.setChecked(bool(muted))
        self.btn_mute.setText("Com áudio" if muted else "Mudo")
        self.btn_mute.blockSignals(False)
        if self.audio_output is not None:
            self.audio_output.setMuted(bool(muted))

    def is_muted(self) -> bool:
        return self.btn_mute.isChecked()

    def set_volume(self, volume: float) -> None:
        volume = max(0.0, min(1.0, float(volume)))
        if self.slider_vol.value() != int(volume * 100):
            self.slider_vol.blockSignals(True)
            self.slider_vol.setValue(int(volume * 100))
            self.slider_vol.blockSignals(False)
        if self.audio_output is not None:
            self.audio_output.setVolume(volume)

    def volume(self) -> float:
        if self.audio_output is not None:
            return float(self.audio_output.volume())
        return self.slider_vol.value() / 100.0

    # -------------------------------------------------------------- loop range
    def set_loop_range(self, t0: float, t1: float | None = None) -> None:
        """Repeat from ``t0`` to ``t1`` (seconds).

        With a single argument the end comes from the loaded clip range (or the
        media duration), matching the contract call ``set_loop_range(t0)``.
        """
        if t1 is None:
            start = float(t0)
            end = self._range[1] if self._range and self._range[1] > start else self._duration
        else:
            start, end = float(t0), float(t1)
        if end <= start:
            self.clear_loop_range()
            return
        self._loop = (start, end)
        self.chk_loop.blockSignals(True)
        self.chk_loop.setChecked(True)
        self.chk_loop.setText("Repetindo trecho")
        self.chk_loop.blockSignals(False)
        self.chk_loop.setToolTip(f"Repete {ts_short(start)} → {ts_short(end)}")

    def clear_loop_range(self) -> None:
        self._loop = None
        self.chk_loop.blockSignals(True)
        self.chk_loop.setChecked(False)
        self.chk_loop.setText("Repetir trecho")
        self.chk_loop.setToolTip("Mantém a reprodução dentro do trecho do corte")
        self.chk_loop.blockSignals(False)

    def loop_range(self) -> tuple[float, float] | None:
        return self._loop

    def _toggle_loop(self, on: bool) -> None:
        if not on:
            self.clear_loop_range()
        elif self._range:
            self.set_loop_range(self._range[0], self._range[1])
        else:
            self.chk_loop.blockSignals(True)
            self.chk_loop.setChecked(False)
            self.chk_loop.blockSignals(False)
            self.set_status("Carregue um corte (com início e fim) para repetir um trecho.")

    # -------------------------------------------------------------------- crop
    def set_crop_rect(self, rect: Any) -> None:
        """Darken the area outside ``rect`` (source pixels); ``None`` clears."""
        normalised = normalise_rect(rect)
        if normalised == self._last_crop:
            return
        self._last_crop = normalised
        self.area.overlay.set_crop_rect(normalised)

    def crop_rect(self) -> tuple[int, int, int, int] | None:
        return self.area.overlay.crop_rect()

    def set_crop_label(self, text: str) -> None:
        self.area.overlay.set_label(text)

    # ---------------------------------------------------------- qt signal slots
    def _on_position(self, ms: int) -> None:
        seconds = ms / 1000.0
        loop = self._loop
        if loop is not None and self.is_playing():
            if seconds >= loop[1] - 0.02:
                self.seek(loop[0])
                self.ended.emit(loop[1])
                return
            if seconds < loop[0] - 0.5:
                self.seek(loop[0])
                return
        if not self._slider_down and self.slider.maximum() > 0:
            self.slider.blockSignals(True)
            self.slider.setValue(int(ms))
            self.slider.blockSignals(False)
        self._update_time(seconds)
        self.timeChanged.emit(seconds)

    def _on_duration(self, ms: int) -> None:
        self._duration = max(0.0, ms / 1000.0)
        self.slider.setRange(0, max(0, int(ms)))
        self._update_time(self.position())

    def _on_status(self, status: Any) -> None:
        name = getattr(status, "name", str(status))
        if name in ("LoadedMedia", "BufferedMedia"):
            if self._pending_start:
                self.player.setPosition(int(round(self._pending_start * 1000)))
            self.mediaLoaded.emit(True)
            self.set_status(f"{self._path.name if self._path else ''} · "
                            f"{ts_short(self._duration)} · {self._source_note()}", "ok")
        elif name == "InvalidMedia":
            self.mediaLoaded.emit(False)
            self._fallback_to_poster("o formato não pôde ser aberto")
        elif name == "EndOfMedia":
            self._update_time(self._duration)

    def _on_state(self, state: Any) -> None:
        playing = getattr(state, "name", str(state)) == "PlayingState"
        self.btn_play.setText("Pausar" if playing else "Reproduzir")

    def _on_error(self, error: Any, text: str) -> None:
        if getattr(error, "name", str(error)) == "NoError":
            return
        reason = (text or "decodificação de vídeo indisponível").strip()
        self.mediaLoaded.emit(False)
        self.set_status(f"Não foi possível decodificar o vídeo ({reason}). "
                        "Mostrando um quadro real extraído pelo FFmpeg.", "error")
        self._fallback_to_poster(reason)

    def _on_video_frame(self, frame: Any) -> None:
        """Learn the real frame size from decoded frames (never from videoSize)."""
        try:
            width, height = int(frame.width()), int(frame.height())
        except Exception:                                  # noqa: BLE001
            return
        if width > 0 and height > 0 and self.area.overlay.source_size().width() != width:
            self.set_frame_size(width, height)
        if self._poster_pending:
            self._poster_pending = False
            self.area.poster.clear()

    def _on_slider_moved(self, value: int) -> None:
        self._update_time(value / 1000.0)

    def _on_slider_released(self) -> None:
        self._slider_down = False
        self.seek(self.slider.value() / 1000.0)

    # ------------------------------------------------------------------ helpers
    def _source_note(self) -> str:
        size = self.area.overlay.source_size()
        if size.width() and size.height():
            return f"{size.width()}×{size.height()} px"
        return "resolução ainda desconhecida"

    def _update_time(self, seconds: float) -> None:
        total = ts_short(self._duration) if self._duration > 0 else "—"
        self.lbl_time.setText(f"{ts_short(seconds)} / {total}")

    def _fallback_to_poster(self, reason: str) -> None:
        """Ask the worker pool for a real frame when the backend shows nothing."""
        if self._path is None:
            return
        self._poster_pending = True
        self.area.poster.set_caption(f"Sem decodificação de vídeo aqui ({reason}).\n"
                                     "Gerando quadro real com FFmpeg…")
        try:
            from corteia.workers.jobs import run_blocking_in_thread

            path = self._path
            at = self._range[0] if self._range else self._pending_start

            def work(progress):
                progress(0.1, "extraindo quadro")
                pixmap = load_poster_pixmap(path, at)
                progress(1.0, "quadro pronto")
                return pixmap

            run_blocking_in_thread(self.ctx, work, name="poster",
                                   on_done=self._poster_ready,
                                   on_error=self._poster_failed)
        except Exception:                                  # noqa: BLE001
            log.debug("poster fallback indisponível", exc_info=True)

    def _poster_ready(self, pixmap: Any) -> None:
        if isinstance(pixmap, QPixmap) and not pixmap.isNull():
            self.area.poster.set_pixmap(pixmap)
            self.area.poster.set_caption(
                "Quadro real extraído pelo FFmpeg — decodificação de vídeo "
                "indisponível neste ambiente")

    def _poster_failed(self, text: str) -> None:
        self.area.poster.set_caption(
            f"Sem decodificação de vídeo e o FFmpeg também falhou: {text}")

    def current_pixmap(self) -> QPixmap | None:
        """Best real image available right now (poster or video surface)."""
        shown = self.area.poster.pixmap()
        if shown is not None:
            return shown
        if self.area.video_widget is not None:
            try:
                return self.area.video_widget.grab()
            except Exception:                              # noqa: BLE001
                return None
        return None


# ------------------------------------------------------------------ poster tools
def poster_file(path: Path, at: float) -> Path:
    """Extract one real frame with FFmpeg (worker thread only).

    Prefers ``corteia.core.media.thumbnail`` because that owns the shared
    cache; the direct FFmpeg call keeps the player useful when the core module
    is not importable yet.
    """
    from corteia import paths

    dest = paths.cache_dir() / "thumbs" / (
        f"{path.stem}_{int(round(max(0.0, float(at)) * 10))}_poster.png")
    if dest.exists() and dest.stat().st_size > 0:
        return dest
    try:
        from corteia.core.media import thumbnail

        produced = thumbnail(path, at=float(at), size=640, dest=dest)
        if produced and Path(str(produced)).exists():
            return Path(str(produced))
    except Exception:                                      # noqa: BLE001
        log.debug("core.media.thumbnail indisponível", exc_info=True)
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("FFmpeg não encontrado para gerar o quadro de fallback")
    dest.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        [ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
         "-ss", f"{max(0.0, float(at)):.3f}", "-i", str(path),
         "-frames:v", "1", "-vf", "scale=640:-2", str(dest)],
        capture_output=True, text=True, timeout=120)
    if result.returncode != 0 or not dest.exists():
        raise RuntimeError((result.stderr or "FFmpeg falhou").strip()[-300:])
    return dest


def load_poster_pixmap(path: Path, at: float) -> QPixmap:
    """``poster_file`` plus decoding; safe from a worker thread."""
    from PySide6.QtGui import QImage

    image = QImage(str(poster_file(path, at)))
    if image.isNull():
        raise RuntimeError("O FFmpeg gerou uma imagem ilegível")
    return QPixmap.fromImage(image.copy())
