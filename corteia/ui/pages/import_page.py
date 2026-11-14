"""Importar: arquivo/link → validação real → transcrição → cortes.

Fluxo honesto, etapa por etapa:

1. o usuário solta um arquivo (ou escolhe no diálogo, ou cola um link);
2. :func:`corteia.core.media.is_supported` responde o formato e
   :func:`corteia.core.media.probe_or_raise` lê resolução, duração, fps,
   codecs e áudio **de verdade** (ffprobe), fora da thread da interface;
3. com a duração real, :func:`corteia.core.transcribe.estimate_duration_hours`
   mostra quanto a transcrição deve levar *neste computador*;
4. o modelo Whisper mostra o tamanho real do download e se já está no disco
   (:func:`corteia.core.transcribe.is_downloaded`); o botão baixar usa o
   progresso real de ``download_model``;
5. "Analisar" roda :meth:`corteia.core.pipeline.Pipeline.analyze` no
   ``JobRunner`` e mostra :class:`StageProgress` alimentado por ``.stages()``,
   com Cancelar ligado a ``pipeline.cancel()``.

Link passa pelo gate de consentimento
(:func:`corteia.core.downloader.can_download`) e a explicação em português é
mostrada inteira quando o download está bloqueado.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (QCheckBox, QComboBox, QFileDialog, QFrame,
                               QHBoxLayout, QLabel, QLineEdit, QListWidget,
                               QListWidgetItem, QPushButton, QScrollArea,
                               QSizePolicy, QVBoxLayout, QWidget)

from .. import theme
from ..widgets.busy import run_job
from ..widgets.cards import EmptyState
from ..widgets.droparea import DropArea
from ..widgets.progress import MiniBar, StageProgress
from ...i18n import t

log = logging.getLogger("corteia.ui.import")

#: rótulo em português por chave de estágio do pipeline
STAGE_LABELS = {
    "probe": "Lendo o arquivo",
    "thumb": "Gerando miniatura",
    "transcribe": "Transcrevendo a fala",
    "diarize": "Separando falantes",
    "silence": "Detectando silêncios",
    "scenes": "Detectando planos",
    "llm": "Avaliando com IA",
    "analyze": "Montando os cortes",
}

_LANGUAGES = (
    ("pt", "Português (Brasil)"),
    ("en", "English"),
    ("es", "Español"),
    ("fr", "Français"),
    ("de", "Deutsch"),
    ("it", "Italiano"),
    ("ja", "日本語"),
    ("auto", "Detectar automaticamente"),
)


class ImportPage(QWidget):
    def __init__(self, ctx: Any, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self._path: Path | None = None
        self._info: Any = None                 # VideoInfo do último probe válido
        self._job: Any = None
        self._dl_job: Any = None
        self._analysing = False
        self._estimating = False
        self._model_busy = False

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        outer.addWidget(scroll)

        body = QWidget(scroll)
        body.setProperty("transparent", "true")
        scroll.setWidget(body)
        lay = QVBoxLayout(body)
        lay.setContentsMargins(24, 20, 24, 24)
        lay.setSpacing(16)

        head = QHBoxLayout()
        self.title = QLabel(t("Importar vídeo"), body)
        self.title.setProperty("role", "title")
        head.addWidget(self.title)
        head.addStretch(1)
        lay.addLayout(head)

        self.subtitle = QLabel(
            t("O arquivo não é copiado nem movido: o CorteIA só lê os metadados "
              "com FFmpeg e processa o vídeo no lugar."), body)
        self.subtitle.setProperty("role", "sub")
        self.subtitle.setWordWrap(True)
        lay.addWidget(self.subtitle)

        # ------------------------------------------------------- área de soltura
        self.drop = DropArea(body)
        self.drop.filesDropped.connect(self.handle_paths)
        self.drop.urlDropped.connect(self.handle_url)
        self.drop.browseRequested.connect(self.choose_file)
        lay.addWidget(self.drop)

        row = QHBoxLayout()
        row.setSpacing(8)
        self.btn_browse = QPushButton(t("Escolher arquivo"), body)
        self.btn_browse.setProperty("variant", "outline")
        self.btn_browse.setIcon(theme.icon("folder", "muted", 18))
        self.btn_browse.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_browse.clicked.connect(self.choose_file)
        row.addWidget(self.btn_browse)
        self.url_edit = QLineEdit(body)
        self.url_edit.setPlaceholderText(t("ou cole um link (YouTube, Vimeo…)"))
        self.url_edit.returnPressed.connect(self.try_url)
        row.addWidget(self.url_edit, 1)
        self.btn_url = QPushButton(t("Usar link"), body)
        self.btn_url.setProperty("variant", "outline")
        self.btn_url.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_url.clicked.connect(self.try_url)
        row.addWidget(self.btn_url)
        lay.addLayout(row)

        self.consent = QCheckBox(t("Permitir download de vídeos por link"), body)
        self.consent.setToolTip(t("Necessário para o yt-dlp funcionar. Também pode ser "
                                 "ativado em Configurações → Armazenamento."))
        self.consent.setChecked(self.ctx.config.bool_("net.download_consent"))
        self.consent.toggled.connect(self._on_consent)
        lay.addWidget(self.consent)

        # ------------------------------------------------------------- validação
        self.status_card = QFrame(body)
        self.status_card.setProperty("card", "flat")
        sc = QVBoxLayout(self.status_card)
        sc.setContentsMargins(16, 12, 16, 12)
        sc.setSpacing(6)
        self.file_label = QLabel(t("Nenhum arquivo selecionado."), self.status_card)
        self.file_label.setProperty("role", "page")
        self.file_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.file_label.setWordWrap(True)
        sc.addWidget(self.file_label)
        self.meta_label = QLabel("", self.status_card)
        self.meta_label.setProperty("role", "mono")
        self.meta_label.setWordWrap(True)
        sc.addWidget(self.meta_label)
        self.eta_label = QLabel("", self.status_card)
        self.eta_label.setProperty("role", "muted")
        self.eta_label.setWordWrap(True)
        sc.addWidget(self.eta_label)
        self.probe_bar = MiniBar(self.status_card)
        self.probe_bar.setVisible(False)
        sc.addWidget(self.probe_bar)
        lay.addWidget(self.status_card)

        self.error_card = QFrame(body)
        self.error_card.setProperty("card", "danger")
        ec = QVBoxLayout(self.error_card)
        ec.setContentsMargins(16, 12, 16, 12)
        self.error_label = QLabel("", self.error_card)
        self.error_label.setProperty("role", "error")
        self.error_label.setWordWrap(True)
        self.error_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        ec.addWidget(self.error_label)
        self.error_hint = QLabel("", self.error_card)
        self.error_hint.setProperty("role", "muted")
        self.error_hint.setWordWrap(True)
        ec.addWidget(self.error_hint)
        self.error_card.setVisible(False)
        lay.addWidget(self.error_card)

        # ------------------------------------------------------- opções de análise
        lay.addWidget(_section(t("Transcrição"), body))
        grid = QHBoxLayout()
        grid.setSpacing(10)

        self.model_combo = QComboBox(body)
        self.model_combo.setToolTip(t("Modelo Whisper (CTranslate2) usado na transcrição"))
        for key, meta in _models().items():
            if key == "auto":
                self.model_combo.addItem(t("Automático (escolhe pela RAM)"), key)
            else:
                mb = int(meta.get("mb", 0) or 0)
                suffix = f" · {mb} MB" if mb else ""
                self.model_combo.addItem(f"{key}{suffix}", key)
        for idx in range(self.model_combo.count()):
            key = self.model_combo.itemData(idx)
            self.model_combo.setItemData(idx, str(_models().get(key, {}).get("desc", "")),
                                        Qt.ItemDataRole.ToolTipRole)
        current = self.ctx.config.str_("subs.model", "base") or "base"
        _set_combo(self.model_combo, current)
        self.model_combo.currentIndexChanged.connect(self._on_model_changed)
        grid.addWidget(_field(t("Modelo Whisper"), self.model_combo, body), 2)

        self.lang_combo = QComboBox(body)
        for code, name in _LANGUAGES:
            self.lang_combo.addItem(name, code)
        _set_combo(self.lang_combo, self.ctx.config.str_("subs.language", "pt") or "pt")
        self.lang_combo.currentIndexChanged.connect(self._on_language_changed)
        grid.addWidget(_field(t("Idioma da fala"), self.lang_combo, body), 1)

        self.btn_download = QPushButton(t("Baixar modelo"), body)
        self.btn_download.setProperty("variant", "outline")
        self.btn_download.setIcon(theme.icon("download", "muted", 18))
        self.btn_download.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_download.clicked.connect(self.download_model)
        grid.addWidget(_field(t("Instalação do modelo"), self.btn_download, body), 1)
        lay.addLayout(grid)

        self.model_status = QLabel("", body)
        self.model_status.setProperty("role", "mono")
        self.model_status.setWordWrap(True)
        lay.addWidget(self.model_status)

        self.dl_bar = MiniBar(body)
        self.dl_bar.setVisible(False)
        lay.addWidget(self.dl_bar)

        lay.addWidget(_section(t("Cortes desejados"), body))
        opts = QHBoxLayout()
        opts.setSpacing(10)
        self.preset_combo = QComboBox(body)
        for value, label in ((15, "15 s"), (30, "30 s"), (45, "45 s"), (60, "60 s"),
                             (90, "90 s"), (120, "2 min"), (0, t("personalizado"))):
            self.preset_combo.addItem(label, value)
        _set_combo(self.preset_combo, self.ctx.config.int_("analysis.duration_preset", 60))
        self.preset_combo.currentIndexChanged.connect(self._on_preset)
        opts.addWidget(_field(t("Duração-alvo"), self.preset_combo, body), 1)

        self.min_spin = _spin(5.0, 300.0, self.ctx.config.float_("analysis.min_seconds", 15.0), body)
        self.min_spin.valueChanged.connect(lambda v: self._cfg("analysis.min_seconds", float(v)))
        opts.addWidget(_field(t("Mínimo (s)"), self.min_spin, body), 1)
        self.max_spin = _spin(5.0, 600.0, self.ctx.config.float_("analysis.max_seconds", 60.0), body)
        self.max_spin.valueChanged.connect(lambda v: self._cfg("analysis.max_seconds", float(v)))
        opts.addWidget(_field(t("Máximo (s)"), self.max_spin, body), 1)
        self.clips_spin = _spin(1, 60, self.ctx.config.int_("analysis.max_clips", 12), body, step=1)
        self.clips_spin.valueChanged.connect(lambda v: self._cfg("analysis.max_clips", int(v)))
        opts.addWidget(_field(t("Máximo de cortes"), self.clips_spin, body), 1)

        self.aspect_combo = QComboBox(body)
        for value, label in (("9:16", "9:16 · TikTok / Reels / Shorts"),
                             ("16:9", "16:9 · YouTube"),
                             ("1:1", "1:1 · Feed"),
                             ("4:5", "4:5 · Retrato")):
            self.aspect_combo.addItem(label, value)
        _set_combo(self.aspect_combo, self.ctx.config.str_("analysis.aspect", "9:16"))
        self.aspect_combo.currentIndexChanged.connect(self._on_aspect)
        opts.addWidget(_field(t("Proporção"), self.aspect_combo, body), 2)
        lay.addLayout(opts)

        toggles = QHBoxLayout()
        self.tg_silence = QCheckBox(t("Remover silêncios"), body)
        self.tg_silence.setChecked(self.ctx.config.bool_("analysis.remove_silences", True))
        self.tg_silence.toggled.connect(lambda v: self._cfg("analysis.remove_silences", bool(v)))
        self.tg_boundaries = QCheckBox(t("Cortar em frases inteiras"), body)
        self.tg_boundaries.setChecked(self.ctx.config.bool_("analysis.smart_boundaries", True))
        self.tg_boundaries.toggled.connect(lambda v: self._cfg("analysis.smart_boundaries", bool(v)))
        self.tg_follow = QCheckBox(t("Seguir quem está falando"), body)
        self.tg_follow.setChecked(self.ctx.config.bool_("analysis.follow_speaker", True))
        self.tg_follow.toggled.connect(lambda v: self._cfg("analysis.follow_speaker", bool(v)))
        self.tg_llm = QCheckBox(t("Usar IA externa para pontuar"), body)
        self.tg_llm.setToolTip(t("Só liga se um provedor estiver configurado em Configurações → APIs."))
        self.tg_llm.setChecked(self.ctx.config.str_("api.provider", "off") not in ("", "off"))
        self.tg_llm.toggled.connect(self._on_llm_toggled)
        for cb in (self.tg_silence, self.tg_boundaries, self.tg_follow, self.tg_llm):
            cb.setObjectName("plainCheck")
            toggles.addWidget(cb)
        toggles.addStretch(1)
        lay.addLayout(toggles)

        # ---------------------------------------------------------------- análise
        actions = QHBoxLayout()
        actions.setSpacing(10)
        self.btn_analyze = QPushButton(t("Analisar e gerar cortes"), body)
        self.btn_analyze.setProperty("variant", "primary")
        self.btn_analyze.setProperty("size", "lg")
        self.btn_analyze.setIcon(theme.icon("wand", "on_accent", 20))
        self.btn_analyze.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_analyze.clicked.connect(self.analyze_current)
        actions.addWidget(self.btn_analyze)
        self.btn_cancel = QPushButton(t("Cancelar"), body)
        self.btn_cancel.setProperty("variant", "danger")
        self.btn_cancel.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_cancel.setIcon(theme.icon("close", "error", 16))
        self.btn_cancel.clicked.connect(self.cancel_analysis)
        self.btn_cancel.setEnabled(False)
        actions.addWidget(self.btn_cancel)
        self.btn_open_dir = QPushButton(t("Abrir pasta do vídeo"), body)
        self.btn_open_dir.setProperty("variant", "ghost")
        self.btn_open_dir.setIcon(theme.icon("folder", "muted", 18))
        self.btn_open_dir.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_open_dir.clicked.connect(self._open_folder)
        actions.addWidget(self.btn_open_dir)
        actions.addStretch(1)
        lay.addLayout(actions)

        self.overall = MiniBar(body)
        self.overall.setVisible(False)
        lay.addWidget(self.overall)
        self.stages = StageProgress(ctx, body, t("Etapas da análise"))
        lay.addWidget(self.stages)

        lay.addWidget(_section(t("Arquivos deste projeto"), body))
        self.source_list = QListWidget(body)
        self.source_list.setMinimumHeight(96)
        self.source_list.itemSelectionChanged.connect(self._list_selection)
        lay.addWidget(self.source_list)

        self.empty = EmptyState(t("Nada importado ainda"),
                                t("Solte um vídeo, escolha um arquivo ou cole um link para começar."),
                                icon="import", parent=body)
        self.empty.set_action("", None)
        lay.addWidget(self.empty)
        lay.addStretch(1)

        # ------------------------------------------------------------ timers
        self._stage_timer = QTimer(self)
        self._stage_timer.setInterval(400)
        self._stage_timer.timeout.connect(self._poll_stages)

        ctx.signals.sources_changed.connect(self._rebuild_list)
        ctx.signals.project_loaded.connect(lambda _p: self._rebuild_list())
        ctx.signals.clips_changed.connect(self._on_clips_changed)

        QTimer.singleShot(60, self._warm)
        self.refresh()

    # ------------------------------------------------------------------ aquecer
    def _warm(self) -> None:
        """Fora da GUI: hardware (FFmpeg/GPU) e instalação dos modelos Whisper."""
        def work(progress) -> dict:
            from ... import hardware
            from ...core import transcribe as TR

            hw = hardware.probe()
            progress(70.0, t("verificando modelos no disco"))
            return {
                "encoders": list(hw.video_encoders),
                "cuda": bool(hw.cuda_available),
                "nvenc": bool(hw.nvenc_available),
                "downloaded": {k: TR.is_downloaded(k) for k in _models()},
                "threads": int(self.ctx.config.int_("proc.cpu_threads", 0) or hw.cpu_count),
            }

        def done(result: Any) -> None:
            self._hw = result or {}
            self._update_model_status()
            self._update_eta()

        run_job(self.ctx, work, name=t("diagnóstico de hardware"), on_done=done, busy=False)

    # ------------------------------------------------------------- entrada/arquivo
    def focus_input(self) -> None:
        self.url_edit.setFocus()

    def choose_file(self) -> None:
        try:
            from ...core.media import SUPPORTED_EXTS

            exts = list(SUPPORTED_EXTS)
        except Exception:  # noqa: BLE001
            exts = [".mp4", ".mov", ".mkv", ".webm", ".avi"]
        filters = [t("Vídeo") + " (" + " ".join(f"*{e}" for e in exts) + ")",
                   t("Todos os arquivos") + " (*)"]
        start = self.ctx.config.str_("last_source_dir", "") or str(Path.home())
        path, _sel = QFileDialog.getOpenFileName(self, t("Escolher vídeo"), start,
                                                " ;; ".join(filters))
        if path:
            try:
                self.ctx.config.set("last_source_dir", str(Path(path).parent))
            except Exception as exc:  # noqa: BLE001
                log.debug("last_source_dir: %s", exc)
            self.handle_paths([path])

    def handle_paths(self, paths_list: list[str]) -> None:
        for chunk in list(paths_list or []):
            p = Path(chunk)
            if p.is_dir():
                continue
            try:
                self.ctx.config.set("last_source_dir", str(p.parent))
            except Exception:  # noqa: BLE001
                pass
            self.select_file(p)
            return
        remaining = [c for c in (paths_list or [])[1:] if Path(c).is_file()]
        if remaining:
            self.ctx.toast(t("{n} outro(s) arquivo(s) ignorado(s) nesta importação.").format(
                n=len(remaining)), "info")

    def select_file(self, path: Path) -> None:
        self._path = Path(path)
        self._info = None
        self._set_error("")
        self.file_label.setText(str(self._path))
        self.meta_label.setText(t("Lendo o arquivo com FFmpeg…"))
        self.probe_bar.setVisible(True)
        self.probe_bar.set_indeterminate(True)
        self.empty.setVisible(False)
        self.btn_analyze.setEnabled(False)
        self._register_source(origin="file")
        self._probe(path)

    def _probe(self, path: Path) -> None:
        def work(progress) -> Any:
            from ...core import media

            progress(10.0, t("verificando formato"))
            ok, why = media.is_supported(path)
            if not ok:
                raise ValueError(f"FORMATO::{why}")
            progress(45.0, t("lendo metadados (ffprobe)"))
            return media.probe_or_raise(path)

        def done(info: Any) -> None:
            self.probe_bar.setVisible(False)
            self._info = info
            if info is None:
                return
            # Guardar o probe na fonte do projeto: sem isto o Painel e a lista
            # de fontes mostram "--" para resolução/duração mesmo depois de um
            # import bem-sucedido, e a próxima etapa releria o arquivo à toa.
            try:
                self.ctx.add_source(self._path, info, origin="file")
            except Exception as exc:  # noqa: BLE001 - metadados extras não podem travar a UI
                log.debug("probe não gravado na fonte: %s", exc)
            self.meta_label.setText(_describe(info))
            self.eta_label.setText(t("Preparando estimativa de transcrição…"))
            self.btn_analyze.setEnabled(not self._analysing)
            self._update_eta()

        def error(msg: str) -> None:
            self.probe_bar.setVisible(False)
            self.btn_analyze.setEnabled(False)
            if msg.startswith("FORMATO::"):
                self._set_error(msg.split("::", 1)[1], "")
            else:
                self._set_error(msg, t("Veja se o FFmpeg está instalado (Configurações → Diagnóstico)."))

        run_job(self.ctx, work, name=t("leitura do vídeo"), on_done=done, on_error=error, busy=False)

    def _register_source(self, origin: str = "file") -> None:
        if self._path is None:
            return
        try:
            self.ctx.add_source(self._path, origin=origin)
        except Exception as exc:  # noqa: BLE001 - o projeto ainda não salvou nada
            log.debug("add_source: %s", exc)

    # --------------------------------------------------------------------- link
    def handle_url(self, url: str) -> None:
        self.url_edit.setText(url)
        self.try_url()

    def try_url(self) -> None:
        url = (self.url_edit.text() or "").strip()
        if not url:
            return
        from ...core import downloader as DL

        if not DL.is_url(url):
            self._set_error(t("Isso não parece ser um link http/https."),
                            t("Para um arquivo do computador, use “Escolher arquivo”."))
            return
        ok, why = DL.can_download(url)
        if not ok:
            self._set_error(why, t("Marque a permissão de download acima para liberar essa porta."))
            if "desativado" in why or "Ative" in why:
                self.consent.setChecked(False)
            return
        self._set_error("")
        self.file_label.setText(url)
        self.meta_label.setText(t("Baixando o vídeo…"))
        self.probe_bar.setVisible(True)
        self.probe_bar.set_indeterminate(True)
        self.btn_analyze.setEnabled(False)

        def work(progress) -> Path:
            return DL.download(url, on_progress=lambda f, m: progress(
                (f * 100.0 if f <= 1.0 else f), m))

        def done(result: Any) -> None:
            self.probe_bar.setVisible(False)
            path = Path(str(result))
            self.ctx.toast(t("Download concluído: {name}").format(name=path.name), "ok")
            self.select_file(path)
            self.analyze_current()

        def error(msg: str) -> None:
            self.probe_bar.setVisible(False)
            self._set_error(msg, t("O link pode exigir login, ser privado ou estar fora do ar."))

        run_job(self.ctx, work, name=t("download do vídeo"), on_done=done,
                on_error=error, busy=False)

    def _on_consent(self, value: bool) -> None:
        self.ctx.config.set("net.download_consent", bool(value))
        if value:
            self.ctx.toast(t("Download por link liberado. O arquivo vai para a pasta temporária."), "info")

    # ------------------------------------------------------------------ modelos
    def _on_model_changed(self, _index: int) -> None:
        key = self.model_combo.currentData() or "base"
        self.ctx.config.set("subs.model", str(key))
        self.ctx.config.set("asr.model", str(key))
        try:
            self.ctx.project.options.whisper_model = str(key)
        except Exception:  # noqa: BLE001
            pass
        self._update_model_status()
        self._update_eta()

    def _on_language_changed(self, _index: int) -> None:
        code = str(self.lang_combo.currentData() or "pt")
        self.ctx.config.set("subs.language", code)
        self.ctx.config.set("asr.language", code)
        try:
            self.ctx.project.options.language = code
        except Exception:  # noqa: BLE001
            pass

    def _update_model_status(self) -> None:
        key = str(self.model_combo.currentData() or "base")
        meta = _models().get(key, {})
        mb = float(meta.get("mb", 0) or 0)
        peak = float(meta.get("peak_mb", 0) or 0)
        try:
            from ...core import transcribe as TR

            have = TR.is_downloaded(key)
        except Exception as exc:  # noqa: BLE001
            self.model_status.setText(f"{t('Não foi possível checar o modelo')}: {exc}")
            self.btn_download.setEnabled(False)
            return
        from ... import paths

        bits = [meta.get("desc") or key]
        if mb:
            bits.append(t("download de {mb}").format(mb=paths.human_bytes(mb * 1024 * 1024)))
        if peak:
            bits.append(t("pico de RAM {mb}").format(mb=paths.human_bytes(peak * 1024 * 1024)))
        state = t("já instalado e pronto para uso offline") if have else t("não baixado")
        self.model_status.setText(f"{key}: {state} · " + " · ".join(bits[1:3]))
        self.btn_download.setText(t("Baixar modelo") if not have else t("Reinstalar modelo"))
        self.btn_download.setEnabled(not self._model_busy)
        self._set_pill(self.model_status)

    def _set_pill(self, widget: QWidget) -> None:
        widget.style().unpolish(widget)
        widget.style().polish(widget)

    def download_model(self) -> None:
        key = str(self.model_combo.currentData() or "base")
        if self._model_busy:
            return
        self._model_busy = True
        self.btn_download.setEnabled(False)
        self.btn_download.setText(t("baixando…"))
        self.dl_bar.setVisible(True)
        self.dl_bar.set_indeterminate(True)

        def work(progress) -> Path:
            from ...core import transcribe as TR

            return TR.download_model(key, on_progress=lambda f, m: progress(
                (f * 100.0 if f <= 1.0 else f), m))

        def done(result: Any) -> None:
            self._model_busy = False
            self.dl_bar.setVisible(False)
            self.btn_download.setText(t("Baixar modelo"))
            self._update_model_status()
            self.ctx.toast(t("Modelo {name} instalado.").format(name=key), "ok")

        def error(msg: str) -> None:
            self._model_busy = False
            self.dl_bar.setVisible(False)
            self.btn_download.setText(t("Baixar modelo"))
            self.btn_download.setEnabled(True)
            self._set_error(msg, t("Sem internet o modelo não baixa; use um já instalado."))

        run_job(self.ctx, work, name=t("download do modelo"), on_done=done, on_error=error,
                busy=False, on_progress=self._on_dl_progress)

    def _on_dl_progress(self, percent: float, label: str) -> None:
        self.dl_bar.set_indeterminate(False)
        self.dl_bar.set_percent(percent)
        if label:
            self.dl_bar.setToolTip(label)

    # --------------------------------------------------------------- estimativa
    def _update_eta(self) -> None:
        info = self._info
        if info is None:
            self.eta_label.setText("")
            return
        if self._estimating:
            return
        self._estimating = True
        model = str(self.model_combo.currentData() or "base")
        seconds = float(getattr(info, "duration", 0.0) or 0.0)
        threads = int(self.ctx.config.int_("proc.cpu_threads", 0) or 0)

        def work(progress) -> str:
            from ...core import transcribe as TR

            progress(20.0, t("calculando estimativa"))
            return TR.estimate_duration_hours(model, seconds, threads)

        def done(result: Any) -> None:
            self._estimating = False
            if not isinstance(result, str) or not result:
                return
            audio = t("com áudio") if getattr(info, "has_audio", False) else t("SEM trilha de áudio")
            self.eta_label.setText(
                t("Transcrição com {model}: {eta} · áudio {audio}").format(
                    model=model, eta=result, audio=audio))

        def error(_msg: str) -> None:
            self._estimating = False

        run_job(self.ctx, work, name=t("estimativa de transcrição"), on_done=done,
                on_error=error, busy=False)

    # ------------------------------------------------------------------ opções
    def _on_preset(self, _index: int) -> None:
        value = int(self.preset_combo.currentData() or 0)
        self.ctx.config.set("analysis.duration_preset", value)
        try:
            opts = self.ctx.project.options
            opts.preset_duration = value
            if value:
                opts.target_min = max(5.0, value * 0.5)
                opts.target_max = value
        except Exception:  # noqa: BLE001
            pass
        if value:
            self.min_spin.blockSignals(True)
            self.max_spin.blockSignals(True)
            self.min_spin.setValue(max(5.0, value * 0.5))
            self.max_spin.setValue(float(value))
            self.min_spin.blockSignals(False)
            self.max_spin.blockSignals(False)

    def _on_aspect(self, _index: int) -> None:
        value = str(self.aspect_combo.currentData() or "9:16")
        self.ctx.config.set("analysis.aspect", value)
        try:
            self.ctx.project.export_settings.aspect = value  # type: ignore[assignment]
        except Exception:  # noqa: BLE001
            pass

    def _on_llm_toggled(self, value: bool) -> None:
        provider = self.ctx.config.str_("api.provider", "off")
        if value and provider in ("", "off"):
            self.ctx.toast(t("Nenhum provedor de IA configurado. Abrindo Configurações → APIs."), "warn")
            self.ctx.go("settings")
            fn = getattr(self.ctx, "_open_api_tab", None)
            if callable(fn):
                fn()
            self.tg_llm.setChecked(False)
            return
        try:
            self.ctx.project.options.use_llm = bool(value)
        except Exception:  # noqa: BLE001
            pass

    def _cfg(self, key: str, value: Any) -> None:
        try:
            self.ctx.config.set(key, value)
        except Exception as exc:  # noqa: BLE001
            log.debug("config %s: %s", key, exc)
        try:
            opts = self.ctx.project.options
            if key == "analysis.min_seconds":
                opts.target_min = float(value)
            elif key == "analysis.max_seconds":
                opts.target_max = float(value)
            elif key == "analysis.max_clips":
                opts.max_clips = int(value)
            elif key == "analysis.remove_silences":
                opts.remove_silences = bool(value)
            elif key == "analysis.smart_boundaries":
                opts.smart_boundaries = bool(value)
            elif key == "analysis.follow_speaker":
                opts.follow_speaker = bool(value)
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------------ análise
    def analyze_current(self) -> None:
        """Roda o pipeline real no JobRunner e acompanha ``pipeline.stages()``."""
        if self._analysing:
            self.ctx.toast(t("Já existe uma análise em andamento."), "warn")
            return
        path = self._path or (Path(self.ctx.current_source_path())
                              if self.ctx.current_source_path() else None)
        if path is None or not Path(path).is_file():
            self._set_error(t("Escolha um arquivo de vídeo primeiro."),
                            t("Use a área de soltura ou o botão “Escolher arquivo”."))
            return
        self._path = Path(path)
        self._analysing = True
        self.btn_analyze.setEnabled(False)
        self.btn_cancel.setEnabled(True)
        self.overall.setVisible(True)
        self.overall.set_percent(0.0)
        self._set_error("")
        self.stages.reset_clocks()
        self._poll_stages()
        self._stage_timer.start()

        options = self._options()

        def work(progress) -> list:
            from ...core import errors as ERR

            cancel = threading.Event()
            self._cancel_event = cancel

            def on_frac(frac: float, msg: str) -> None:
                percent = frac * 100.0 if 0.0 <= frac <= 1.0 else frac
                progress(max(0.0, min(100.0, percent)), msg)

            def on_stage(key: str, state: str) -> None:
                log.debug("stage %s -> %s", key, state)

            try:
                clips = self.ctx.pipeline.analyze(str(path), options, on_progress=on_frac,
                                                 on_stage=on_stage, cancel=cancel,
                                                 force=False)
            except ERR.CancelledError as exc:
                progress.cancelled = True  # type: ignore[attr-defined]
                raise exc
            return list(clips or [])

        def done(result: Any) -> None:
            self._finish_analysis()
            n = len(result or [])
            if n:
                self.ctx.toast(t("{n} corte(s) sugerido(s). Veja a página Cortes.").format(n=n), "ok")
                self.ctx.go("clips")
            else:
                self.ctx.toast(t("A análise terminou sem sugerir cortes."), "warn")
            self.refresh()

        def error(msg: str) -> None:
            self._finish_analysis()
            self._set_error(msg, t("Cancele e tente outro modelo, ou revise o arquivo."))
            self._poll_stages()

        self._job = run_job(self.ctx, work, name=t("análise do vídeo"), on_done=done,
                            on_error=error, busy=True, busy_label=t("Analisando o vídeo"),
                            on_progress=self._on_overall)

    def _options(self) -> Any:
        from ...models import AnalysisOptions

        base = self.ctx.project.options if getattr(self.ctx.project, "options", None) else AnalysisOptions()
        try:
            import copy

            opts = copy.deepcopy(base) if hasattr(copy, "deepcopy") else base
        except Exception:  # noqa: BLE001
            opts = base
        opts.preset_duration = int(self.preset_combo.currentData() or 0)
        opts.target_min = float(self.min_spin.value())
        opts.target_max = float(self.max_spin.value())
        opts.max_clips = int(self.clips_spin.value())
        opts.min_score = float(self.ctx.config.float_("analysis.min_score", 25.0))
        opts.remove_silences = bool(self.tg_silence.isChecked())
        opts.smart_boundaries = bool(self.tg_boundaries.isChecked())
        opts.follow_speaker = bool(self.tg_follow.isChecked())
        opts.use_llm = bool(self.tg_llm.isChecked())
        opts.whisper_model = str(self.model_combo.currentData() or "base")
        opts.language = str(self.lang_combo.currentData() or "pt")
        try:
            from ...models import AspectRatio

            opts.aspect = AspectRatio(str(self.aspect_combo.currentData() or "9:16"))
        except Exception:  # noqa: BLE001
            pass
        return opts

    def _on_overall(self, percent: float, label: str) -> None:
        self.overall.set_percent(percent)
        self.statusBarMessage(label)

    def statusBarMessage(self, label: str) -> None:
        if label:
            self.file_label.setToolTip(label)

    def _finish_analysis(self) -> None:
        self._analysing = False
        self._stage_timer.stop()
        self.btn_cancel.setEnabled(False)
        self.btn_analyze.setEnabled(True)
        self.overall.set_indeterminate(False)
        self._poll_stages()

    def cancel_analysis(self) -> None:
        stopped = 0
        try:
            obj = getattr(self.ctx, "_pipeline", None)
            if obj is not None:
                obj.cancel()
                stopped += 1
        except Exception as exc:  # noqa: BLE001
            log.debug("pipeline.cancel: %s", exc)
        try:
            from ...workers.jobs import runner_for

            runner_for(self.ctx).cancel()
            stopped += 1
        except Exception as exc:  # noqa: BLE001
            log.debug("runner.cancel: %s", exc)
        if stopped:
            self.btn_cancel.setEnabled(False)
            self.btn_cancel.setText(t("Cancelando…"))
            self.ctx.toast(t("Cancelando a análise…"), "warn")

    def _poll_stages(self) -> None:
        pipeline = getattr(self.ctx, "_pipeline", None)
        if pipeline is None:
            self.stages.refresh()
            return
        try:
            raw = pipeline.stages()
        except Exception as exc:  # noqa: BLE001
            self.stages.set_message(f"{t('Não foi possível ler as etapas')}: {exc}")
            return
        self.stages.set_stages([_adapt_stage(s) for s in raw])

    # -------------------------------------------------------------- arquivos
    def _rebuild_list(self) -> None:
        self.source_list.blockSignals(True)
        self.source_list.clear()
        for src in getattr(self.ctx.project, "sources", []) or []:
            p = Path(src.path or "")
            item = QListWidgetItem()
            if not p.is_file():
                item.setText(f"⚠ {src.path} — {t('arquivo não encontrado')}")
                item.setForeground(theme.qcolor("error"))
            else:
                dur = float(getattr(src.info, "duration", 0.0) or 0.0) if src.info else 0.0
                meta = f"{theme.format_duration_long(dur)}" if dur else t("sem metadados")
                done = len(src.stages_done or [])
                item.setText(f"{src.path} — {meta} · {t('{n} etapa(s) prontas').format(n=done)}")
                item.setToolTip(src.path)
            item.setData(Qt.ItemDataRole.UserRole, str(src.path))
            self.source_list.addItem(item)
            if self.ctx.project.sources and str(self.ctx.project.sources[
                    max(0, min(int(self.ctx.project.active_source_index or 0),
                               len(self.ctx.project.sources) - 1))].path) == str(src.path):
                self.source_list.setCurrentItem(item)
        self.source_list.blockSignals(False)
        self.empty.setVisible(self.source_list.count() == 0)

    def _list_selection(self) -> None:
        item = self.source_list.currentItem()
        if item is None:
            return
        path = Path(item.data(Qt.ItemDataRole.UserRole) or "")
        if not path.is_file():
            self.ctx.toast(t("Esse vídeo não está mais neste caminho."), "warn")
            return
        if str(path) != str(self._path or ""):
            self.select_file(path)
        try:
            src = self.ctx.source_for(path)
            if src is not None:
                self.ctx.set_active_source(src)
        except Exception as exc:  # noqa: BLE001
            log.debug("set_active_source: %s", exc)

    def _on_clips_changed(self) -> None:
        if self._analysing:
            self._poll_stages()

    # ------------------------------------------------------------------ erros
    def _set_error(self, message: str, hint: str = "") -> None:
        self.error_card.setVisible(bool(message))
        self.error_label.setText(message or "")
        self.error_hint.setText(hint or "")
        self.error_hint.setVisible(bool(hint))

    def _open_folder(self) -> None:
        target = self._path
        if target is None:
            self.ctx.toast(t("Nenhum arquivo selecionado."), "info")
            return
        ok, why = theme.open_location(Path(target).parent)
        if not ok:
            self.ctx.toast(why, "error")

    # ------------------------------------------------------------------ ciclo
    def refresh(self) -> None:
        self.consent.blockSignals(True)
        self.consent.setChecked(self.ctx.config.bool_("net.download_consent"))
        self.consent.blockSignals(False)
        self.tg_llm.blockSignals(True)
        self.tg_llm.setChecked(self.ctx.config.str_("api.provider", "off") not in ("", "off"))
        self.tg_llm.blockSignals(False)
        current = self.ctx.config.str_("subs.model", "base") or "base"
        if str(self.model_combo.currentData() or "") != current:
            _set_combo(self.model_combo, current)
        self._update_model_status()
        self._rebuild_list()
        if self._path and self._info is None:
            self.meta_label.setText(t("Abra novamente o arquivo para reler os metadados."))
        self.btn_analyze.setEnabled(not self._analysing and (
            self._path is not None or bool(self.ctx.current_source_path())))


# --------------------------------------------------------------------------- #
# utilidades de módulo
# --------------------------------------------------------------------------- #
def _models() -> dict:
    try:
        from ...core.transcribe import MODELS

        return dict(MODELS)
    except Exception as exc:  # noqa: BLE001
        log.debug("MODELS indisponível: %s", exc)
        return {"base": {"repo": "", "mb": 148, "desc": "base"}}


def _adapt_stage(stage: dict) -> dict:
    """``stages()`` do núcleo (key/state/progress/detail) → forma do StageProgress."""
    key = str(stage.get("key", "") or stage.get("id", "") or "")
    state = str(stage.get("state", "idle") or "idle")
    progress = float(stage.get("progress", 0.0) or 0.0)
    percent = progress * 100.0 if 0.0 <= progress <= 1.0 else progress
    note = str(stage.get("detail", "") or "")
    label = t(STAGE_LABELS.get(key, key or "etapa"))
    if state == "error":
        note = f"{t('erro')}: {note}" if note else t("etapa com erro")
        percent = max(percent, 0.0)
    elif state == "cancelled":
        note = f"{t('cancelado')}: {note}" if note else t("cancelado pelo usuário")
    elif state == "idle" and not note:
        note = t("aguardando")
    return {"id": key or label, "label": label, "done": state == "done",
            "percent": 0.0 if state == "idle" else percent, "note": note}


def _describe(info: Any) -> str:
    from ... import paths

    bits = [f"{int(info.width)}×{int(info.height)}",
            theme.format_duration_long(float(info.duration)),
            f"{float(info.fps):.2f} fps"]
    if info.codec_video:
        bits.append(t("vídeo {codec}").format(codec=info.codec_video))
    bits.append(t("áudio {codec}").format(codec=info.codec_audio)
                if info.has_audio and info.codec_audio else
                (t("sem áudio") if not info.has_audio else t("áudio presente")))
    if info.bitrate:
        bits.append(f"{info.bitrate / 1_000_000:.1f} Mbps")
    size = None
    try:
        size = Path(info.path).stat().st_size
    except OSError:
        pass
    if size:
        bits.append(paths.human_bytes(size))
    if info.rotation:
        bits.append(t("girado {deg}°").format(deg=info.rotation))
    return " · ".join(bits)


def _section(text: str, parent: QWidget) -> QLabel:
    label = QLabel(text, parent)
    label.setProperty("role", "section")
    return label


def _field(label_text: str, widget: QWidget, parent: QWidget) -> QWidget:
    host = QWidget(parent)
    host.setProperty("transparent", "true")
    lay = QVBoxLayout(host)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(3)
    cap = QLabel(label_text, host)
    cap.setProperty("role", "faint")
    lay.addWidget(cap)
    lay.addWidget(widget)
    return host


def _spin(lo: float, hi: float, value: float, parent: QWidget, *, step: int = 0) -> Any:
    from PySide6.QtCore import QLocale

    if step:
        from PySide6.QtWidgets import QSpinBox

        box = QSpinBox(parent)
        box.setRange(int(lo), int(hi))
        box.setValue(int(value))
        box.setSingleStep(step)
    else:
        from PySide6.QtWidgets import QDoubleSpinBox

        box = QDoubleSpinBox(parent)
        box.setDecimals(1)
        box.setRange(lo, hi)
        box.setValue(float(value))
        box.setSingleStep(0.5)
    box.setLocale(QLocale(QLocale.Language.English))
    box.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
    return box


def _set_combo(combo: QComboBox, value: Any) -> None:
    idx = combo.findData(value)
    if idx < 0 and isinstance(value, str):
        idx = combo.findData(value.lower())
    if idx >= 0:
        combo.setCurrentIndex(idx)
