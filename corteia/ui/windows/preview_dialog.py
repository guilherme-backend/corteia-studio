"""``PreviewDialog`` — prévia curta (proxy) do recorte final, renderizada de verdade.

Fluxo honesto: cliqque em "Gerar prévia" → ``render.preview_render`` roda ~6 s de
FFmpeg **dentro do JobRunner** (nunca na GUI), com ``BusyOverlay`` mostrando a
etapa e botão Cancelar que seta o evento cooperativo; ao terminar, o arquivo
gerado é aberto no ``QMediaPlayer`` do ``VideoPlayer``.

Fallback documentado: ``corteia/core/render.py::preview_render`` referencia
``Resolution`` sem importar o nome (``NameError``). Enquanto o núcleo não for
corrigido, reproduzimos exatamente o mesmo proxy chamando ``render_clip`` com
``proxy=True`` — mesmo grafo, mesmo resultado, só sem o atalho quebrado.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QDialog, QFileDialog, QHBoxLayout, QLabel,
                               QPushButton, QVBoxLayout, QWidget)

from .. import theme
from ...i18n import t
from ...models import AspectRatio, Clip, ExportSettings, Resolution, format_ts
from ..widgets.busy import BusyOverlay, run_job
from ..widgets.player import VideoPlayer
from ..widgets.progress import MiniBar

log = logging.getLogger("corteia.ui.preview")


class PreviewDialog(QDialog):
    """Diálogo modal leve: renderiza e reproduz o proxy do corte."""

    def __init__(self, ctx: Any, clip: Any = None, parent: QWidget | None = None,
                 seconds: float = 6.0) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self.clip = clip if isinstance(clip, Clip) or clip is not None else None
        self.seconds = float(seconds)
        self._dest: Path | None = None
        self._handle: Any = None

        self.setWindowTitle(t("Prévia do corte"))
        self.resize(520, 720)
        self.setMinimumSize(380, 480)

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(8)

        head = QHBoxLayout()
        self.lbl_title = QLabel("", self)
        self.lbl_title.setProperty("role", "page")
        self.lbl_title.setWordWrap(True)
        head.addWidget(self.lbl_title, 1)
        root.addLayout(head)

        self.player = VideoPlayer(ctx, self)
        self.player.setMinimumSize(300, 420)
        root.addWidget(self.player, 1)

        # overlay de progresso sobre o player (não-modal, com Cancelar)
        self.busy = BusyOverlay(self.player, ctx, self)
        self.busy.cancelRequested.connect(self.cancel)

        self.bar = MiniBar(self)
        self.bar.setVisible(False)
        root.addWidget(self.bar)

        self.lbl_info = QLabel(t("Gere a prévia para ver o recorte real."), self)
        self.lbl_info.setProperty("role", "muted")
        self.lbl_info.setWordWrap(True)
        root.addWidget(self.lbl_info)

        row = QHBoxLayout()
        self.btn_generate = QPushButton(t("Gerar prévia"), self)
        self.btn_generate.setProperty("variant", "primary")
        self.btn_generate.setIcon(theme.icon("video", "on_accent", 16))
        self.btn_generate.clicked.connect(self.render_preview)
        self.btn_cancel = QPushButton(t("Cancelar"), self)
        self.btn_cancel.setProperty("variant", "danger")
        self.btn_cancel.setVisible(False)
        self.btn_cancel.clicked.connect(self.cancel)
        self.btn_reveal = QPushButton(t("Mostrar arquivo"), self)
        self.btn_reveal.setProperty("variant", "ghost")
        self.btn_reveal.setIcon(theme.icon("folder", "muted", 16))
        self.btn_reveal.setVisible(False)
        self.btn_reveal.clicked.connect(self._reveal)
        self.btn_close = QPushButton(t("Fechar"), self)
        self.btn_close.setProperty("variant", "outline")
        self.btn_close.clicked.connect(self.close)
        for widget in (self.btn_generate, self.btn_cancel, self.btn_reveal, self.btn_close):
            row.addWidget(widget)
        root.addLayout(row)

        self.set_clip(self.clip)

    # ---------------------------------------------------------------------- API
    def set_clip(self, clip: Any) -> None:
        """Troca o corte mostrado (o diálogo pode ser reusado na lista)."""
        self.clip = clip
        if clip is None:
            self.lbl_title.setText(t("Nenhum corte selecionado"))
            self.btn_generate.setEnabled(False)
            return
        self.btn_generate.setEnabled(True)
        self.lbl_title.setText(f"{clip.title or t('(sem título)')} · "
                               f"{format_ts(clip.start)} → {format_ts(clip.end)}")
        self.player.set_status(t("Prévia ainda não gerada"), "info")

    def preview_path(self) -> Path | None:
        return self._dest

    # -------------------------------------------------------------------- render
    def render_preview(self) -> None:
        if self.clip is None:
            return
        from corteia import paths
        from corteia.core import render

        dest = paths.temp_file(".mp4", "preview")
        clip = self.clip

        def work(progress, cancel):
            def cb(frac: float, msg: Any = "") -> None:
                # fração do núcleo (0..1) -> 0..100 para a UI
                progress(max(0.0, min(100.0, float(frac) * 100.0)), str(msg))

            try:
                return render.preview_render(clip, dest, seconds=self.seconds,
                                             progress=cb, cancel=cancel)
            except NameError as exc:
                log.warning("preview_render quebrado no núcleo (%s); usando render_clip", exc)
                cb(1.0, t("gerando proxy"))
                return _proxy_render(self.ctx, clip, dest, self.seconds, cb, cancel)

        self.bar.setVisible(True)
        self.bar.set_indeterminate(True)
        self.btn_generate.setEnabled(False)
        self.btn_cancel.setVisible(True)
        self.btn_reveal.setVisible(False)
        self.lbl_info.setText(t("Renderizando ~{n} s de recorte com FFmpeg…").format(
            n=int(self.seconds)))
        self._handle = run_job(self.ctx, work, name=t("Prévia"),
                               busy_label=t("Gerando prévia"), overlay=self.busy,
                               on_done=self._done, on_error=self._failed)

    def _done(self, result: Any = None) -> None:
        self.btn_generate.setEnabled(True)
        self.btn_cancel.setVisible(False)
        self.bar.setVisible(False)
        dest = Path(str(result)) if result else None
        if dest is None or not dest.is_file():
            self.lbl_info.setText(t("A prévia não foi gerada."))
            self.player.set_status(t("Sem prévia"), "error")
            return
        self._dest = dest
        size = dest.stat().st_size
        self.lbl_info.setText(t("Arquivo: {p} · {kb}").format(
            p=dest.name, kb=f"{size / 1024:.0f} KB"))
        self.btn_reveal.setVisible(True)
        info = getattr(self.ctx.source_for(Path(self.clip.source_path)), "info", None)
        if info is not None:
            self.player.set_fps(info.fps or 0.0)
            self.player.set_frame_size(info.width, info.height)
        self.player.set_source(dest, start=0.0, end=None)
        self.player.play()

    def _failed(self, text: str = "") -> None:
        self.btn_generate.setEnabled(True)
        self.btn_cancel.setVisible(False)
        self.bar.setVisible(False)
        self.lbl_info.setText(t("Falha na prévia: ") + str(text))
        self.player.set_status(t("Prévia indisponível"), "error")

    def cancel(self) -> None:
        if self._handle is not None:
            self._handle.cancel()
        self.busy.set_detail(t("Cancelando…"), None)
        self.btn_cancel.setEnabled(False)
        self.btn_cancel.setText(t("Cancelando…"))

    def _reveal(self) -> None:
        if self._dest is None or not self._dest.is_file():
            return
        theme.open_location(str(self._dest), reveal=True)
        # alternativa: abrir no player padrão do sistema
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self._dest.resolve())))

    # ------------------------------------------------------------- atalho externo
    @staticmethod
    def open_for(ctx: Any, clip: Any, parent: QWidget | None = None,
                 seconds: float = 6.0) -> "PreviewDialog":
        dialog = PreviewDialog(ctx, clip, parent, seconds=seconds)
        dialog.render_preview()
        return dialog


def _proxy_render(ctx: Any, clip: Any, dest: Path, seconds: float,
                  cb, cancel) -> Path:
    """Mesmo proxy de ``preview_render``, montado na mão (contorna o bug do núcleo)."""
    from corteia.core import media, render
    from corteia.core.timeline import TimelineMap

    source = Path(clip.source_path)
    info = getattr(ctx.source_for(source), "info", None) or media.probe(source)
    t0 = clip.edit.trim_start if clip.edit.trim_start is not None else clip.start
    t1 = clip.edit.trim_end if clip.edit.trim_end is not None else clip.end
    end = min(float(t1), float(t0) + max(0.5, float(seconds)))
    short = Clip(**{k: v for k, v in clip.to_dict().items() if k != "edit"})
    short.edit = clip.edit
    short.start, short.end = float(t0), end
    settings = ExportSettings(resolution=Resolution.R_720,
                              fps=min(30.0, float(info.fps or 30.0)),
                              aspect=clip.edit.aspect or AspectRatio.VERTICAL_9_16,
                              quality_crf=32, preset="ultrafast")
    return render.render_clip(short, source=source, info=info,
                              transcript=ctx.transcript_for(source), settings=settings,
                              plan=None, tmap=TimelineMap.full(t0, end), out=Path(dest),
                              progress=cb, cancel=cancel, proxy=True)
