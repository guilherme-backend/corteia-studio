"""Página "Exportar": fila por corte, progresso real, estimativa e histórico.

Fonte da verdade do progresso: os sinais ``export_progress`` / ``export_state``
/ ``exports_finished`` do núcleo e ``ctx.exporter.queue`` (lista de
``ExportJob``); a página só espelha isso em linhas, uma por corte.

A estimativa (MB e minutos) vem de ``corteia.core.exporter.estimate``, que faz
``ffprobe`` e por isso roda no ``JobRunner``, nunca na GUI. O histórico lê
``ctx.project.exports``: o núcleo grava linhas como **dict** (chaves ``out`` e
``at``), então convertemos para o formato de ``widgets.cards.HistoryRow``
(``path`` e ``finished_at``).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QCheckBox, QComboBox, QFileDialog, QFrame, QHBoxLayout,
                               QLabel, QLineEdit, QPushButton, QScrollArea, QSpinBox,
                               QVBoxLayout, QWidget)

from .. import theme
from ...i18n import t
from ...models import AspectRatio, Codec, JobState, Resolution, format_ts
from ..widgets.busy import get_runner
from ..widgets.cards import EmptyState, HistoryRow
from ..widgets.progress import MiniBar
from ..widgets.segmented import SegmentedControl
from ..widgets.switch import SwitchButton

log = logging.getLogger("corteia.ui.export")

RESOLUTIONS = (("720p", "720p"), ("1080p", "1080p"), ("4K", "4K (2160p)"))
CODECS = (("libx264", "H.264 (libx264)"), ("libx265", "H.265 (libx265)"))
PRESETS = ("ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow")
BITRATES = ("128k", "160k", "192k", "256k", "320k")
FPS = (24, 25, 30, 50, 60)
ASPECTS = ((AspectRatio.VERTICAL_9_16.value, "9:16"), (AspectRatio.SQUARE_1_1.value, "1:1"),
           (AspectRatio.PORTRAIT_4_5.value, "4:5"),
           (AspectRatio.HORIZONTAL_16_9.value, "16:9"))
STATE_PT = {"pending": "na fila", "running": "exportando", "done": "concluído",
            "failed": "falhou", "cancelled": "cancelado"}
PILL = {"pending": "idle", "running": "busy", "done": "ok", "failed": "error",
        "cancelled": "warn"}


class ExportPage(QWidget):
    """Fila de exportação com progresso por arquivo e histórico consultável."""

    def __init__(self, ctx: Any, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self._rows: dict[str, dict[str, Any]] = {}
        self._running = False
        self._job: Any = None
        self._warned_save = False

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 12, 16, 10)
        root.setSpacing(8)

        head = QHBoxLayout()
        title = QLabel(t("Exportar cortes"), self)
        title.setProperty("role", "title")
        self.lbl_estimate = QLabel(t("calculando tamanho…"), self)
        self.lbl_estimate.setProperty("role", "muted")
        self.lbl_estimate.setWordWrap(True)
        head.addWidget(title)
        head.addSpacing(10)
        head.addWidget(self.lbl_estimate, 1)
        self.btn_estimate = QPushButton(t("Refazer estimativa"), self)
        self.btn_estimate.setProperty("variant", "ghost")
        self.btn_estimate.setProperty("size", "sm")
        self.btn_estimate.clicked.connect(self._estimate)
        head.addWidget(self.btn_estimate)
        root.addLayout(head)

        body = QHBoxLayout()
        body.setSpacing(12)
        body.addWidget(self._build_settings(), 0)
        right = QVBoxLayout()
        right.setSpacing(8)
        right.addWidget(self._build_queue())
        right.addWidget(self._build_actions())
        right.addWidget(self._build_history(), 1)
        body.addLayout(right, 1)
        root.addLayout(body, 1)

        # ------------------------------------------------------- sinais do núcleo
        ctx.signals.export_progress.connect(self._on_progress)
        ctx.signals.export_state.connect(self._on_state)
        ctx.signals.exports_finished.connect(self._on_finished)
        ctx.signals.clips_changed.connect(self._rebuild_queue)
        ctx.signals.history_changed.connect(self._rebuild_history)

    # ------------------------------------------------------------------- ajustes
    def _build_settings(self) -> QWidget:
        settings = self.ctx.project.export_settings
        box = QFrame(self)
        box.setProperty("panel", "surface")
        outer = QVBoxLayout(box)
        outer.setContentsMargins(12, 10, 12, 10)
        outer.setSpacing(6)
        box.setFixedWidth(330)

        scroll = QScrollArea(box)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        host = QWidget(scroll)
        form = QVBoxLayout(host)
        form.setSpacing(7)
        outer.addWidget(scroll, 1)
        scroll.setWidget(host)

        caption = QLabel(t("Ajustes do arquivo"), box)
        caption.setProperty("role", "section")
        outer.insertWidget(0, caption)

        def field(label: str, widget: QWidget) -> None:
            row = QVBoxLayout()
            row.setSpacing(2)
            text = QLabel(label, host)
            text.setProperty("role", "muted")
            row.addWidget(text)
            row.addWidget(widget)
            form.addLayout(row)

        self.combo_res = QComboBox(host)
        for value, label in RESOLUTIONS:
            self.combo_res.addItem(label, value)
        self.combo_res.setCurrentIndex(max(0, self.combo_res.findData(
            str(getattr(settings.resolution, "value", settings.resolution)))))
        self.combo_res.currentIndexChanged.connect(lambda _i: self._apply())
        field(t("Resolução"), self.combo_res)

        self.combo_codec = QComboBox(host)
        for value, label in CODECS:
            self.combo_codec.addItem(label, value)
        self.combo_codec.setCurrentIndex(max(0, self.combo_codec.findData(
            str(getattr(settings.codec, "value", settings.codec)))))
        self.combo_codec.setToolTip(t("H.265 codifica mais rápido em GPU (NVENC) e cabe em menos MB"))
        self.combo_codec.currentIndexChanged.connect(lambda _i: self._apply())
        field(t("Codificador"), self.combo_codec)

        self.spin_crf = QSpinBox(host)
        self.spin_crf.setRange(0, 51)
        self.spin_crf.setValue(int(settings.quality_crf))
        self.spin_crf.setToolTip(t("Menor número = mais qualidade e mais MB (20 é um bom padrão)"))
        self.spin_crf.valueChanged.connect(lambda _v: self._apply())
        field(t("Qualidade (CRF)"), self.spin_crf)

        self.combo_preset = QComboBox(host)
        for name in PRESETS:
            self.combo_preset.addItem(name)
        self.combo_preset.setCurrentText(str(settings.preset))
        self.combo_preset.setToolTip(t("Mais rápido = arquivo maior para a mesma qualidade"))
        self.combo_preset.currentIndexChanged.connect(lambda _i: self._apply())
        field(t("Preset de velocidade"), self.combo_preset)

        self.combo_fps = QComboBox(host)
        for value in FPS:
            self.combo_fps.addItem(f"{value} fps", value)
        self.combo_fps.setCurrentIndex(max(0, self.combo_fps.findData(int(settings.fps))))
        self.combo_fps.currentIndexChanged.connect(lambda _i: self._apply())
        field(t("Quadros por segundo"), self.combo_fps)

        self.combo_audio = QComboBox(host)
        for value in BITRATES:
            self.combo_audio.addItem(value, value)
        self.combo_audio.setCurrentIndex(max(0, self.combo_audio.findData(
            str(settings.audio_bitrate))))
        self.combo_audio.currentIndexChanged.connect(lambda _i: self._apply())
        field(t("Áudio"), self.combo_audio)

        self.seg_aspect = SegmentedControl(ASPECTS,
                                           str(getattr(settings.aspect, "value",
                                                       AspectRatio.VERTICAL_9_16.value)),
                                           host, size="sm")
        self.seg_aspect.changed.connect(lambda _v: self._apply())
        field(t("Proporção do arquivo"), self.seg_aspect)

        self.sw_burn = SwitchButton(t("Queimar legendas"), bool(settings.burn_subtitles), host)
        self.sw_burn.checkedChanged.connect(self._on_burn)
        form.addWidget(self.sw_burn)

        self.sw_faststart = SwitchButton(t("mp4 otimizado para web (faststart)"),
                                         bool(settings.faststart), host)
        self.sw_faststart.checkedChanged.connect(lambda _v: self._apply())
        form.addWidget(self.sw_faststart)

        self.spin_parallel = QSpinBox(host)
        self.spin_parallel.setRange(1, 8)
        self.spin_parallel.setValue(int(settings.max_parallel))
        self.spin_parallel.setToolTip(t("Quantos cortes codificar ao mesmo tempo"))
        self.spin_parallel.valueChanged.connect(lambda _v: self._apply())
        field(t("Exportações paralelas"), self.spin_parallel)

        # ------------------------------------------------------------- pasta/nome
        dir_row = QHBoxLayout()
        self.edit_dir = QLineEdit(host)
        self.edit_dir.setText(str(settings.out_dir or self.ctx.config.str_("export.dir")))
        self.edit_dir.setPlaceholderText(t("Pasta dos arquivos finais"))
        self.edit_dir.editingFinished.connect(lambda _=None: self._apply())
        btn_dir = QPushButton(t("Pasta…"), host)
        btn_dir.setProperty("variant", "outline")
        btn_dir.clicked.connect(self._pick_dir)
        dir_row.addWidget(self.edit_dir, 1)
        dir_row.addWidget(btn_dir)
        wrap = QVBoxLayout()
        label = QLabel(t("Pasta de saída"), host)
        label.setProperty("role", "muted")
        wrap.addWidget(label)
        wrap.addLayout(dir_row)
        form.addLayout(wrap)

        self.edit_pattern = QLineEdit(host)
        self.edit_pattern.setText(str(settings.filename_pattern))
        self.edit_pattern.setPlaceholderText("{index:02d}-{title}-corteia")
        self.edit_pattern.setToolTip(t("Válidos: {index} {title} {start} {end} {dur} {project} {score}"))
        self.edit_pattern.editingFinished.connect(lambda _=None: self._apply())
        field(t("Padrão de nome"), self.edit_pattern)

        btn_open = QPushButton(t("Abrir pasta de saída"), host)
        btn_open.setProperty("variant", "ghost")
        btn_open.setIcon(theme.icon("folder", "muted", 16))
        btn_open.clicked.connect(self._open_out_dir)
        form.addWidget(btn_open)
        form.addStretch(1)
        return box

    # ------------------------------------------------------------------ fila
    def _build_queue(self) -> QWidget:
        box = QFrame(self)
        box.setProperty("panel", "surface")
        lay = QVBoxLayout(box)
        lay.setContentsMargins(12, 10, 12, 10)
        lay.setSpacing(6)

        head = QHBoxLayout()
        caption = QLabel(t("Fila de exportação"), box)
        caption.setProperty("role", "section")
        self.lbl_queue = QLabel("", box)
        self.lbl_queue.setProperty("role", "muted")
        head.addWidget(caption)
        head.addWidget(self.lbl_queue, 1)
        self.btn_all = QPushButton(t("Selecionar aprovados"), box)
        self.btn_all.setProperty("variant", "ghost")
        self.btn_all.setProperty("size", "sm")
        self.btn_all.clicked.connect(self._select_approved)
        head.addWidget(self.btn_all)
        lay.addLayout(head)

        self.queue_host = QWidget(box)
        self.queue_lay = QVBoxLayout(self.queue_host)
        self.queue_lay.setContentsMargins(0, 0, 0, 0)
        self.queue_lay.setSpacing(4)
        self.empty = EmptyState(t("Nada na fila"),
                                t("Aprove cortes na aba Cortes para exportá-los aqui."),
                                icon="export", parent=self.queue_host)
        self.empty.set_action(t("Ir para Cortes"), lambda: self.ctx.go("clips"))
        self.queue_lay.addWidget(self.empty)

        scroll = QScrollArea(box)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(self.queue_host)
        scroll.setMaximumHeight(260)
        lay.addWidget(scroll)
        return box

    def _build_actions(self) -> QWidget:
        row = QFrame(self)
        row.setProperty("panel", "surface2")
        lay = QHBoxLayout(row)
        lay.setContentsMargins(12, 8, 12, 8)
        lay.setSpacing(8)
        self.btn_export = QPushButton(t("Exportar seleção"), row)
        self.btn_export.setProperty("variant", "primary")
        self.btn_export.setIcon(theme.icon("export", "on_accent", 16))
        self.btn_export.clicked.connect(self._start)
        self.btn_cancel = QPushButton(t("Cancelar"), row)
        self.btn_cancel.setProperty("variant", "danger")
        self.btn_cancel.setVisible(False)
        self.btn_cancel.clicked.connect(self._cancel)
        self.btn_reveal = QPushButton(t("Abrir última saída"), row)
        self.btn_reveal.setProperty("variant", "ghost")
        self.btn_reveal.setIcon(theme.icon("folder", "muted", 16))
        self.btn_reveal.clicked.connect(self._reveal_last)
        self.lbl_total = QLabel("", row)
        self.lbl_total.setProperty("role", "muted")
        lay.addWidget(self.btn_export)
        lay.addWidget(self.btn_cancel)
        lay.addWidget(self.btn_reveal)
        lay.addWidget(self.lbl_total, 1)
        return row

    def _build_history(self) -> QWidget:
        box = QFrame(self)
        box.setProperty("panel", "surface")
        lay = QVBoxLayout(box)
        lay.setContentsMargins(12, 10, 12, 10)
        lay.setSpacing(6)
        caption = QLabel(t("Últimas exportações"), box)
        caption.setProperty("role", "section")
        lay.addWidget(caption)

        self.hist_host = QWidget(box)
        self.hist_lay = QVBoxLayout(self.hist_host)
        self.hist_lay.setContentsMargins(0, 0, 0, 0)
        self.hist_lay.setSpacing(4)
        self.hist_empty = QLabel(t("nenhuma exportação neste projeto ainda"), box)
        self.hist_empty.setProperty("role", "faint")
        self.hist_lay.addWidget(self.hist_empty)
        self.hist_lay.addStretch(1)

        scroll = QScrollArea(box)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(self.hist_host)
        lay.addWidget(scroll, 1)
        return box

    # ------------------------------------------------------------- fila dinâmica
    def refresh(self) -> None:
        self._rebuild_queue()
        self._rebuild_history()
        self._estimate()

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self._rebuild_queue()
        self._rebuild_history()

    def _queue_clips(self) -> list:
        return [c for c in self.ctx.project.clips
                if str(getattr(c.status, "value", c.status)) in ("approved", "edited")
                or bool(c.exported_path)]

    def _rebuild_queue(self) -> None:
        clips = self._queue_clips()
        self.lbl_queue.setText(t("{n} na fila").format(n=len(clips)) if clips
                               else t("fila vazia"))
        self.empty.setVisible(not clips)
        seen: set[str] = set()
        for clip in clips:
            seen.add(clip.id)
            row = self._rows.get(clip.id)
            if row is None:
                row = self._make_row(clip)
                self._rows[clip.id] = row
                self.queue_lay.insertWidget(self.queue_lay.count() - 1, row["frame"])
            row["title"].setText(f"{clip.title or t('(sem título)')} · "
                                 f"{format_ts(clip.start)}→{format_ts(clip.end)}")
            job = self._job_for(clip.id)
            if job is not None:
                self._paint_row(row, str(getattr(job.state, "value", job.state)),
                                float(job.progress or 0.0), str(job.message or ""),
                                str(job.error or ""))
        for cid, row in list(self._rows.items()):
            if cid not in seen:
                row["frame"].setVisible(False)
        self._update_total()

    def _make_row(self, clip) -> dict[str, Any]:
        frame = QFrame(self.queue_host)
        frame.setProperty("card", "flat")
        lay = QHBoxLayout(frame)
        lay.setContentsMargins(8, 5, 8, 5)
        lay.setSpacing(8)
        check = QCheckBox(frame)
        check.setToolTip(t("Incluir este corte na próxima exportação"))
        check.setChecked(True)
        title = QLabel("", frame)
        title.setProperty("role", "value")
        bar = MiniBar(frame)
        bar.setFixedWidth(120)
        state = QLabel(t("na fila"), frame)
        state.setProperty("pill", "idle")
        msg = QLabel("", frame)
        msg.setProperty("role", "faint")
        msg.setMinimumWidth(150)
        btn = QPushButton(t("Abrir"), frame)
        btn.setProperty("variant", "ghost")
        btn.setProperty("size", "sm")
        btn.setVisible(False)
        btn.clicked.connect(lambda _c=False, p="": self._open_path(self._rows[clip.id]["path"]
                                                                   .text()))
        for widget in (check, title, bar, state, msg, btn):
            lay.addWidget(widget)
        lay.addStretch(1)
        return {"frame": frame, "check": check, "title": title, "bar": bar,
                "state": state, "msg": msg, "btn": btn, "path": QLabel("", frame)}

    def _job_for(self, clip_id: str):
        for job in list(getattr(self.ctx.exporter, "queue", []) or []):
            if job.clip_id == clip_id:
                return job
        return None

    # ------------------------------------------------------------- progresso vivo
    def _on_progress(self, clip_id: str, frac: float, message: str) -> None:
        row = self._rows.get(clip_id)
        if row is None:
            self._rebuild_queue()
            row = self._rows.get(clip_id)
            if row is None:
                return
        self._paint_row(row, "running", float(frac), str(message), "")
        self._update_total()

    def _on_state(self, clip_id: str, state: str) -> None:
        row = self._rows.get(clip_id)
        if row is None:
            return
        job = self._job_for(clip_id)
        self._paint_row(row, str(state), float(getattr(job, "progress", 0.0) or 0.0),
                        str(getattr(job, "message", "") or ""),
                        str(getattr(job, "error", "") or ""))
        self._rebuild_history()

    def _on_finished(self, ok: int, failed: int) -> None:
        """Sinal global ``exports_finished`` do núcleo.

        A exportação pode terminar sem passar por esta página — pelo Editor ou
        pela linha de comando.  Aqui apenas espelhamos o estado real; o aviso ao
        usuário fica em ``_done`` para não duplicar o toast.
        """
        self._rebuild_queue()
        self._rebuild_history()
        if getattr(self, "_running", False):
            self._finish_job()
        self._update_total()
        log.info("exports_finished: %s ok, %s falhas", ok, failed)

    def _paint_row(self, row: dict[str, Any], state: str, frac: float,
                   message: str, error: str) -> None:
        """Espelha o estado do ``ExportJob`` na linha (fração 0..1 → 0..100)."""
        percent = max(0.0, min(1.0, float(frac))) * 100.0
        if state == "running":
            row["bar"].set_indeterminate(False)
            row["bar"].set_percent(percent)
        elif state == "pending":
            row["bar"].set_indeterminate(True)
        else:
            row["bar"].set_indeterminate(False)
            row["bar"].set_percent(100.0 if state == "done" else 0.0)
            row["bar"].set_role("ok" if state == "done"
                                else "error" if state == "failed"
                                else "warn" if state == "cancelled" else "")
        row["state"].setText(t(STATE_PT.get(state, state)))
        row["state"].setProperty("pill", PILL.get(state, "idle"))
        self._repolish(row["state"])
        text = error or message
        row["msg"].setText(text[:60])
        row["msg"].setToolTip(text)
        row["msg"].setProperty("role", "error" if error else "faint")
        self._repolish(row["msg"])

    @staticmethod
    def _repolish(widget: QWidget) -> None:
        widget.style().unpolish(widget)
        widget.style().polish(widget)
        widget.update()

    def _update_total(self) -> None:
        running = sum(1 for row in self._rows.values()
                      if str(row["state"].property("pill")) == "busy")
        done = sum(1 for row in self._rows.values()
                   if str(row["state"].property("pill")) == "ok")
        self.lbl_total.setText(t("{d} concluído(s) · {r} em andamento").format(
            d=done, r=running) if self._running else
            t("{d} de {n} prontos").format(d=done, n=len(self._rows)))

    # ---------------------------------------------------------------- estimativa
    def _estimate(self) -> None:
        clips = [clip for clip in self._queue_clips()
                 if self._rows.get(clip.id, {}).get("check") is None
                 or self._rows[clip.id]["check"].isChecked()]

        def work(progress, cancel):
            from corteia.core.exporter import estimate      # fronteira permitida

            progress(30.0, t("Medindo cortes"))
            return estimate(self.ctx, clips or None)

        get_runner(self.ctx).run(work, name=t("Estimativa"), on_done=self._show_estimate,
                                 on_error=self._estimate_failed)

    def _show_estimate(self, data: Any = None) -> None:
        if not isinstance(data, dict):
            return
        mb = float(data.get("mb", 0.0) or 0.0)
        minutes = float(data.get("minutes", 0.0) or 0.0)
        speed = float(data.get("speed", 0.0) or 0.0)
        base = t("{n} corte(s) · ~{mb} MB · ~{min} de codificação").format(
            n=int(data.get("clips", 0) or 0), mb=f"{mb:.1f}", min=f"{minutes:.1f} min")
        if data.get("measured"):
            base += t(" (ritmo medido nesta máquina: {s}x)").format(s=f"{speed:.2f}")
        else:
            base += t(" (ritmo presumido: {s}x)").format(s=f"{speed:.2f}")
        self.lbl_estimate.setText(base)
        self.lbl_estimate.setToolTip(t("Pasta: {p}").format(p=str(data.get("out_dir", ""))))

    def _estimate_failed(self, text: str = "") -> None:
        self.lbl_estimate.setText(t("Estimativa indisponível: ") + str(text))

    # -------------------------------------------------------------------- export
    def _selected(self) -> list:
        out = []
        for clip in self._queue_clips():
            row = self._rows.get(clip.id)
            if row is None or row["check"].isChecked():
                out.append(clip)
        return out

    def _select_approved(self) -> None:
        for clip in self._queue_clips():
            row = self._rows.get(clip.id)
            if row is not None:
                row["check"].setChecked(True)
        self._update_total()

    def _start(self) -> None:
        if self._running:
            return
        clips = self._selected()
        if not clips:
            self.ctx.toast(t("Selecione ao menos um corte aprovado."), "warn")
            return
        self._running = True
        self.btn_export.setEnabled(False)
        self.btn_export.setText(t("Exportando…"))
        self.btn_cancel.setVisible(True)
        for clip in clips:
            self._paint_row(self._rows[clip.id], "pending", 0.0, t("na fila"), "")
        names = ", ".join((c.title or c.id) for c in clips[:3])

        def work(progress, cancel):
            def cb(clip_id, frac, msg=""):
                progress(max(0.0, min(100.0, float(frac) * 100.0)), str(msg))

            exporter = self.ctx.exporter
            if len(clips) == 1:
                return {"ok": 1, "failed": 0, "outputs": [exporter.export_one(clips[0].id)]}
            return exporter.export_all(clips=clips, on_progress=cb)

        get_runner(self.ctx).run(work, name=t("Exportação"), on_done=self._done,
                                 on_error=self._failed)
        self.ctx.toast(t("Exportando {n} corte(s): {l}").format(n=len(clips), l=names), "info")

    def _done(self, result: Any = None) -> None:
        self._finish_job()
        ok = int(result.get("ok", 0)) if isinstance(result, dict) else 0
        failed = int(result.get("failed", 0)) if isinstance(result, dict) else 0
        self.ctx.toast(t("{ok} exportado(s), {bad} com falha.").format(
            ok=ok, bad=failed), "ok" if failed == 0 else "warn")
        self._rebuild_queue()
        self._rebuild_history()

    def _failed(self, text: str = "") -> None:
        self._finish_job()
        self.ctx.toast(t("Exportação interrompida: ") + str(text), "error")
        self._rebuild_history()

    def _finish_job(self) -> None:
        self._running = False
        self.btn_export.setEnabled(True)
        self.btn_export.setText(t("Exportar seleção"))
        self.btn_cancel.setVisible(False)
        self._update_total()

    def _cancel(self) -> None:
        self.btn_cancel.setEnabled(False)
        self.btn_cancel.setText(t("Cancelando…"))
        try:
            self.ctx.exporter.cancel()
        except Exception as exc:  # noqa: BLE001 - cancelar deve falhar em silêncio
            log.info("exporter.cancel indisponível: %s", exc)
        runner = get_runner(self.ctx)
        try:
            runner.cancel(t("Exportação"))
        except AttributeError:
            runner.cancel_all()

    # ------------------------------------------------------------------ histórico
    def _history_rows(self) -> list[dict[str, Any]]:
        """Normaliza ``project.exports`` (dicts do núcleo ou ``ExportJob``)."""
        rows: list[dict[str, Any]] = []
        for item in list(getattr(self.ctx.project, "exports", []) or []):
            if isinstance(item, dict):
                rows.append({
                    "clip_id": item.get("clip_id", ""),
                    "path": item.get("out", "") or item.get("path", ""),
                    "state": item.get("state", "done"),
                    "error": item.get("error", ""),
                    "seconds": item.get("seconds", 0.0),
                    "finished_at": item.get("at", 0.0),
                    "project": item.get("title", ""),
                })
            else:
                rows.append({
                    "clip_id": getattr(item, "clip_id", ""),
                    "path": getattr(item, "out_path", ""),
                    "state": str(getattr(getattr(item, "state", None), "value", "done")),
                    "error": getattr(item, "error", ""),
                    "seconds": max(0.0, float(getattr(item, "finished_at", 0.0) or 0.0)
                                   - float(getattr(item, "started_at", 0.0) or 0.0)),
                    "finished_at": getattr(item, "finished_at", 0.0),
                    "project": "",
                })
        rows.sort(key=lambda r: -float(r.get("finished_at") or 0.0))
        return rows[:12]

    def _rebuild_history(self) -> None:
        rows = self._history_rows()
        self.hist_empty.setVisible(not rows)
        if rows:
            self._estimate_from_history(rows)
        for row in rows:
            widget = HistoryRow(row, self.hist_host)
            widget.openRequested.connect(self._open_path)
            self.hist_lay.insertWidget(0, widget)
        # mantém o topo enxuto: remove além de 12 linhas
        while self.hist_lay.count() - 1 > 12:
            item = self.hist_lay.takeAt(1)
            if item.widget() is not None:
                item.widget().deleteLater()

    def _estimate_from_history(self, rows: list[dict[str, Any]]) -> None:
        """Tamanho real medido no disco (o histórico do núcleo não guarda MB)."""
        total = 0.0
        for row in rows[:8]:
            path = str(row.get("path", ""))
            if path and Path(path).is_file():
                try:
                    total += Path(path).stat().st_size
                except OSError:
                    pass
        if total:
            from corteia import paths as _paths

            self.lbl_total.setText(t("últimos arquivos: {size}").format(
                size=_paths.human_bytes(total)))

    # ---------------------------------------------------------------- abrir pasta
    def _open_path(self, path: str) -> None:
        target = Path(str(path or ""))
        if not target.is_file():
            self.ctx.toast(t("O arquivo não existe mais."), "warn")
            return
        theme.open_location(str(target), reveal=True)
        self.ctx.toast(t("Abrindo {p}").format(p=target.name), "info")

    def _reveal_last(self) -> None:
        rows = self._history_rows()
        for row in rows:
            if row.get("path") and Path(row["path"]).is_file():
                self._open_path(row["path"])
                return
        self._open_out_dir()

    def _open_out_dir(self) -> None:
        out_dir = Path(str(self.edit_dir.text() or self.ctx.config.str_("export.dir")))
        out_dir.mkdir(parents=True, exist_ok=True)
        theme.open_location(str(out_dir), reveal=False)

    def _pick_dir(self) -> None:
        chosen = QFileDialog.getExistingDirectory(
            self, t("Pasta de saída"), str(self.edit_dir.text() or Path.home()))
        if chosen:
            self.edit_dir.setText(chosen)
            self._apply()

    # -------------------------------------------------------------------- gravar
    def _apply(self) -> None:
        """Escreve os ajustes em ``project.export_settings`` e persiste."""
        settings = self.ctx.project.export_settings
        settings.resolution = Resolution(str(self.combo_res.currentData()))
        settings.codec = Codec(str(self.combo_codec.currentData()))
        settings.quality_crf = int(self.spin_crf.value())
        settings.preset = str(self.combo_preset.currentText())
        settings.fps = float(self.combo_fps.currentData() or 30)
        settings.audio_bitrate = str(self.combo_audio.currentData() or "192k")
        settings.aspect = AspectRatio(str(self.seg_aspect.current()))
        settings.faststart = bool(self.sw_faststart.isChecked())
        settings.max_parallel = int(self.spin_parallel.value())
        settings.out_dir = str(self.edit_dir.text() or "")
        settings.filename_pattern = str(self.edit_pattern.text() or "{index:02d}-{title}")
        self._save()

    def _on_burn(self, on: bool) -> None:
        self.ctx.project.export_settings.burn_subtitles = bool(on)
        # o núcleo lê a mesma chave ao decidir queimar ou gerar .ass solto
        try:
            self.ctx.config.set("export.burn_subtitles", bool(on))
            self.ctx.config.save()
        except Exception as exc:  # noqa: BLE001 - config é opcional aqui
            log.info("config export.burn_subtitles não persistida: %s", exc)
        self._save()

    def _save(self) -> None:
        """``ctx.save()`` protegido: o núcleo grava histórico como dict e ``to_dict``
        espera objetos; a UI não pode quebrar por isso."""
        try:
            self.ctx.save()
        except Exception as exc:  # noqa: BLE001
            if not self._warned_save:
                self._warned_save = True
                log.warning("ctx.save falhou: %s", exc)
                self.ctx.toast(t("Ajustes aplicados, mas o projeto não pôde ser salvo "
                                 "(histórico de exportação incompatível)."), "warn")
