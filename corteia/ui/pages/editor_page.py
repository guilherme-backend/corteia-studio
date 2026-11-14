"""Página "Editor": prévia do recorte, timeline e quatro abas de ajustes.

* player real (``widgets.player.VideoPlayer``) com o retângulo do
  ``pipeline.crop_plan`` por cima — o que se vê é o ``crop`` que o FFmpeg fará;
* ``widgets.timeline.ClipTimeline`` com falas, silêncios removidos (lacunas entre
  ``TimelineMap.ranges``) e marcações de plano, alças de entrada/saída
  arrastáveis e repetição de faixa;
* abas Legenda / Áudio / Enquadramento / Identidade; tudo escreve em
  ``clip.edit`` + ``ctx.save()``;
* "Gerar prévia" renderiza ~6 s de proxy fora da GUI; "Exportar este corte" leva
  à fila.

Duas leituras de tempo convivem de propósito: posição na **fonte** (o que o
``QMediaPlayer`` entrega) e posição na **saída** (``TimelineMap.src_time_at``,
que desconta os silêncios cortados). Progresso do núcleo chega como fração
0..1 e é convertido para 0..100 na fronteira da UI.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from PySide6.QtCore import QUrl, Qt
from PySide6.QtGui import QDesktopServices, QPixmap
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog,
                               QFormLayout, QFrame, QHBoxLayout, QLabel,
                               QLineEdit, QPushButton, QSlider, QSpinBox,
                               QTabWidget, QVBoxLayout, QWidget)

from .. import theme
from ...i18n import t
from ...models import (AspectRatio, Clip, ExportSettings, Resolution,
                       SubtitleSettings, SubtitleStyle, format_ts)
from ..widgets.busy import get_runner
from ..widgets.player import VideoPlayer, ts_short
from ..widgets.progress import MiniBar
from ..widgets.segmented import SegmentedControl
from ..widgets.stylepanel import StylePanel
from ..widgets.subtitle_overlay import SubtitleOverlay
from ..widgets.switch import SwitchButton
from ..widgets.timeline import ClipTimeline

log = logging.getLogger("corteia.ui.editor")

ASPECTS = ((AspectRatio.VERTICAL_9_16.value, "9:16"),
           (AspectRatio.SQUARE_1_1.value, "1:1"),
           (AspectRatio.PORTRAIT_4_5.value, "4:5"),
           (AspectRatio.HORIZONTAL_16_9.value, "16:9"))
WM_POS = (("tl", "superior esquerdo"), ("tr", "superior direito"),
          ("bl", "inferior esquerdo"), ("br", "inferior direito"), ("c", "centro"))


def effective_range(clip: Any) -> tuple[float, float]:
    """Trecho efetivo: o ajuste fino do editor vence a sugestão automática.

    ``clip`` pode ser ``None``: ``clips_updated`` chega para o editor mesmo
    quando nada está aberto (projeto novo, corte apagado, sinal vindo de outra
    página).  Antes isso subia um ``AttributeError`` sem dono na thread da GUI.
    """
    if clip is None:
        return 0.0, 0.0
    edit = getattr(clip, "edit", None)
    if edit is None:
        return float(clip.start), float(max(clip.start + 0.2, clip.end))
    start = edit.trim_start if edit.trim_start is not None else clip.start
    end = edit.trim_end if edit.trim_end is not None else clip.end
    return float(start), float(max(start + 0.2, end))


def crop_at(plan: Any, seconds: float) -> tuple[int, int, int, int] | None:
    """Retângulo do ``CropPlan`` no instante ``seconds`` (keyframe anterior)."""
    points = getattr(plan, "points", None) or []
    if not points:
        return None
    best = points[0]
    for point in points:
        if float(point[0]) <= float(seconds):
            best = point
        else:
            break
    return (int(best[1]), int(best[2]), int(best[3]), int(best[4]))


class EditorPage(QWidget):
    """Editor de um corte: vídeo + régua + abas laterais."""

    def __init__(self, ctx: Any, clip_id: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self.clip_id = clip_id or self._first_clip_id()
        self._tmap: Any = None
        self._plan: Any = None
        self._cues: list[Any] = []
        self._job: Any = None
        self._preview_dest: Path | None = None
        self._warned_save = False
        self._blocked = True                     # suprime gravações ao carregar as abas

        root = QVBoxLayout(self)
        root.setContentsMargins(14, 12, 14, 10)
        root.setSpacing(8)
        root.addWidget(self._build_header())

        body = QHBoxLayout()
        body.setSpacing(12)
        left = QVBoxLayout()
        left.setSpacing(6)
        self.player = VideoPlayer(ctx, self)
        self.player.setMinimumHeight(250)
        left.addWidget(self.player, 1)
        self.timeline = ClipTimeline(ctx, self)
        left.addWidget(self.timeline)
        left.addWidget(self._build_status_row())
        body.addLayout(left, 5)

        self.tabs = QTabWidget(self)
        self.tabs.addTab(self._build_subs_tab(), t("Legenda"))
        self.tabs.addTab(self._build_audio_tab(), t("Áudio"))
        self.tabs.addTab(self._build_frame_tab(), t("Enquadramento"))
        self.tabs.addTab(self._build_identity_tab(), t("Identidade"))
        body.addWidget(self.tabs, 4)
        root.addLayout(body, 1)

        self.player.timeChanged.connect(self._on_time)
        self.timeline.seekRequested.connect(self.player.seek)
        self.timeline.trimChanged.connect(self._on_trim)
        self.timeline.loopChanged.connect(self._on_loop)
        ctx.signals.clip_updated.connect(self._on_clip_updated)
        ctx.signals.project_loaded.connect(lambda _p: self._reload())

    # ------------------------------------------------------------------ cabeçalho
    def _first_clip_id(self) -> str:
        clips = self.ctx.project.clips
        return clips[0].id if clips else ""

    def _build_header(self) -> QWidget:
        bar = QFrame(self)
        bar.setProperty("panel", "surface")
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(10, 6, 10, 6)
        lay.setSpacing(8)

        self.btn_back = QPushButton(t("Voltar"), bar)
        self.btn_back.setProperty("variant", "ghost")
        self.btn_back.setIcon(theme.icon("back", "muted", 16))
        self.btn_back.clicked.connect(lambda: self.ctx.go("clips"))

        self.combo_clip = QComboBox(bar)
        self.combo_clip.setToolTip(t("Outros cortes deste projeto"))
        self.combo_clip.currentIndexChanged.connect(self._on_combo)

        self.edit_title = QLineEdit(bar)
        self.edit_title.setPlaceholderText(t("Título do corte"))
        self.edit_title.editingFinished.connect(self._save_title)

        self.lbl_dur = QLabel("", bar)
        self.lbl_dur.setProperty("role", "mono")

        self.btn_preview = QPushButton(t("Gerar prévia"), bar)
        self.btn_preview.setProperty("variant", "outline")
        self.btn_preview.setIcon(theme.icon("video", "muted", 16))
        self.btn_preview.setToolTip(t("Renderiza ~6 s do recorte final com FFmpeg"))
        self.btn_preview.clicked.connect(self._preview)

        self.btn_reopen = QPushButton("", bar)
        self.btn_reopen.setProperty("variant", "ghost")
        self.btn_reopen.setIcon(theme.icon("folder", "muted", 16))
        self.btn_reopen.setToolTip(t("Abrir o arquivo no player do sistema"))
        self.btn_reopen.setVisible(False)
        self.btn_reopen.clicked.connect(self._open_preview)

        self.btn_export = QPushButton(t("Exportar este corte"), bar)
        self.btn_export.setProperty("variant", "primary")
        self.btn_export.setIcon(theme.icon("export", "on_accent", 16))
        self.btn_export.clicked.connect(lambda: self.ctx.go("export"))

        for widget in (self.btn_back, self.combo_clip, self.edit_title, self.lbl_dur,
                       self.btn_preview, self.btn_reopen, self.btn_export):
            lay.addWidget(widget)
        return bar

    def _build_status_row(self) -> QWidget:
        row = QFrame(self)
        row.setProperty("panel", "surface2")
        lay = QHBoxLayout(row)
        lay.setContentsMargins(10, 4, 10, 4)
        lay.setSpacing(10)
        self.lbl_src = QLabel(t("Fonte —"), row)
        self.lbl_src.setProperty("role", "mono")
        self.lbl_out = QLabel(t("Saída —"), row)
        self.lbl_out.setProperty("role", "mono")
        self.lbl_crop = QLabel(t("recorte: —"), row)
        self.lbl_crop.setProperty("role", "muted")
        self.progress = MiniBar(row)
        self.progress.setFixedWidth(140)
        self.progress.setVisible(False)
        self.btn_cancel_job = QPushButton(t("Cancelar"), row)
        self.btn_cancel_job.setProperty("variant", "danger")
        self.btn_cancel_job.setProperty("size", "sm")
        self.btn_cancel_job.setVisible(False)
        self.btn_cancel_job.clicked.connect(self._cancel_job)
        for widget in (self.lbl_src, self.lbl_out, self.lbl_crop, self.progress,
                       self.btn_cancel_job):
            lay.addWidget(widget)
        lay.addStretch(1)
        return row

    # ---------------------------------------------------------------- aba Legenda
    def _build_subs_tab(self) -> QWidget:
        page = QWidget(self)
        outer = QVBoxLayout(page)
        outer.setContentsMargins(6, 6, 6, 6)
        outer.setSpacing(6)

        self.style_panel = StylePanel(self.ctx, page)
        self.style_panel.settingsChanged.connect(self._on_style)
        self.style_panel.styleRequested.connect(self._on_style_preset)

        holder = QFrame(page)
        holder.setProperty("panel", "surface2")
        hlay = QVBoxLayout(holder)
        hlay.setContentsMargins(8, 8, 8, 8)
        head = QLabel(t("Prévia WYSIWYG"), holder)
        head.setProperty("role", "section")
        self.overlay = SubtitleOverlay(self.ctx, holder)
        self.overlay.setMinimumHeight(190)
        self.lbl_subs_info = QLabel("", holder)
        self.lbl_subs_info.setProperty("role", "faint")
        self.lbl_subs_info.setWordWrap(True)
        # queimar legenda é ajuste do ARQUIVO: vive na exportação, não no estilo
        self.chk_burn = QCheckBox(t("Queimar legendas no arquivo final"), holder)
        self.chk_burn.setToolTip(t("Desligado: o .ass é salvo junto, sem gravar na imagem"))
        self.chk_burn.toggled.connect(self._on_burn)
        hlay.addWidget(head)
        hlay.addWidget(self.overlay, 1)
        hlay.addWidget(self.lbl_subs_info)
        hlay.addWidget(self.chk_burn)

        outer.addWidget(self.style_panel)
        outer.addWidget(holder, 1)
        return page

    # ------------------------------------------------------------------ aba Áudio
    def _build_audio_tab(self) -> QWidget:
        page = QWidget(self)
        form = QFormLayout(page)
        form.setContentsMargins(10, 10, 10, 10)
        form.setVerticalSpacing(9)

        self.slider_volume = QSlider(Qt.Orientation.Horizontal, page)
        self.slider_volume.setRange(0, 200)
        self.slider_volume.setValue(100)
        self.lbl_volume = QLabel("100%", page)
        self.slider_volume.valueChanged.connect(self._on_volume)
        row = QHBoxLayout()
        row.addWidget(self.slider_volume, 1)
        row.addWidget(self.lbl_volume)
        form.addRow(t("Volume da fala"), row)

        self.lbl_music = QLabel(t("nenhuma música"), page)
        self.lbl_music.setProperty("role", "muted")
        btn_music = QPushButton(t("Escolher música…"), page)
        btn_music.setProperty("variant", "outline")
        btn_music.clicked.connect(self._pick_music)
        mrow = QHBoxLayout()
        mrow.addWidget(self.lbl_music, 1)
        mrow.addWidget(btn_music)
        form.addRow(t("Trilha de fundo"), mrow)

        self.slider_music = QSlider(Qt.Orientation.Horizontal, page)
        self.slider_music.setRange(0, 100)
        self.slider_music.setValue(18)
        self.lbl_music_vol = QLabel("18%", page)
        self.slider_music.valueChanged.connect(self._on_music_volume)
        vrow = QHBoxLayout()
        vrow.addWidget(self.slider_music, 1)
        vrow.addWidget(self.lbl_music_vol)
        form.addRow(t("Volume da música"), vrow)

        self.sw_duck = SwitchButton(t("Abaixar a música na fala"), True, page)
        self.sw_duck.checkedChanged.connect(self._on_duck)
        form.addRow(self.sw_duck)

        self.sw_silence = SwitchButton(t("Cortar silêncios"), True, page)
        self.sw_silence.setToolTip(t("A remontagem desconta as pausas do tempo final"))
        self.sw_silence.checkedChanged.connect(self._on_silences)
        form.addRow(self.sw_silence)
        return page

    # ----------------------------------------------------------- aba Enquadramento
    def _build_frame_tab(self) -> QWidget:
        page = QWidget(self)
        form = QFormLayout(page)
        form.setContentsMargins(10, 10, 10, 10)
        form.setVerticalSpacing(9)

        self.seg_aspect = SegmentedControl(ASPECTS, AspectRatio.VERTICAL_9_16.value,
                                           page, size="sm")
        self.seg_aspect.changed.connect(self._on_aspect)
        form.addRow(t("Proporção"), self.seg_aspect)

        self.spin_zoom = QDoubleSpinBox(page)
        self.spin_zoom.setRange(0.6, 3.0)
        self.spin_zoom.setSingleStep(0.05)
        self.spin_zoom.setValue(1.0)
        self.spin_zoom.valueChanged.connect(self._on_zoom)
        form.addRow(t("Zoom"), self.spin_zoom)

        self.spin_offset_x = QSpinBox(page)
        self.spin_offset_x.setRange(-2000, 2000)
        self.spin_offset_x.valueChanged.connect(self._on_offset)
        form.addRow(t("Deslocamento horizontal"), self.spin_offset_x)

        self.spin_offset_y = QSpinBox(page)
        self.spin_offset_y.setRange(-2000, 2000)
        self.spin_offset_y.valueChanged.connect(self._on_offset)
        form.addRow(t("Deslocamento vertical"), self.spin_offset_y)

        self.sw_follow = SwitchButton(t("Seguir o falante"), True, page)
        self.sw_follow.setToolTip(
            t("Desligado: o recorte é fixado no centro e salvo em posição manual"))
        self.sw_follow.checkedChanged.connect(self._on_follow)
        form.addRow(self.sw_follow)

        self.btn_recrop = QPushButton(t("Recalcular enquadramento"), page)
        self.btn_recrop.setProperty("variant", "outline")
        self.btn_recrop.setIcon(theme.icon("crop", "muted", 16))
        self.btn_recrop.clicked.connect(lambda: self._request_plan(force=True))
        form.addRow(self.btn_recrop)

        self.lbl_plan = QLabel(t("plano ainda não calculado"), page)
        self.lbl_plan.setProperty("role", "faint")
        self.lbl_plan.setWordWrap(True)
        form.addRow(t("Recorte automático"), self.lbl_plan)
        return page

    # -------------------------------------------------------------- aba Identidade
    def _build_identity_tab(self) -> QWidget:
        page = QWidget(self)
        form = QFormLayout(page)
        form.setContentsMargins(10, 10, 10, 10)
        form.setVerticalSpacing(9)

        self.edit_wm_text = QLineEdit(page)
        self.edit_wm_text.setPlaceholderText("@meucanal")
        self.edit_wm_text.editingFinished.connect(self._on_watermark)
        form.addRow(t("Marca d’água (texto)"), self.edit_wm_text)

        self.combo_wm_pos = QComboBox(page)
        for key, label in WM_POS:
            self.combo_wm_pos.addItem(t(label), key)
        self.combo_wm_pos.currentIndexChanged.connect(self._on_watermark)
        form.addRow(t("Posição da marca"), self.combo_wm_pos)

        self.slider_wm_opacity = QSlider(Qt.Orientation.Horizontal, page)
        self.slider_wm_opacity.setRange(5, 100)
        self.slider_wm_opacity.setValue(50)
        self.slider_wm_opacity.valueChanged.connect(self._on_watermark)
        form.addRow(t("Opacidade"), self.slider_wm_opacity)

        self.lbl_wm_image = QLabel(t("nenhuma imagem"), page)
        self.lbl_wm_image.setProperty("role", "muted")
        btn_image = QPushButton(t("Imagem…"), page)
        btn_image.setProperty("variant", "outline")
        btn_image.clicked.connect(self._pick_watermark_image)
        btn_clear = QPushButton(t("Limpar"), page)
        btn_clear.setProperty("variant", "ghost")
        btn_clear.clicked.connect(self._clear_watermark_image)
        irow = QHBoxLayout()
        irow.addWidget(self.lbl_wm_image, 1)
        irow.addWidget(btn_image)
        irow.addWidget(btn_clear)
        form.addRow(t("Logotipo"), irow)

        self.edit_title_text = QLineEdit(page)
        self.edit_title_text.setPlaceholderText(t("Gancho escrito na tela"))
        self.edit_title_text.editingFinished.connect(self._on_identity)
        form.addRow(t("Título na tela"), self.edit_title_text)

        self.combo_title_style = QComboBox(page)
        for key, label in (("bar", "Faixa"), ("plain", "Simples"), ("none", "Sem título")):
            self.combo_title_style.addItem(t(label), key)
        self.combo_title_style.currentIndexChanged.connect(self._on_identity)
        form.addRow(t("Estilo do título"), self.combo_title_style)
        return page

    # ------------------------------------------------------------------ carga
    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self._reload()

    def refresh(self) -> None:
        self._reload()

    def _reload(self) -> None:
        clips = self.ctx.project.clips
        ids = [c.id for c in clips]
        self.combo_clip.blockSignals(True)
        self.combo_clip.clear()
        for clip in clips:
            self.combo_clip.addItem(f"{int(clip.score.total or 0)} · "
                                    f"{clip.title or t('(sem título)')}", clip.id)
        self.combo_clip.blockSignals(False)
        if self.clip_id not in ids:
            self.clip_id = ids[0] if ids else ""
        if self.clip_id in ids:
            self.combo_clip.setCurrentIndex(ids.index(self.clip_id))
        self._apply_clip()

    def _on_combo(self, index: int) -> None:
        if index < 0:
            return
        value = self.combo_clip.itemData(index)
        if value and value != self.clip_id:
            self.clip_id = value
            self._apply_clip()

    def _on_clip_updated(self, clip_id: str) -> None:
        if clip_id == self.clip_id:
            t0, t1 = effective_range(self._clip())
            self.lbl_dur.setText(f"{format_ts(t0)} → {format_ts(t1)}")

    def _clip(self) -> Any:
        return self.ctx.clip(self.clip_id) if self.clip_id else None

    def _source(self, clip: Any) -> Any:
        """``SourceVideo`` com info/transcrição/silêncios/planos já lidos."""
        source = self.ctx.source_for(Path(clip.source_path))
        return source if source is not None else self.ctx.project.active_source()

    def _apply_clip(self) -> None:
        """Recarrega vídeo, régua e abas para o corte atual."""
        clip = self._clip()
        self._blocked = True
        if clip is None:
            self.player.set_status(t("Nenhum corte para editar — gere cortes primeiro."),
                                   "warn")
            self.timeline.set_segments([])
            self.overlay.set_cues([])
            self.btn_reopen.setVisible(False)
            self._blocked = False
            return
        path = Path(clip.source_path)
        t0, t1 = effective_range(clip)
        source = self._source(clip)
        info = getattr(source, "info", None)
        if info is not None:
            self.player.set_fps(info.fps or 0.0)
            self.player.set_frame_size(info.width, info.height)
        self.player.set_source(path, start=t0, end=t1)

        self.edit_title.setText(clip.title)
        self.lbl_dur.setText(f"{format_ts(t0)} → {format_ts(t1)}")
        has_output = bool(clip.exported_path) and Path(str(clip.exported_path)).is_file()
        self.btn_reopen.setVisible(has_output)
        self.btn_reopen.setToolTip(str(clip.exported_path or ""))

        # ----------------------------------------------------------------- régua
        self.timeline.set_source_length(float(getattr(info, "duration", 0.0) or 0.0))
        self.timeline.set_clip(t0, t1)
        transcript = getattr(source, "transcript", None) or self.ctx.transcript_for(path)
        self.timeline.set_segments(getattr(transcript, "segments", []) or [])
        self.timeline.set_scenes(getattr(source, "scenes", []) or [])
        try:
            self._tmap = self.ctx.pipeline.timeline_for(clip)
            self.timeline.set_removals(
                self.timeline.removed_spans(clip.start, clip.end, self._tmap.ranges))
        except Exception as exc:  # noqa: BLE001 — régua sem lacunas > janela quebrada
            log.debug("timeline_for indisponível: %s", exc)
            self._tmap = None
            self.timeline.set_removals([])
        thumb = str(getattr(source, "thumb", "") or "")
        self.timeline.set_thumbnail(QPixmap(thumb) if thumb and Path(thumb).is_file()
                                    else None)
        self.timeline.fit_clip()

        self._load_subs_tab(clip)
        self._load_audio_tab(clip)
        self._load_frame_tab(clip)
        self._load_identity_tab(clip)
        self._blocked = False
        self._request_plan(force=False)

    # -------------------------------------------------------------- carregar abas
    def _load_subs_tab(self, clip: Any) -> None:
        settings = clip.edit.subtitles or self._default_subs()
        self.chk_burn.blockSignals(True)
        self.chk_burn.setChecked(bool(self.ctx.project.export_settings.burn_subtitles))
        self.chk_burn.blockSignals(False)
        self.style_panel.set_settings(settings,
                                      burn=bool(self.ctx.project.export_settings
                                                .burn_subtitles))
        self._rebuild_cues(clip, settings)

    def _default_subs(self) -> SubtitleSettings:
        from corteia.core import subtitles as SUB        # fronteira de prévia

        style = self.ctx.project.options.subtitle_style or SubtitleStyle.VIRAL
        return SUB.preset(style)

    def _rebuild_cues(self, clip: Any, settings: SubtitleSettings | None = None) -> None:
        """Gera as cues e o ``.ass`` reais: o texto pré-visualizado é o do arquivo."""
        from corteia import paths
        from corteia.core import render, subtitles as SUB

        settings = settings or clip.edit.subtitles or self._default_subs()
        transcript = self.ctx.transcript_for(Path(clip.source_path))
        t0, t1 = effective_range(clip)
        aspect = clip.edit.aspect or self.ctx.project.export_settings.aspect
        try:
            out_w, out_h = render.output_size(self.ctx.project.export_settings, aspect)
        except Exception:  # noqa: BLE001 — tamanho só muda a escala da prévia
            out_w, out_h = 1080, 1920
        self.overlay.set_output_size(int(out_w), int(out_h))
        try:
            self._cues = SUB.cues(transcript, t0, t1, settings,
                                  clip.edit.manual_text_edits, self._tmap)
        except Exception as exc:  # noqa: BLE001
            log.info("cues indisponíveis: %s", exc)
            self._cues = []
        self.overlay.set_cues(self._cues)
        self.overlay.set_settings(settings)
        if self.player.current_pixmap() is not None:
            self.overlay.set_frame(self.player.current_pixmap())

        note = ""
        if settings.style is not SubtitleStyle.NONE and self._cues:
            try:
                dest = paths.temp_file(".ass", "subs")
                SUB.build_ass(transcript, t0, t1, settings, dest,
                              clip_w=int(out_w), clip_h=int(out_h),
                              edits=clip.edit.manual_text_edits, tmap=self._tmap)
                self.overlay.set_ass_path(dest)
                note = t("arquivo .ass real: {p}").format(p=Path(dest).name)
            except Exception as exc:  # noqa: BLE001
                note = t("não foi possível gerar o .ass: {e}").format(e=exc)
        else:
            self.overlay.set_ass_path("")
        counts = self.overlay.counts()
        self.lbl_subs_info.setText(
            f"{counts['cues']} {t('blocos')} · {counts['duration']:.1f} s"
            + (f" · {note}" if note else ""))

    def _load_audio_tab(self, clip: Any) -> None:
        edit = clip.edit
        volume = int(round(float(edit.volume) * 100))
        music = int(round(float(edit.music_volume) * 100))
        self.slider_volume.blockSignals(True)
        self.slider_volume.setValue(volume)
        self.slider_volume.blockSignals(False)
        self.lbl_volume.setText(f"{volume}%")
        self.slider_music.blockSignals(True)
        self.slider_music.setValue(music)
        self.slider_music.blockSignals(False)
        self.lbl_music_vol.setText(f"{music}%")
        self.lbl_music.setText(Path(str(edit.music_path)).name if edit.music_path
                               else t("nenhuma música"))
        self.sw_duck.setChecked(bool(edit.duck_music))
        self.sw_silence.setChecked(bool(edit.remove_silences))

    def _load_frame_tab(self, clip: Any) -> None:
        aspect = clip.edit.aspect or self.ctx.project.export_settings.aspect
        self.seg_aspect.blockSignals(True)
        self.seg_aspect.setValue(str(getattr(aspect, "value", aspect)))
        self.seg_aspect.blockSignals(False)
        self.spin_zoom.blockSignals(True)
        self.spin_zoom.setValue(float(clip.edit.zoom or 1.0))
        self.spin_zoom.blockSignals(False)
        self.spin_offset_x.blockSignals(True)
        self.spin_offset_x.setValue(int(clip.edit.crop_offset_x or 0))
        self.spin_offset_x.blockSignals(False)
        self.spin_offset_y.blockSignals(True)
        self.spin_offset_y.setValue(int(clip.edit.crop_offset_y or 0))
        self.spin_offset_y.blockSignals(False)
        self.sw_follow.setChecked(clip.edit.position_override is None)

    def _load_identity_tab(self, clip: Any) -> None:
        edit = clip.edit
        self.edit_wm_text.blockSignals(True)
        self.edit_wm_text.setText(edit.watermark_text or "")
        self.edit_wm_text.blockSignals(False)
        self.combo_wm_pos.blockSignals(True)
        self.combo_wm_pos.setCurrentIndex(max(0, self.combo_wm_pos.findData(
            edit.watermark_pos or "br")))
        self.combo_wm_pos.blockSignals(False)
        self.slider_wm_opacity.blockSignals(True)
        self.slider_wm_opacity.setValue(int(round(float(edit.watermark_opacity) * 100)))
        self.slider_wm_opacity.blockSignals(False)
        self.lbl_wm_image.setText(Path(str(edit.watermark_path)).name
                                  if edit.watermark_path else t("nenhuma imagem"))
        self.edit_title_text.blockSignals(True)
        self.edit_title_text.setText(edit.title_text or "")
        self.edit_title_text.blockSignals(False)
        self.combo_title_style.blockSignals(True)
        self.combo_title_style.setCurrentIndex(max(0, self.combo_title_style.findData(
            edit.title_style or "bar")))
        self.combo_title_style.blockSignals(False)

    # -------------------------------------------------------------------- gravar
    def _save(self) -> None:
        """Persiste sem quebrar a GUI (o histórico do núcleo pode estar em dict)."""
        self.ctx.signals.clip_updated.emit(self.clip_id)
        try:
            self.ctx.save()
        except Exception as exc:  # noqa: BLE001
            if not self._warned_save:
                self._warned_save = True
                log.warning("ctx.save falhou: %s", exc)
                self.ctx.toast(t("Ajustes aplicados, mas o projeto não pôde ser salvo "
                                 "(histórico de exportação incompatível)."), "warn")

    def _save_title(self) -> None:
        clip = self._clip()
        if clip is None or self._blocked:
            return
        text = self.edit_title.text().strip()
        if text and text != clip.title:
            clip.title = text
            self._save()

    # ---------------------------------------------------------------- handlers
    def _on_style(self, settings: SubtitleSettings) -> None:
        clip = self._clip()
        if clip is None or self._blocked:
            return
        clip.edit.subtitles = settings
        self._save()
        self._rebuild_cues(clip, settings)

    def _on_style_preset(self, value: str) -> None:
        """Busca o preset real do núcleo e recarrega o formulário nele."""
        clip = self._clip()
        if clip is None or self._blocked:
            return
        try:
            from corteia.core import subtitles as SUB

            preset = SUB.preset(SubtitleStyle(value))
        except Exception as exc:  # noqa: BLE001
            log.info("preset indisponível: %s", exc)
            return
        clip.edit.subtitles = preset
        self.style_panel.set_settings(preset, burn=self.chk_burn.isChecked())
        self._save()
        self._rebuild_cues(clip, preset)

    def _on_burn(self, on: bool) -> None:
        self.ctx.project.export_settings.burn_subtitles = bool(on)
        try:
            self.ctx.config.set("export.burn_subtitles", bool(on))
        except Exception:  # noqa: BLE001 - config é gentilmente opcional
            pass
        self._save()
        self.style_panel.set_burn(bool(on))
        self.ctx.toast(t("Queimar legendas: {s}").format(
            s=t("ligado") if on else t("desligado")), "info")

    def _on_volume(self, value: int) -> None:
        clip = self._clip()
        if clip is None or self._blocked:
            return
        self.lbl_volume.setText(f"{value}%")
        clip.edit.volume = value / 100.0
        self.player.set_volume(value / 100.0)
        self._save()

    def _on_music_volume(self, value: int) -> None:
        clip = self._clip()
        if clip is None or self._blocked:
            return
        self.lbl_music_vol.setText(f"{value}%")
        clip.edit.music_volume = value / 100.0
        self._save()

    def _pick_music(self) -> None:
        clip = self._clip()
        if clip is None:
            return
        path, _ = QFileDialog.getOpenFileName(
            self, t("Escolher música"), str(Path.home()),
            t("Áudio (*.mp3 *.wav *.m4a *.aac *.flac *.ogg)"))
        if path:
            clip.edit.music_path = path
            self.lbl_music.setText(Path(path).name)
            self._save()

    def _on_duck(self, on: bool) -> None:
        clip = self._clip()
        if clip is not None and not self._blocked:
            clip.edit.duck_music = bool(on)
            self._save()

    def _on_silences(self, on: bool) -> None:
        clip = self._clip()
        if clip is None or self._blocked:
            return
        clip.edit.remove_silences = bool(on)
        self._save()
        self._apply_clip()          # muda a remontagem: lacunas e cues recalculadas

    def _on_aspect(self, value: str) -> None:
        clip = self._clip()
        if clip is None or self._blocked:
            return
        clip.edit.aspect = AspectRatio(str(value))
        self._save()
        self._rebuild_cues(clip)
        self._request_plan(force=True)

    def _on_zoom(self, value: float) -> None:
        clip = self._clip()
        if clip is not None and not self._blocked:
            clip.edit.zoom = float(value)
            self._save()
            self._request_plan(force=True)

    def _on_offset(self, _value: int) -> None:
        clip = self._clip()
        if clip is None or self._blocked:
            return
        clip.edit.crop_offset_x = int(self.spin_offset_x.value())
        clip.edit.crop_offset_y = int(self.spin_offset_y.value())
        self._save()
        self._apply_crop_at(self.player.position())

    def _on_follow(self, on: bool) -> None:
        """``ClipEdit`` não tem campo "seguir falante": sem seguir, fixamos o recorte."""
        clip = self._clip()
        if clip is None or self._blocked:
            return
        if on:
            clip.edit.position_override = None
        else:
            info = getattr(self._source(clip), "info", None)
            if info is not None and info.width and info.height:
                aspect = clip.edit.aspect or self.ctx.project.export_settings.aspect
                ratio = float(aspect.value_w_h)
                if ratio >= 1.0:
                    height = int(info.height)
                    width = min(int(info.width), int(round(height * ratio)))
                else:
                    width = int(info.width)
                    height = min(int(info.height), int(round(width / ratio)))
                width, height = max(2, width // 2 * 2), max(2, height // 2 * 2)
                clip.edit.position_override = (
                    f"{(int(info.width) - width) // 2},{(int(info.height) - height) // 2},"
                    f"{width},{height}")
        self._save()
        self._request_plan(force=True)

    def _on_watermark(self, _value: Any = None) -> None:
        clip = self._clip()
        if clip is None or self._blocked:
            return
        clip.edit.watermark_text = self.edit_wm_text.text().strip()
        clip.edit.watermark_pos = str(self.combo_wm_pos.currentData() or "br")
        clip.edit.watermark_opacity = self.slider_wm_opacity.value() / 100.0
        self._save()

    def _on_identity(self, _value: Any = None) -> None:
        clip = self._clip()
        if clip is None or self._blocked:
            return
        clip.edit.title_text = self.edit_title_text.text().strip()
        clip.edit.title_style = str(self.combo_title_style.currentData() or "bar")
        self._save()

    def _pick_watermark_image(self) -> None:
        clip = self._clip()
        if clip is None:
            return
        path, _ = QFileDialog.getOpenFileName(
            self, t("Imagem da marca d’água"), str(Path.home()),
            t("Imagem (*.png *.jpg *.jpeg *.webp)"))
        if path:
            clip.edit.watermark_path = path
            self.lbl_wm_image.setText(Path(path).name)
            self._save()

    def _clear_watermark_image(self) -> None:
        clip = self._clip()
        if clip is None:
            return
        clip.edit.watermark_path = None
        self.lbl_wm_image.setText(t("nenhuma imagem"))
        self._save()

    # ---------------------------------------------------------------- tempo/crop
    def _on_time(self, seconds: float) -> None:
        clip = self._clip()
        if clip is None:
            return
        t0, _t1 = effective_range(clip)
        self.timeline.set_playhead(seconds)
        if self._tmap is not None:
            inside = self._tmap.src_to_clip(seconds) is not None
            self.lbl_src.setText(f"{t('Fonte')} {ts_short(seconds)}"
                                 + ("" if inside else f" · {t('silêncio cortado')}"))
            self.lbl_out.setText(f"{t('Saída')} {ts_short(self._tmap.src_time_at(seconds))}"
                                 f" · {t('corte')} {ts_short(max(0.0, seconds - t0))}")
            self.overlay.set_time(self._tmap.src_to_clip(seconds) or max(0.0, seconds - t0))
        else:
            self.lbl_src.setText(f"{t('Fonte')} {ts_short(seconds)}")
            self.lbl_out.setText(f"{t('Saída')} {ts_short(max(0.0, seconds - t0))}")
            self.overlay.set_time(max(0.0, seconds - t0))
        self._apply_crop_at(seconds)

    def _apply_crop_at(self, seconds: float) -> None:
        """Aplica o retângulo do plano (+ offsets/posição manual) no instante atual."""
        clip = self._clip()
        if clip is None:
            return
        rect = None
        manual = str(clip.edit.position_override or "")
        if manual:
            try:
                parts = [int(float(v)) for v in manual.split(",")]
                if len(parts) >= 4:
                    rect = (parts[0], parts[1], parts[2], parts[3])
            except ValueError:
                rect = None
        if rect is None:
            rect = crop_at(self._plan, seconds)
        if rect is None:
            self.lbl_crop.setText(t("recorte: —"))
            return
        rect = (int(rect[0]) + int(clip.edit.crop_offset_x or 0),
                int(rect[1]) + int(clip.edit.crop_offset_y or 0),
                int(rect[2]), int(rect[3]))
        self.player.set_crop_rect(rect)
        self.player.set_crop_label(t("recorte do corte"))
        self.lbl_crop.setText(f"{t('recorte')} {rect[2]}×{rect[3]} @ {rect[0]},{rect[1]}"
                             + (f" · {t('posição manual')}" if manual else ""))

    def _request_plan(self, *, force: bool = False) -> None:
        """Plano de recorte fora da GUI: detecção de rosto leva segundos."""
        clip = self._clip()
        if clip is None:
            return
        aspect = clip.edit.aspect or self.ctx.project.export_settings.aspect
        clip_id = clip.id

        def work(progress, cancel):
            return self.ctx.pipeline.crop_plan(
                clip, aspect,
                on_progress=lambda frac, msg="": progress(
                    max(0.0, min(100.0, float(frac) * 100.0)), str(msg)),
                cancel=cancel, force=force)

        def ready(plan: Any = None) -> None:
            if self.clip_id != clip_id:
                return
            self._plan_ready(plan)

        self._start_job(work, t("Calculando enquadramento"), ready)

    def _plan_ready(self, plan: Any = None) -> None:
        self._job_finished()
        self._plan = plan
        if plan is None or not getattr(plan, "points", []):
            self.lbl_plan.setText(t("recorte automático indisponível — usando o centro"))
            return
        self.lbl_plan.setText(f"{t('modo')} {plan.mode} · {len(plan.points)} "
                              f"{t('quadros-chave')}"
                              + (f" · {plan.note}" if plan.note else ""))
        self._apply_crop_at(self.player.position())

    def _on_trim(self, t0: float, t1: float) -> None:
        clip = self._clip()
        if clip is None or self._blocked:
            return
        clip.edit.trim_start = round(float(t0), 3)
        clip.edit.trim_end = round(float(t1), 3)
        self.player.set_range(float(t0), float(t1))
        self.lbl_dur.setText(f"{format_ts(t0)} → {format_ts(t1)}")
        self._save()
        self._rebuild_cues(clip)

    def _on_loop(self, span: Any) -> None:
        if span is None:
            self.player.clear_loop_range()
            self.ctx.toast(t("Repetição desligada."), "info")
            return
        self.player.set_loop_range(float(span[0]), float(span[1]))
        self.ctx.toast(t("Repetindo {a} → {b}").format(a=ts_short(span[0]),
                                                       b=ts_short(span[1])), "info")

    # ---------------------------------------------------------------------- jobs
    def _start_job(self, fn, label: str, on_done) -> None:
        self.progress.setVisible(True)
        self.progress.set_indeterminate(True)
        self.btn_cancel_job.setVisible(True)
        self.btn_cancel_job.setEnabled(True)
        self.btn_cancel_job.setText(t("Cancelar"))
        self.btn_preview.setEnabled(False)
        self._job = get_runner(self.ctx).run(fn, name=label, on_done=on_done,
                                             on_progress=self._job_progress,
                                             on_error=self._job_failed)

    def _job_progress(self, percent: float, label: str = "") -> None:
        self.progress.set_indeterminate(False)
        self.progress.set_percent(float(percent))
        if label:
            self.lbl_crop.setText(str(label))

    def _job_finished(self) -> None:
        self.progress.setVisible(False)
        self.progress.set_indeterminate(False)
        self.btn_cancel_job.setVisible(False)
        self.btn_preview.setEnabled(True)

    def _job_failed(self, text: str = "") -> None:
        self._job_finished()
        self.ctx.toast(t("Falha: ") + str(text), "error")

    def _cancel_job(self) -> None:
        if self._job is not None:
            self._job.cancel()
        try:
            self.ctx.pipeline.cancel()
        except Exception:  # noqa: BLE001 — cancelar nunca estoura na GUI
            log.debug("pipeline.cancel indisponível", exc_info=True)
        self.btn_cancel_job.setEnabled(False)
        self.btn_cancel_job.setText(t("Cancelando…"))

    # -------------------------------------------------------------------- prévia
    def _preview(self) -> None:
        clip = self._clip()
        if clip is None:
            self.ctx.toast(t("Selecione um corte primeiro."), "warn")
            return
        from corteia import paths
        from corteia.core import render

        dest = paths.temp_file(".mp4", "preview")

        def work(progress, cancel):
            def cb(frac: float, msg: Any = "") -> None:
                progress(max(0.0, min(100.0, float(frac) * 100.0)), str(msg))

            try:
                return render.preview_render(clip, dest, seconds=6.0, progress=cb,
                                             cancel=cancel)
            except NameError:
                # ``render.preview_render`` referencia ``Resolution`` sem importar
                # (bug do núcleo): refazemos o mesmo proxy por ``render_clip``.
                log.warning("preview_render indisponível; usando render_clip(proxy=True)")
                return self._preview_proxy(clip, dest, cb, cancel)

        self._preview_dest = None
        self._start_job(work, t("Gerando prévia"), self._preview_ready)

    def _preview_proxy(self, clip: Any, dest: Path, cb, cancel) -> Path:
        from corteia.core import media
        from corteia.core import render
        from corteia.core.timeline import TimelineMap

        source = Path(clip.source_path)
        info = getattr(self._source(clip), "info", None) or media.probe(source)
        t0, t1 = effective_range(clip)
        end = min(float(t1), t0 + 6.0)
        short = Clip(**{k: v for k, v in clip.to_dict().items() if k != "edit"})
        short.edit = clip.edit
        short.start, short.end = t0, end
        settings = ExportSettings(resolution=Resolution.R_720,
                                  fps=min(30.0, float(info.fps or 30.0)),
                                  aspect=clip.edit.aspect or AspectRatio.VERTICAL_9_16,
                                  quality_crf=32, preset="ultrafast")
        return render.render_clip(short, source=source, info=info,
                                  transcript=self.ctx.transcript_for(source),
                                  settings=settings, plan=None,
                                  tmap=TimelineMap.full(t0, end), out=Path(dest),
                                  progress=cb, cancel=cancel, proxy=True)

    def _preview_ready(self, result: Any = None) -> None:
        self._job_finished()
        dest = Path(str(result)) if result else None
        if dest is None or not dest.is_file():
            self.ctx.toast(t("A prévia não foi gerada."), "error")
            return
        self._preview_dest = dest
        self.btn_reopen.setVisible(True)
        self.btn_reopen.setToolTip(str(dest))
        poster = preview_poster(dest)
        if poster is not None:
            self.overlay.set_frame(poster)
        self.ctx.toast(t("Prévia pronta: {p}").format(p=dest.name), "ok")

    def _open_preview(self) -> None:
        clip = self._clip()
        target = self._preview_dest
        if (target is None or not Path(target).is_file()) and clip is not None \
                and clip.exported_path:
            target = Path(str(clip.exported_path))
        if target is None or not Path(target).is_file():
            self.ctx.toast(t("Gere a prévia primeiro."), "warn")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(target).resolve())))


def preview_poster(video: Path) -> QPixmap | None:
    """Quadro real da prévia, para a legenda aparecer sobre a imagem certa."""
    try:
        from corteia.core import media

        return QPixmap(str(media.thumbnail(video, at=0.5, size=360)))
    except Exception:  # noqa: BLE001 — pôster é enfeite, não função crítica
        return None
