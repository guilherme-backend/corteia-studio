"""Configurações: sete abas, tudo persistido por ``ctx.config.set``.

Regras seguidas aqui:

* nada de opção decorativa — cada campo escreve a chave que o **núcleo lê**;
* quando o núcleo usa duas chaves para a mesma coisa (``proc.silence_db`` e
  ``silence.db``), gravamos as duas para o valor valer de fato;
* os codificadores oferecidos vêm de ``hardware.probe().video_encoders`` — se
  NVENC não está ativo, a interface diz o motivo real (campo ``notes``);
* fontes, versões e tamanhos de disco são medidos no computador do usuário;
* o que custa caro (ffprobe, ``fc-list``, teste de API, limpeza de cache) roda
  no ``JobRunner``, nunca na thread da interface.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (QCheckBox, QComboBox, QFileDialog, QFrame,
                               QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit,
                               QPushButton, QScrollArea, QSizePolicy,
                               QSpinBox, QDoubleSpinBox, QTabWidget,
                               QVBoxLayout, QWidget)

from .. import theme
from ..widgets.busy import run_job
from ..widgets.segmented import SegmentedControl
from ...i18n import t
from ... import paths

log = logging.getLogger("corteia.ui.settings")


class SettingsPage(QWidget):
    def __init__(self, ctx: Any, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self.cfg = ctx.config
        self._loading = True
        self._hw: Any = None
        self._fonts: list[str] = []

        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 20, 24, 20)
        outer.setSpacing(12)

        head = QHBoxLayout()
        title = QLabel(t("Configurações"), self)
        title.setProperty("role", "title")
        head.addWidget(title)
        head.addStretch(1)
        self.btn_reset = QPushButton(t("Restaurar padrões"), self)
        self.btn_reset.setProperty("variant", "danger")
        self.btn_reset.setProperty("size", "sm")
        self.btn_reset.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_reset.clicked.connect(self._reset_defaults)
        head.addWidget(self.btn_reset)
        outer.addLayout(head)

        self.tabs = QTabWidget(self)
        self.tabs.setDocumentMode(True)
        outer.addWidget(self.tabs, 1)

        self.tab_interface = self._build_interface()
        self.tab_proc = self._build_processing()
        self.tab_export = self._build_export()
        self.tab_api = self._build_api()
        self.tab_subs = self._build_subtitles()
        self.tab_store = self._build_storage()
        self.tab_diag = self._build_diagnostic()
        for widget, label in (
            (self.tab_interface, t("Interface")),
            (self.tab_proc, t("Processamento")),
            (self.tab_export, t("Exportação")),
            (self.tab_api, t("APIs externas")),
            (self.tab_subs, t("Legenda")),
            (self.tab_store, t("Armazenamento")),
            (self.tab_diag, t("Diagnóstico")),
        ):
            self.tabs.addTab(widget, label)

        QTimer.singleShot(50, self._load_heavy)
        QTimer.singleShot(0, self._load_values)
        self._loading = False

    # ------------------------------------------------------------------ abas
    def _scroll_body(self) -> tuple[QWidget, QVBoxLayout]:
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        body = QWidget(scroll)
        body.setProperty("transparent", "true")
        scroll.setWidget(body)
        lay = QVBoxLayout(body)
        lay.setContentsMargins(18, 16, 18, 18)
        lay.setSpacing(10)
        return scroll, lay

    def _build_interface(self) -> QWidget:
        scroll, lay = self._scroll_body()

        lay.addWidget(_group(t("Idioma da interface")))
        self.lang_seg = SegmentedControl([("pt", "Português"), ("en", "English")], self)
        self.lang_seg.changed.connect(lambda k: self._set_pair("ui.language", k))
        lay.addWidget(_row(t("Idioma"), self.lang_seg, t("Aplicado na hora, sem reiniciar.")))

        lay.addWidget(_group(t("Aparência")))
        self.theme_seg = SegmentedControl([("dark", t("Escuro")), ("light", t("Claro"))], self)
        self.theme_seg.changed.connect(self._on_theme)
        lay.addWidget(_row(t("Tema"), self.theme_seg, t("Os dois temas vêm dos QSS do pacote.")))

        self.accent_combo = QComboBox(self)
        for value, label in (("#7C5CFF", t("Roxo (padrão)")), ("#22E07A", t("Verde")),
                             ("#4EC5FF", t("Azul")), ("#FFB020", t("Âmbar")),
                             ("#FF5C7A", t("Rosa"))):
            self.accent_combo.addItem(label, value)
        self.accent_combo.currentIndexChanged.connect(
            lambda _i: self._set_pair("ui.accent", str(self.accent_combo.currentData())))
        lay.addWidget(_row(t("Cor de destaque"), self.accent_combo, ""))

        lay.addWidget(_group(t("Comportamento")))
        self.first_run_check = QCheckBox(t("Mostrar o assistente na próxima abertura"), self)
        self.first_run_check.toggled.connect(
            lambda v: self._set_pair("ui.first_run", bool(v), cast=bool))
        lay.addWidget(self.first_run_check)
        lay.addStretch(1)
        return scroll

    def _build_processing(self) -> QWidget:
        scroll, lay = self._scroll_body()

        lay.addWidget(_group(t("Transcrição (Whisper)")))
        self.model_combo = QComboBox(self)
        self.model_combo.currentIndexChanged.connect(
            lambda _i: self._set_pair("subs.model", str(self.model_combo.currentData())))
        lay.addWidget(_row(t("Modelo padrão"), self.model_combo,
                           t("Modelos menores são mais rápidos; o grande vale a pena só com GPU.")))
        self.lang_asr = QComboBox(self)
        for code, name in (("pt", "Português (Brasil)"), ("en", "English"), ("es", "Español"),
                           ("fr", "Français"), ("de", "Deutsch"), ("auto", t("detectar"))):
            self.lang_asr.addItem(name, code)
        self.lang_asr.currentIndexChanged.connect(
            lambda _i: self._set_pair("subs.language", str(self.lang_asr.currentData())))
        lay.addWidget(_row(t("Idioma da fala"), self.lang_asr, ""))
        self.beam_spin = QSpinBox(self)
        self.beam_spin.setRange(1, 10)
        self.beam_spin.valueChanged.connect(lambda v: self._set_pair("subs.beam_size", int(v), cast=int))
        lay.addWidget(_row(t("Beam size"), self.beam_spin, t("Maior = mais preciso e mais lento.")))
        self.words_check = QCheckBox(t("Marcação palavra a palavra (para legenda dinâmica)"), self)
        self.words_check.toggled.connect(lambda v: self._set_pair("subs.word_timestamps", bool(v), cast=bool))
        self.diar_check = QCheckBox(t("Separar falantes (diarização)"), self)
        self.diar_check.toggled.connect(lambda v: self._set_pair("subs.diarize", bool(v), cast=bool))
        lay.addWidget(self.words_check)
        lay.addWidget(self.diar_check)

        lay.addWidget(_group(t("Execução")))
        self.device_seg = SegmentedControl([("auto", t("Auto")), ("cpu", "CPU"), ("cuda", "CUDA")], self)
        self.device_seg.changed.connect(lambda k: self._set_pair("proc.device", k))
        lay.addWidget(_row(t("Dispositivo"), self.device_seg, t("CUDA só aparece como útil se a GPU responder.")))
        self.compute_combo = QComboBox(self)
        for value in ("auto", "float32", "float16", "int8"):
            self.compute_combo.addItem(value, value)
        self.compute_combo.currentIndexChanged.connect(
            lambda _i: self._set_pair("proc.compute_type", str(self.compute_combo.currentData())))
        lay.addWidget(_row(t("Precisão numérica"), self.compute_combo, ""))
        self.threads_spin = QSpinBox(self)
        self.threads_spin.setRange(0, 128)
        self.threads_spin.valueChanged.connect(
            lambda v: self._set_pair("proc.cpu_threads", int(v), cast=int,
                                     extra=("performance.cpu_threads",)))
        lay.addWidget(_row(t("Threads de CPU (0 = automático)"), self.threads_spin, ""))
        self.parallel_spin = QSpinBox(self)
        self.parallel_spin.setRange(1, 16)
        self.parallel_spin.valueChanged.connect(
            lambda v: self._set_pair("proc.max_parallel_jobs", int(v), cast=int))
        lay.addWidget(_row(t("Exportações em paralelo"), self.parallel_spin,
                           t("Limitado pela RAM disponível.")))

        lay.addWidget(_group(t("Silêncios")))
        self.db_spin = QDoubleSpinBox(self)
        self.db_spin.setRange(-80.0, -6.0)
        self.db_spin.setDecimals(1)
        self.db_spin.setSuffix(" dB")
        self.db_spin.valueChanged.connect(
            lambda v: self._set_pair("proc.silence_db", float(v), cast=float,
                                     extra=("silence.db",)))
        lay.addWidget(_row(t("Piso de silêncio"), self.db_spin,
                           t("Mais perto de 0 dB corta só pausas bem altas.")))
        self.ms_spin = QSpinBox(self)
        self.ms_spin.setRange(50, 5000)
        self.ms_spin.setSingleStep(50)
        self.ms_spin.setSuffix(" ms")
        self.ms_spin.valueChanged.connect(
            lambda v: self._set_pair("proc.silence_min_ms", int(v), cast=int,
                                     extra=("silence.min_ms",)))
        lay.addWidget(_row(t("Pausa mínima"), self.ms_spin, ""))

        lay.addWidget(_group(t("Cenas / planos")))
        self.scene_spin = QDoubleSpinBox(self)
        self.scene_spin.setRange(5.0, 90.0)
        self.scene_spin.setDecimals(1)
        self.scene_spin.valueChanged.connect(
            lambda v: self._set_pair("proc.scene_threshold", float(v), cast=float,
                                     extra=("scenes.threshold",)))
        lay.addWidget(_row(t("Sensibilidade de corte de cena"), self.scene_spin,
                           t("Valor menor detecta mais trocas de plano.")))
        self.scenemin_spin = QSpinBox(self)
        self.scenemin_spin.setRange(2, 240)
        self.scenemin_spin.valueChanged.connect(
            lambda v: self._set_pair("proc.scene_min_len", int(v), cast=int))
        lay.addWidget(_row(t("Mínimo de quadros por plano"), self.scenemin_spin, ""))
        lay.addStretch(1)
        return scroll

    def _build_export(self) -> QWidget:
        scroll, lay = self._scroll_body()

        lay.addWidget(_group(t("Arquivo final")))
        self.res_combo = QComboBox(self)
        for value in ("720p", "1080p", "4K"):
            self.res_combo.addItem(value, value)
        self.res_combo.currentIndexChanged.connect(
            lambda _i: self._set_pair("export.resolution", str(self.res_combo.currentData())))
        lay.addWidget(_row(t("Resolução"), self.res_combo, t("Refere-se ao lado menor do recorte.")))
        self.aspect_combo = QComboBox(self)
        for value, label in (("9:16", "9:16 · TikTok / Reels / Shorts"), ("16:9", "16:9 · YouTube"),
                             ("1:1", "1:1"), ("4:5", "4:5")):
            self.aspect_combo.addItem(label, value)
        self.aspect_combo.currentIndexChanged.connect(
            lambda _i: self._set_pair("analysis.aspect", str(self.aspect_combo.currentData())))
        lay.addWidget(_row(t("Proporção"), self.aspect_combo, ""))
        self.fps_combo = QComboBox(self)
        for value in (24, 25, 30, 50, 60):
            self.fps_combo.addItem(f"{value} fps", value)
        self.fps_combo.currentIndexChanged.connect(
            lambda _i: self._set_pair("export.fps", int(self.fps_combo.currentData()), cast=int))
        lay.addWidget(_row(t("Quadros por segundo"), self.fps_combo, ""))
        self.crf_spin = QSpinBox(self)
        self.crf_spin.setRange(14, 40)
        self.crf_spin.valueChanged.connect(
            lambda v: self._set_pair("export.quality_crf", int(v), cast=int))
        lay.addWidget(_row(t("Qualidade (CRF — menor é melhor)"), self.crf_spin,
                           t("CRF 20 é o padrão; 23 economiza espaço.")))
        self.preset_combo = QComboBox(self)
        for value in ("ultrafast", "superfast", "veryfast", "faster", "fast", "medium",
                      "slow", "slower"):
            self.preset_combo.addItem(value, value)
        self.preset_combo.currentIndexChanged.connect(
            lambda _i: self._set_pair("export.preset", str(self.preset_combo.currentData())))
        lay.addWidget(_row(t("Preset de velocidade"), self.preset_combo,
                           t("Mais lento = arquivo menor pela mesma qualidade.")))
        self.audio_combo = QComboBox(self)
        for value in ("128k", "160k", "192k", "256k", "320k"):
            self.audio_combo.addItem(value, value)
        self.audio_combo.currentIndexChanged.connect(
            lambda _i: self._set_pair("export.audio_bitrate", str(self.audio_combo.currentData())))
        lay.addWidget(_row(t("Áudio"), self.audio_combo, ""))
        self.burn_check = QCheckBox(t("Gravar legendas no vídeo (burn-in)"), self)
        self.burn_check.toggled.connect(lambda v: self._set_pair("export.burn_subtitles", bool(v), cast=bool))
        lay.addWidget(self.burn_check)

        lay.addWidget(_group(t("Codificador de vídeo")))
        self.codec_combo = QComboBox(self)
        self.codec_combo.currentIndexChanged.connect(
            lambda _i: self._set_pair("export.codec", str(self.codec_combo.currentData())))
        lay.addWidget(_row(t("Codificador"), self.codec_combo, ""))
        self.codec_note = QLabel("", self)
        self.codec_note.setProperty("role", "muted")
        self.codec_note.setWordWrap(True)
        lay.addWidget(self.codec_note)

        lay.addWidget(_group(t("Nome dos arquivos")))
        self.pattern_edit = QLineEdit(self)
        self.pattern_edit.setToolTip(t("Campos: {index}, {title}, {start}, {end}, {dur}, {project}, {score}"))
        self.pattern_edit.editingFinished.connect(
            lambda: self._set_pair("export.pattern", self.pattern_edit.text().strip()))
        lay.addWidget(self.pattern_edit)
        self.pattern_hint = QLabel("", self)
        self.pattern_hint.setProperty("role", "mono")
        self.pattern_hint.setWordWrap(True)
        lay.addWidget(self.pattern_hint)
        lay.addStretch(1)
        return scroll

    def _build_api(self) -> QWidget:
        scroll, lay = self._scroll_body()

        lay.addWidget(_group(t("Provedor de IA")))
        self.provider_seg = SegmentedControl([("off", t("Desligado")), ("openai", "OpenAI"),
                                             ("ollama", "Ollama")], self)
        self.provider_seg.changed.connect(self._on_provider)
        lay.addWidget(_row(t("Provedor"), self.provider_seg,
                           t("Desligado usa apenas o modelo de pontuação local.")))
        self.base_edit = QLineEdit(self)
        self.base_edit.setPlaceholderText("https://api.openai.com/v1")
        self.base_edit.editingFinished.connect(
            lambda: self._set_pair("api.base_url", self.base_edit.text().strip()))
        lay.addWidget(_row(t("URL base"), self.base_edit, ""))
        self.key_edit = QLineEdit(self)
        self.key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.key_edit.setPlaceholderText(t("sk-…  (não sai deste computador)"))
        self.key_edit.editingFinished.connect(
            lambda: self._set_pair("api.key", self.key_edit.text().strip()))
        lay.addWidget(_row(t("Chave de API"), self.key_edit, ""))
        self.btn_show_key = QPushButton(t("Mostrar"), self)
        self.btn_show_key.setProperty("variant", "ghost")
        self.btn_show_key.setProperty("size", "sm")
        self.btn_show_key.setCheckable(True)
        self.btn_show_key.toggled.connect(
            lambda on: self.key_edit.setEchoMode(
                QLineEdit.EchoMode.Normal if on else QLineEdit.EchoMode.Password))
        lay.addWidget(self.btn_show_key)
        self.model_edit = QLineEdit(self)
        self.model_edit.setPlaceholderText("gpt-4o-mini / llama3")
        self.model_edit.editingFinished.connect(
            lambda: self._set_pair("api.model", self.model_edit.text().strip()))
        lay.addWidget(_row(t("Modelo"), self.model_edit,
                           t("Em Ollama use o nome exato de \"ollama list\".")))
        self.temp_spin = QDoubleSpinBox(self)
        self.temp_spin.setRange(0.0, 2.0)
        self.temp_spin.setSingleStep(0.1)
        self.temp_spin.setDecimals(2)
        self.temp_spin.valueChanged.connect(
            lambda v: self._set_pair("api.temperature", float(v), cast=float))
        lay.addWidget(_row(t("Temperatura"), self.temp_spin, t("0 = sempre a mesma resposta.")))
        self.tokens_spin = QSpinBox(self)
        self.tokens_spin.setRange(256, 32000)
        self.tokens_spin.setSingleStep(256)
        self.tokens_spin.valueChanged.connect(
            lambda v: self._set_pair("api.max_output_tokens", int(v), cast=int))
        lay.addWidget(_row(t("Máximo de tokens de saída"), self.tokens_spin, ""))
        self.timeout_spin = QSpinBox(self)
        self.timeout_spin.setRange(5, 600)
        self.timeout_spin.setSuffix(" s")
        self.timeout_spin.valueChanged.connect(
            lambda v: self._set_pair("api.timeout", int(v), cast=int))
        lay.addWidget(_row(t("Tempo limite"), self.timeout_spin, ""))
        self.fallback_check = QCheckBox(t("Se a API falhar, usar a pontuação local"), self)
        self.fallback_check.toggled.connect(
            lambda v: self._set_pair("api.offline_fallback", bool(v), cast=bool))
        lay.addWidget(self.fallback_check)

        row = QHBoxLayout()
        self.btn_test = QPushButton(t("Testar conexão"), self)
        self.btn_test.setProperty("variant", "primary")
        self.btn_test.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_test.clicked.connect(self.test_connection)
        row.addWidget(self.btn_test)
        self.btn_test.setEnabled(False)
        row.addStretch(1)
        lay.addLayout(row)
        self.api_status = QLabel(t("Aguardando diagnóstico do provedor…"), self)
        self.api_status.setProperty("role", "mono")
        self.api_status.setWordWrap(True)
        self.api_status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        lay.addWidget(self.api_status)

        note = QLabel(t("Modelo local (arquivo .gguf ou Ollama nesta máquina) não exige "
                        "chave: deixe a chave em branco e informe só o nome do modelo."), self)
        note.setProperty("role", "warn")
        note.setWordWrap(True)
        lay.addWidget(note)
        lay.addStretch(1)
        return scroll

    def _build_subtitles(self) -> QWidget:
        scroll, lay = self._scroll_body()

        lay.addWidget(_group(t("Fonte da legenda")))
        self.font_combo = QComboBox(self)
        self.font_combo.setEditable(True)
        self.font_combo.currentIndexChanged.connect(
            lambda _i: self._on_font_choice(self.font_combo.currentText()))
        self.font_combo.lineEdit().editingFinished.connect(
            lambda: self._on_font_choice(self.font_combo.currentText()))
        lay.addWidget(_row(t("Fonte"), self.font_combo, ""))
        self.font_note = QLabel(t("Lendo as fontes instaladas…"), self)
        self.font_note.setProperty("role", "muted")
        self.font_note.setWordWrap(True)
        lay.addWidget(self.font_note)

        self.size_spin = QSpinBox(self)
        self.size_spin.setRange(0, 200)
        self.size_spin.valueChanged.connect(
            lambda v: self._set_pair("subs.size", int(v), cast=int))
        lay.addWidget(_row(t("Tamanho (0 = automático)"), self.size_spin,
                           t("Automático calcula pela altura do recorte.")))
        self.upper_check = QCheckBox(t("Texto em MAIÚSCULAS"), self)
        self.upper_check.toggled.connect(lambda v: self._set_pair("subs.uppercase", bool(v), cast=bool))
        lay.addWidget(self.upper_check)

        lay.addWidget(_group(t("Estilo")))
        self.style_combo = QComboBox(self)
        try:
            from ...models import SubtitleStyle

            for st in SubtitleStyle:
                self.style_combo.addItem(st.label, st.value)
        except Exception:  # noqa: BLE001
            self.style_combo.addItem("viral", "viral")
        self.style_combo.currentIndexChanged.connect(
            lambda _i: self._set_pair("subs.default_style", str(self.style_combo.currentData())))
        lay.addWidget(_row(t("Estilo padrão"), self.style_combo, ""))
        self.outline_spin = QSpinBox(self)
        self.outline_spin.setRange(0, 8)
        self.outline_spin.valueChanged.connect(
            lambda v: self._set_pair("subs.outline", int(v), cast=int))
        lay.addWidget(_row(t("Contorno"), self.outline_spin, ""))
        self.position_combo = QComboBox(self)
        for value, label in (("bottom", t("parte de baixo")), ("center", t("centro")),
                             ("top", t("topo"))):
            self.position_combo.addItem(label, value)
        self.position_combo.currentIndexChanged.connect(
            lambda _i: self._set_pair("subs.position", str(self.position_combo.currentData())))
        lay.addWidget(_row(t("Posição"), self.position_combo, ""))
        self.cps_spin = QSpinBox(self)
        self.cps_spin.setRange(8, 60)
        self.cps_spin.valueChanged.connect(
            lambda v: self._set_pair("subs.reading_speed_cps", int(v), cast=int))
        lay.addWidget(_row(t("Velocidade de leitura (caracteres/s)"), self.cps_spin, ""))
        lay.addStretch(1)
        return scroll

    def _build_storage(self) -> QWidget:
        scroll, lay = self._scroll_body()

        lay.addWidget(_group(t("Pasta de exportação")))
        row = QHBoxLayout()
        self.dir_edit = QLineEdit(self)
        self.dir_edit.editingFinished.connect(
            lambda: self._set_pair("export.dir", self.dir_edit.text().strip()))
        row.addWidget(self.dir_edit, 1)
        self.btn_dir = QPushButton(t("Escolher"), self)
        self.btn_dir.setProperty("variant", "outline")
        self.btn_dir.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_dir.clicked.connect(self._choose_export_dir)
        row.addWidget(self.btn_dir)
        self.btn_open_dir = QPushButton(t("Abrir"), self)
        self.btn_open_dir.setProperty("variant", "ghost")
        self.btn_open_dir.setIcon(theme.icon("folder", "muted", 18))
        self.btn_open_dir.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_open_dir.clicked.connect(lambda: self._open_path(self.dir_edit.text()))
        row.addWidget(self.btn_open_dir)
        lay.addLayout(row)

        self.consent_check = QCheckBox(t("Permitir download de vídeos por link (yt-dlp)"), self)
        self.consent_check.setToolTip(t("Sem essa permissão a página Importar recusa links."))
        self.consent_check.toggled.connect(
            lambda v: self._set_pair("net.download_consent", bool(v), cast=bool))
        lay.addWidget(self.consent_check)

        lay.addWidget(_group(t("Espaço em disco (medido agora)")))
        self.storage_label = QLabel(t("Medindo…"), self)
        self.storage_label.setProperty("role", "mono")
        self.storage_label.setWordWrap(True)
        lay.addWidget(self.storage_label)
        row2 = QHBoxLayout()
        self.btn_remeasure = QPushButton(t("Medir de novo"), self)
        self.btn_remeasure.setProperty("variant", "outline")
        self.btn_remeasure.setProperty("size", "sm")
        self.btn_remeasure.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_remeasure.clicked.connect(self._measure_storage)
        self.btn_clean = QPushButton(t("Limpar arquivos temporários"), self)
        self.btn_clean.setProperty("variant", "danger")
        self.btn_clean.setProperty("size", "sm")
        self.btn_clean.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_clean.clicked.connect(self._clean_temp)
        self.btn_open_temp = QPushButton(t("Abrir pasta temporária"), self)
        self.btn_open_temp.setProperty("variant", "ghost")
        self.btn_open_temp.setProperty("size", "sm")
        self.btn_open_temp.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_open_temp.clicked.connect(lambda: self._open_path(paths.temp_root()))
        for b in (self.btn_remeasure, self.btn_clean, self.btn_open_temp):
            row2.addWidget(b)
        row2.addStretch(1)
        lay.addLayout(row2)

        lay.addWidget(_group(t("Modelos Whisper instalados")))
        self.models_label = QLabel(t("Consultando o disco…"), self)
        self.models_label.setProperty("role", "mono")
        self.models_label.setWordWrap(True)
        lay.addWidget(self.models_label)
        lay.addStretch(1)
        return scroll

    def _build_diagnostic(self) -> QWidget:
        scroll, lay = self._scroll_body()

        self.diag_text = QPlainTextEdit(self)
        self.diag_text.setReadOnly(True)
        self.diag_text.setMaximumBlockCount(4000)
        self.diag_text.setPlaceholderText(t("Coletando informação real do sistema…"))
        self.diag_text.setMinimumHeight(320)
        lay.addWidget(self.diag_text, 1)

        row = QHBoxLayout()
        self.btn_copy = QPushButton(t("Copiar relatório"), self)
        self.btn_copy.setProperty("variant", "outline")
        self.btn_copy.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_copy.clicked.connect(self._copy_report)
        self.btn_refresh_diag = QPushButton(t("Atualizar"), self)
        self.btn_refresh_diag.setProperty("variant", "ghost")
        self.btn_refresh_diag.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_refresh_diag.clicked.connect(self._load_heavy)
        row.addWidget(self.btn_copy)
        row.addWidget(self.btn_refresh_diag)
        row.addStretch(1)
        lay.addLayout(row)
        return scroll

    # ------------------------------------------------------------ carregar/onibus
    def _load_values(self) -> None:
        cfg = self.cfg
        self.lang_seg.setValue(cfg.str_("ui.language", "pt"), notify=False)
        self.theme_seg.setValue(cfg.str_("ui.theme", "dark"), notify=False)
        _set_combo(self.accent_combo, cfg.str_("ui.accent", "#7C5CFF"))
        self.first_run_check.setChecked(cfg.bool_("ui.first_run", True))

        self.device_seg.setValue(cfg.str_("proc.device", "auto"), notify=False)
        _set_combo(self.compute_combo, cfg.str_("proc.compute_type", "auto"))
        self.threads_spin.setValue(cfg.int_("proc.cpu_threads", 0))
        self.parallel_spin.setValue(cfg.int_("proc.max_parallel_jobs", 2))
        self.db_spin.setValue(cfg.float_("proc.silence_db", -35.0))
        self.ms_spin.setValue(cfg.int_("proc.silence_min_ms", 350))
        self.scene_spin.setValue(cfg.float_("proc.scene_threshold", 27.0))
        self.scenemin_spin.setValue(cfg.int_("proc.scene_min_len", 15))
        self.words_check.setChecked(cfg.bool_("subs.word_timestamps", True))
        self.diar_check.setChecked(cfg.bool_("subs.diarize", False))
        self.beam_spin.setValue(cfg.int_("subs.beam_size", 5))

        _set_combo(self.res_combo, cfg.str_("export.resolution", "1080p"))
        _set_combo(self.aspect_combo, cfg.str_("analysis.aspect", "9:16"))
        self.fps_combo.setCurrentIndex(max(0, self.fps_combo.findData(cfg.int_("export.fps", 30))))
        self.crf_spin.setValue(cfg.int_("export.quality_crf", 20))
        _set_combo(self.preset_combo, cfg.str_("export.preset", "fast"))
        _set_combo(self.audio_combo, cfg.str_("export.audio_bitrate", "192k"))
        self.burn_check.setChecked(cfg.bool_("export.burn_subtitles", True))
        self.pattern_edit.setText(cfg.str_("export.pattern", "{index:02d}-{title}-corteia"))
        self._update_pattern_hint()

        _set_combo(self.model_combo, cfg.str_("subs.model", "base"))
        _set_combo(self.lang_asr, cfg.str_("subs.language", "pt"))

        self.provider_seg.setValue(cfg.str_("api.provider", "off"), notify=False)
        self.base_edit.setText(cfg.str_("api.base_url", ""))
        self.key_edit.setText(cfg.str_("api.key", ""))
        self.model_edit.setText(cfg.str_("api.model", ""))
        self.temp_spin.setValue(cfg.float_("api.temperature", 0.3))
        self.tokens_spin.setValue(cfg.int_("api.max_output_tokens", 4000))
        self.timeout_spin.setValue(cfg.int_("api.timeout", 60))
        self.fallback_check.setChecked(cfg.bool_("api.offline_fallback", True))

        self.size_spin.setValue(cfg.int_("subs.size", 0))
        self.upper_check.setChecked(cfg.bool_("subs.uppercase", True))
        _set_combo(self.style_combo, cfg.str_("subs.default_style", "viral"))
        self.outline_spin.setValue(cfg.int_("subs.outline", 3))
        _set_combo(self.position_combo, cfg.str_("subs.position", "bottom"))
        self.cps_spin.setValue(cfg.int_("subs.reading_speed_cps", 26))

        self.dir_edit.setText(cfg.str_("export.dir", str(paths.exports_dir_default())))
        self.consent_check.setChecked(cfg.bool_("net.download_consent", False))

    def _load_heavy(self) -> None:
        """Hardware, fontes, modelos e disco — tudo fora da thread da interface."""
        def work(progress) -> dict:
            from ... import hardware
            from ...core import fonts as FT, transcribe as TR

            out: dict[str, Any] = {}
            progress(10.0, t("lendo hardware e FFmpeg"))
            try:
                hw = hardware.probe()
                out["hw"] = hw
                out["encoders"] = list(hw.video_encoders)
            except Exception as exc:  # noqa: BLE001
                out["hw_error"] = str(exc)
                out["encoders"] = []
            progress(40.0, t("listando fontes instaladas"))
            try:
                out["fonts"] = FT.installed()
            except Exception as exc:  # noqa: BLE001
                out["fonts"] = []
                out["fonts_error"] = str(exc)
            progress(65.0, t("medindo espaço em disco"))
            try:
                out["installed"] = {k: TR.is_downloaded(k) for k in _model_keys()}
            except Exception as exc:  # noqa: BLE001
                out["installed"] = {}
                out["installed_error"] = str(exc)
            progress(85.0, t("contando arquivos temporários"))
            try:
                out["temp_bytes"] = paths.temp_usage_bytes()
                out["data_bytes"] = paths.projects_usage_bytes()
                out["models_bytes"] = paths.dir_size(paths.models_dir())
                out["free_bytes"] = paths.free_bytes_temp()
            except Exception as exc:  # noqa: BLE001
                out["disk_error"] = str(exc)
            return out

        def done(result: Any) -> None:
            data = result or {}
            self._hw = data.get("hw")
            self._fonts = list(data.get("fonts") or [])
            self._apply_heavy(data)

        def error(msg: str) -> None:
            self.diag_text.setPlainText(f"{t('Diagnóstico indisponível')}: {msg}")
            self.font_note.setText(f"{t('Não foi possível listar as fontes')}: {msg}")

        run_job(self.ctx, work, name=t("diagnóstico"), on_done=done, on_error=error, busy=False)

    def _apply_heavy(self, data: dict) -> None:
        encoders = list(data.get("encoders") or [])
        self.codec_combo.blockSignals(True)
        self.codec_combo.clear()
        labels = {"libx264": "H.264 (x264 · CPU)", "libx265": "H.265 (x265 · CPU)",
                  "h264_nvenc": "H.264 (NVENC · GPU NVIDIA)", "hevc_nvenc": "H.265 (NVENC · GPU NVIDIA)",
                  "h264_amf": "H.264 (AMF · AMD)", "hevc_amf": "H.265 (AMF · AMD)",
                  "h264_qsv": "H.264 (Quick Sync · Intel)", "hevc_qsv": "H.265 (Quick Sync · Intel)",
                  "h264_videotoolbox": "H.264 (VideoToolbox · Apple)", "mpeg4": "MPEG-4 (CPU)"}
        available = encoders or ["libx264"]
        for value in available:
            self.codec_combo.addItem(labels.get(value, value), value)
        current = self.cfg.str_("export.codec", "libx264")
        _set_combo(self.codec_combo, current)
        if self.codec_combo.currentIndex() < 0:
            self.codec_combo.setCurrentIndex(0)
            self.cfg.set("export.codec", str(self.codec_combo.currentData()))
        self.codec_combo.blockSignals(False)

        nvenc = [e for e in available if "nvenc" in e]
        notes = list(getattr(self._hw, "notes", []) or [])
        lines: list[str] = []
        if nvenc:
            if getattr(self._hw, "cuda_available", False):
                lines.append(t("NVENC disponível e com CUDA ativo: codificação por GPU liberada."))
            else:
                lines.append(t("Este FFmpeg tem NVENC, mas a codificação vai rodar em CPU: "
                               "{why}").format(why=" ".join(notes) or t("nenhum driver NVIDIA respondeu")))
        else:
            lines.append(t("Este FFmpeg não traz NVENC — só codificadores de CPU. "
                           "Para usar a GPU, instale um FFmpeg com h264_nvenc."))
        if not getattr(self._hw, "subtitles_filter", False):
            lines.append(t("Sem o filtro 'subtitles' (libass): as legendas não serão gravadas."))
        self.codec_note.setText("\n".join(lines))

        fonts = self._fonts
        want = self.cfg.str_("subs.font", "")
        self.font_combo.blockSignals(True)
        self.font_combo.clear()
        if fonts:
            self.font_combo.addItems(fonts)
        for guess in ("Arial Black", "Inter", "Roboto", "Noto Sans", "DejaVu Sans", "Arial"):
            if guess not in fonts:
                self.font_combo.addItem(guess)
        self.font_combo.setCurrentText(want or (fonts[0] if fonts else "DejaVu Sans"))
        self.font_combo.blockSignals(False)
        self._on_font_choice(self.font_combo.currentText(), save=False)
        if not fonts:
            self.font_note.setText(f"{t('Nenhuma fonte foi encontrada')}: "
                                   f"{data.get('fonts_error') or 'fc-list vazio'}")

        installed = dict(data.get("installed") or {})
        self.model_combo.blockSignals(True)
        self.model_combo.clear()
        for key in _model_keys():
            self.model_combo.addItem(key, key)
        _set_combo(self.model_combo, self.cfg.str_("subs.model", "base"))
        self.model_combo.blockSignals(False)

        from ... import paths as P

        bits = []
        for key in _model_keys():
            d = P.models_dir() / key
            size = P.dir_size(d) if installed.get(key) else 0
            bits.append(f"{key}: " + (P.human_bytes(size) if size else t("não instalado")))
        self.models_label.setText("  ·  ".join(bits) if bits else t("Modelo indisponível para leitura."))

        self._storage_cache = {
            "temp": int(data.get("temp_bytes") or 0), "data": int(data.get("data_bytes") or 0),
            "models": int(data.get("models_bytes") or 0), "free": int(data.get("free_bytes") or 0),
        }
        self._render_storage()
        self.diag_text.setPlainText(self._report_text(data))

    def _measure_storage(self) -> None:
        def work(progress) -> dict:
            progress(20.0, t("contando arquivos temporários"))
            return {"temp": paths.temp_usage_bytes(), "data": paths.projects_usage_bytes(),
                    "models": paths.dir_size(paths.models_dir()),
                    "free": paths.free_bytes_temp()}

        def done(result: Any) -> None:
            self._storage_cache = {k: int((result or {}).get(k, 0) or 0)
                                   for k in ("temp", "data", "models", "free")}
            self._render_storage()
            self.diag_text.setPlainText(self._report_text({"hw": self._hw, "fonts": self._fonts}))

        run_job(self.ctx, work, name=t("medir disco"), on_done=done, busy=False)

    def _render_storage(self) -> None:
        cache = getattr(self, "_storage_cache", {})

        def h(key: str) -> str:
            return paths.human_bytes(int(cache.get(key, 0) or 0))

        self.storage_label.setText(
            t("temporários {temp} · dados/config {data} · modelos {models} · espaço livre {free}")
            .format(temp=h("temp"), data=h("data"), models=h("models"), free=h("free") or "?"))

    def _clean_temp(self) -> None:
        before = int(getattr(self, "_storage_cache", {}).get("temp", 0) or paths.temp_usage_bytes())
        from PySide6.QtWidgets import QMessageBox

        answer = QMessageBox.question(
            self, t("Limpar arquivos temporários"),
            t("Serão apagados os arquivos de {path} ({size}). Cortes já exportados NÃO são tocados.\n"
              "Não rode nenhuma exportação durante a limpeza. Continuar?").format(
                  path=paths.temp_root(), size=paths.human_bytes(before)),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if answer != QMessageBox.StandardButton.Yes:
            return

        def work(progress) -> int:
            progress(20.0, t("apagando arquivos temporários"))
            return int(paths.clean_temp())

        def done(freed: Any) -> None:
            self._storage_cache = dict(getattr(self, "_storage_cache", {}))
            self._storage_cache["temp"] = max(0, before - int(freed or 0))
            self._render_storage()
            self.ctx.toast(t("Liberados {size} de arquivos temporários.")
                           .format(size=paths.human_bytes(int(freed or 0))), "ok")

        run_job(self.ctx, work, name=t("limpeza de temporários"), on_done=done, busy=False)

    # ---------------------------------------------------------------- handlers
    def _on_theme(self, key: str) -> None:
        self._set_pair("ui.theme", key)
        theme.apply(QGuiApplication.instance(), key, self.cfg.str_("ui.accent"))
        self.ctx.toast(t("Tema {name} aplicado.").format(name=t("Escuro") if key == "dark" else t("Claro")), "ok")

    def _on_provider(self, key: str) -> None:
        self._set_pair("api.provider", key)
        if key == "ollama" and not self.base_edit.text().strip():
            self.base_edit.setText("http://localhost:11434/v1")
            self._set_pair("api.base_url", self.base_edit.text())
        elif key == "openai" and not self.base_edit.text().strip():
            self.base_edit.setText("https://api.openai.com/v1")
            self._set_pair("api.base_url", self.base_edit.text())
        try:
            self.ctx.project.options.use_llm = key not in ("", "off")
        except Exception:  # noqa: BLE001
            pass

    def _on_font_choice(self, name: str, *, save: bool = True) -> None:
        name = (name or "").strip()
        if not name:
            return
        if save:
            self._set_pair("subs.font", name)
        try:
            from ...core import fonts as FT

            used, substituted = FT.pick(name)
            if substituted:
                self.font_note.setText(
                    t("A fonte “{want}” não está instalada; a legenda vai usar “{used}”.")
                    .format(want=name, used=used))
                self.font_note.setProperty("role", "warn")
            else:
                file_name = ""
                try:
                    file_name = Path(FT.file_for(name)).name
                except Exception:  # noqa: BLE001
                    pass
                self.font_note.setText(t("Fonte “{used}” instalada e será usada exatamente assim.")
                                       .format(used=used) + (f"  ({file_name})" if file_name else ""))
                self.font_note.setProperty("role", "ok")
        except Exception as exc:  # noqa: BLE001
            self.font_note.setText(f"{t('Não foi possível confirmar a fonte')}: {exc}")
            self.font_note.setProperty("role", "muted")
        self.font_note.style().unpolish(self.font_note)
        self.font_note.style().polish(self.font_note)
        if save:
            self._apply_subs_style()

    def _apply_subs_style(self) -> None:
        """Escreve o estilo no projeto em aberto (o exportador lê dali)."""
        try:
            from ...models import SubtitleSettings, SubtitleStyle

            settings = SubtitleSettings(
                style=SubtitleStyle(self.cfg.str_("subs.default_style", "viral")),
                font=self.cfg.str_("subs.font", "DejaVu Sans"),
                size=int(self.cfg.int_("subs.size", 0) or 0),
                outline=int(self.cfg.int_("subs.outline", 3) or 3),
                position=self.cfg.str_("subs.position", "bottom"),
                uppercase=bool(self.cfg.bool_("subs.uppercase", True)),
                reading_speed_cps=int(self.cfg.int_("subs.reading_speed_cps", 26) or 26),
            )
            self.ctx.project.export_settings.subtitle = settings  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001
            log.debug("estilo de legenda no projeto: %s", exc)

    def test_connection(self) -> None:
        self.btn_test.setEnabled(False)
        self.btn_test.setText(t("testando…"))
        self.api_status.setText(t("Chamando o provedor de verdade…"))
        self.api_status.setProperty("role", "muted")

        def work(progress) -> tuple[bool, str]:
            from ...core.llm import LLMClient

            progress(15.0, t("conectando"))
            return LLMClient(self.cfg).test_connection()

        def done(result: Any) -> None:
            ok, message = result if isinstance(result, tuple) and len(result) == 2 else (False, str(result))
            self.btn_test.setEnabled(True)
            self.btn_test.setText(t("Testar conexão"))
            self.api_status.setText(f"{'✓' if ok else '✗'} {message}")
            self.api_status.setProperty("role", "ok" if ok else "error")
            self.api_status.style().unpolish(self.api_status)
            self.api_status.style().polish(self.api_status)
            self.ctx.toast(message, "ok" if ok else "error")

        def error(msg: str) -> None:
            self.btn_test.setEnabled(True)
            self.btn_test.setText(t("Testar conexão"))
            self.api_status.setText(f"✗ {msg}")
            self.api_status.setProperty("role", "error")

        run_job(self.ctx, work, name=t("teste de API"), on_done=done, on_error=error, busy=False)

    def _choose_export_dir(self) -> None:
        start = self.dir_edit.text().strip() or str(paths.exports_dir_default())
        chosen = QFileDialog.getExistingDirectory(self, t("Pasta de exportação"), start)
        if not chosen:
            return
        self.dir_edit.setText(chosen)
        self._set_pair("export.dir", chosen)
        self.ctx.toast(t("Novos cortes serão gravados em {path}").format(path=chosen), "ok")

    def _open_path(self, target: Any) -> None:
        p = Path(str(target))
        if not str(target):
            self.ctx.toast(t("Caminho não informado."), "warn")
            return
        if not p.exists():
            try:
                p.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                self.ctx.toast(f"{t('Não foi possível criar')}: {exc}", "error")
                return
        ok, why = theme.open_location(p)
        if not ok:
            self.ctx.toast(why, "error")

    def _update_pattern_hint(self) -> None:
        pattern = self.cfg.str_("export.pattern", "{index:02d}-{title}-corteia")
        try:
            sample = pattern.format(index=1, title="exemplo-de-corte", start="12s", end="45s",
                                    dur="33s", project="meu-projeto", score=82)
        except (KeyError, IndexError, ValueError) as exc:
            sample = f"{t('padrão inválido')}: {exc}"
        self.pattern_hint.setText(f"{t('Exemplo do primeiro corte')}: {sample}.mp4")

    def _reset_defaults(self) -> None:
        from PySide6.QtWidgets import QMessageBox

        answer = QMessageBox.question(
            self, t("Restaurar padrões"),
            t("Todas as configurações voltam ao valor original (inclusive a chave de API). Continuar?"),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.cfg.reset()
        theme.apply(QGuiApplication.instance(), self.cfg.str_("ui.theme", "dark"),
                    self.cfg.str_("ui.accent"))
        self._load_values()
        self.ctx.toast(t("Configurações restauradas."), "ok")

    def open_api_tab(self) -> None:
        self.tabs.setCurrentWidget(self.tab_api)

    # ---------------------------------------------------------------- relatório
    def _report_text(self, data: dict) -> str:
        import platform
        import sys

        cfg = self.cfg
        hw = data.get("hw") if isinstance(data, dict) else self._hw
        lines: list[str] = [t("Relatório do CorteIA Studio"), ""]
        try:
            from PySide6 import __version__ as qt_v

            lines.append(f"PySide6: {qt_v}")
        except Exception:  # noqa: BLE001
            lines.append("PySide6: ?")
        try:
            import cv2

            lines.append(f"OpenCV: {cv2.__version__}")
        except Exception as exc:  # noqa: BLE001
            lines.append(f"OpenCV: {t('não instalado')} ({type(exc).__name__})")
        try:
            import faster_whisper

            lines.append(f"faster-whisper: {getattr(faster_whisper, '__version__', '?')}")
        except Exception:  # noqa: BLE001
            lines.append("faster-whisper: " + t("não instalado"))
        try:
            from ...hardware import find_ffmpeg

            ff_ver = getattr(hw, "ffmpeg_version", "") if hw is not None else ""
            ff_path = find_ffmpeg()
            lines.append(f"FFmpeg: {ff_ver or t('não encontrado')}"
                         + (f"  ({ff_path})" if ff_path else ""))
        except Exception:  # noqa: BLE001
            lines.append(f"FFmpeg: {getattr(hw, 'ffmpeg_version', '') or t('não encontrado')}")
        try:
            from scenedetect import __version__ as sd

            lines.append(f"PySceneDetect: {sd}")
        except Exception:  # noqa: BLE001
            pass
        lines.append(f"Python: {sys.version.split()[0]} · {platform.system()} {platform.release()}")
        lines.append("")
        if hw is not None:
            lines += hw.summary_lines()
            if data.get("hw_error"):
                lines.append(f"hardware: {data['hw_error']}")
        else:
            lines.append(t("Hardware não medido (a leitura falhou ou ainda está rodando)."))
        lines += ["", t("Fontes de legenda: {n} instaladas").format(n=len(self._fonts)),
                  t("Fonte escolhida: {f}").format(f=cfg.str_("subs.font") or "(padrão)"), ""]
        cache = getattr(self, "_storage_cache", {})
        lines.append(t("Disco: temporários {temp} · dados {data} · modelos {models} · livre {free}").format(
            temp=paths.human_bytes(int(cache.get("temp", 0) or 0)),
            data=paths.human_bytes(int(cache.get("data", 0) or 0)),
            models=paths.human_bytes(int(cache.get("models", 0) or 0)),
            free=paths.human_bytes(int(cache.get("free", 0) or 0))) if cache else t("Disco: não medido"))
        lines += ["",
                  f"{t('Configuração')}: {cfg.path}",
                  f"{t('Dados e projetos')}: {paths.data_dir()}",
                  f"{t('Cache/miniaturas')}: {paths.cache_dir()}",
                  f"{t('Temporários')}: {paths.temp_root()}",
                  f"{t('Modelos')}: {paths.models_dir()}",
                  f"{t('Exportação')}: {cfg.str_('export.dir') or paths.exports_dir_default()}",
                  f"{t('Logs')}: {paths.logs_dir()}", ""]
        st = None
        try:
            from ...core.llm import LLMClient

            st = LLMClient(cfg).status()
            lines.append(t("IA externa: {provider} · modelo {model} · {state}"
                           " {reason}").format(
                               provider=st.provider, model=st.model or "-",
                               state=t("pronto") if st.usable else t("inutilizável"),
                               reason=st.reason or ""))
        except Exception as exc:  # noqa: BLE001
            lines.append(f"IA externa: {exc}")
        return "\n".join(lines)

    def _copy_report(self) -> None:
        text = self.diag_text.toPlainText()
        if not text.strip():
            self._load_heavy()
            self.ctx.toast(t("Aguarde a leitura terminar e clique de novo."), "warn")
            return
        clip = QGuiApplication.clipboard()
        clip.setText(text)
        self.ctx.toast(t("Relatório copiado para a área de transferência."), "ok")

    # ------------------------------------------------------------------- ciclo
    def _set_pair(self, key: str, value: Any, *, cast: type | None = None,
                  extra: tuple[str, ...] = ()) -> None:
        """Grava a chave pedida + os alias que o núcleo também consulta."""
        if self._loading:
            return
        if cast is bool:
            value = bool(value)
        elif cast is int:
            try:
                value = int(value)
            except (TypeError, ValueError):
                return
        elif cast is float:
            try:
                value = float(value)
            except (TypeError, ValueError):
                return
        else:
            value = str(value)
        try:
            self.cfg.set(key, value)
        except Exception as exc:  # noqa: BLE001
            self.ctx.toast(f"{t('Não foi possível salvar')}: {exc}", "error")
            return
        for alias in _aliases(key) + tuple(extra):
            if alias != key:
                try:
                    self.cfg.set(alias, value)
                except Exception:  # noqa: BLE001
                    pass
        if key == "export.pattern":
            self._update_pattern_hint()
        elif key in ("ui.language",):
            self._retranslate_ui()
        elif key == "api.provider":
            self._refresh_api_status()

    def _aliases(self, key: str) -> tuple[str, ...]:
        return _aliases(key)

    def _refresh_api_status(self) -> None:
        try:
            from ...core.llm import LLMClient

            st = LLMClient(self.cfg).status()
            self.api_status.setText(
                f"{'✓' if st.usable else '✗'} {st.provider} · {st.model or '-'} · "
                f"{st.base_url or '-'}"
                + (f" — {st.reason}" if st.reason else ""))
            self.api_status.setProperty("role", "ok" if st.usable else "muted")
        except Exception as exc:  # noqa: BLE001
            self.api_status.setText(str(exc))
        self.api_status.style().unpolish(self.api_status)
        self.api_status.style().polish(self.api_status)
        self.btn_test.setEnabled(True)

    def _retranslate_ui(self) -> None:
        from ...i18n import set_language

        set_language(self.cfg.str_("ui.language", "pt"))
        win = self.window()
        fn = getattr(win, "_retranslate", None)
        if callable(fn):
            fn()

    def refresh(self) -> None:
        self._load_values()


# --------------------------------------------------------------------------- #
# chaves que o núcleo lê com dois nomes diferentes
# --------------------------------------------------------------------------- #
_ALIAS: dict[str, tuple[str, ...]] = {
    "subs.model": ("asr.model",),
    "subs.language": ("asr.language",),
    "proc.cpu_threads": ("performance.cpu_threads",),
    "proc.silence_db": ("silence.db",),
    "proc.silence_min_ms": ("silence.min_ms",),
    "proc.scene_threshold": ("scenes.threshold",),
    "export.pattern": ("export.filename_pattern",),
    "analysis.aspect": ("export.aspect",),
}


def _aliases(key: str) -> tuple[str, ...]:
    return _ALIAS.get(key, ())


def _model_keys() -> list[str]:
    try:
        from ...core.transcribe import MODELS

        return [k for k in MODELS if not MODELS[k].get("auto")]
    except Exception:  # noqa: BLE001
        return ["tiny", "base", "small", "medium", "large-v3", "distil-large-v3"]


def _group(text: str) -> QLabel:
    label = QLabel(text)
    label.setProperty("role", "section")
    return label


def _row(caption: str, widget: QWidget, hint: str = "") -> QWidget:
    host = QWidget()
    host.setProperty("transparent", "true")
    lay = QHBoxLayout(host)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(10)
    col = QVBoxLayout()
    col.setSpacing(1)
    lab = QLabel(caption, host)
    lab.setProperty("role", "sub")
    col.addWidget(lab)
    if hint:
        h = QLabel(hint, host)
        h.setProperty("role", "faint")
        h.setWordWrap(True)
        col.addWidget(h)
    lay.addLayout(col, 2)
    wrap = QHBoxLayout()
    wrap.addWidget(widget)
    lay.addLayout(wrap, 1)
    widget.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
    return host


def _set_combo(combo: QComboBox, value: Any) -> None:
    idx = combo.findData(value)
    if idx < 0 and isinstance(value, str):
        idx = combo.findData(value.lower())
    if idx < 0 and isinstance(value, int):
        idx = combo.findData(str(value))
    if idx >= 0:
        combo.setCurrentIndex(idx)
