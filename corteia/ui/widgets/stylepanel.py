"""``StylePanel`` — todos os campos de ``SubtitleSettings`` editáveis.

O painel **não** conhece o núcleo: ele emite ``settingsChanged`` com o objeto
``SubtitleSettings`` montado e a página (editor) decide o que fazer — chamar
``SUB.preset``/``SUB.build_ass`` e gravar em ``clip.edit``. Assim o widget pode
ser reutilizado em lote ("aplicar estilo em todos os cortes") sem acoplar ao
pipeline.

Campos: estilo, fonte, tamanho, cor, cor de destaque, posição, maiúsculas,
palavras por linha, contorno/sombra, caixa de fundo e queimar sim/não.
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QColorDialog, QComboBox, QFormLayout, QFrame,
                               QHBoxLayout, QLabel, QPushButton, QSpinBox,
                               QVBoxLayout, QWidget)

from .. import theme
from ...i18n import t
from ...models import SubtitleSettings, SubtitleStyle

#: rótulos dos estilos (o ``.label`` do enum já é PT, mantemos a lista ordenada)
STYLE_ORDER = [SubtitleStyle.VIRAL, SubtitleStyle.DYNAMIC, SubtitleStyle.PODCAST,
               SubtitleStyle.PROFESSIONAL, SubtitleStyle.MINIMALIST,
               SubtitleStyle.NONE]
POSITIONS = (("bottom", "Abaixo"), ("center", "Centro"), ("top", "Acima"))
FONTS = ("Montserrat", "Inter", "Arial Black", "Arial", "Roboto", "Bebas Neue",
         "Impact", "Verdana", "DejaVu Sans")


class ColorButton(QPushButton):
    """Botão que mostra a cor atual e abre o seletor nativo."""

    colorChanged = Signal(str)

    def __init__(self, color: str = "#FFFFFF", label: str = "",
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._color = color
        self.setText("")
        self.setFixedSize(74, 26)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip(t("Escolher cor") + (f" — {label}" if label else ""))
        self.clicked.connect(self._pick)
        self._repaint()

    def color(self) -> str:
        return self._color

    def setColor(self, value: str) -> None:
        self._color = value or "#FFFFFF"
        self._repaint()

    def _pick(self) -> None:
        chosen = QColorDialog.getColor(QColor(self._color), self, t("Escolher cor"))
        if chosen.isValid():
            self._color = chosen.name().upper()
            self._repaint()
            self.colorChanged.emit(self._color)

    def _repaint(self) -> None:
        col = QColor(self._color)
        fg = "#101018" if col.lightnessF() > 0.55 else "#F2F3F7"
        self.setStyleSheet(
            f"background:{self._color};color:{fg};border:1px solid "
            f"{theme.color('border_strong')};border-radius:6px;font-family:monospace;")
        self.setText(self._color)


class StylePanel(QWidget):
    """Formulário de legenda; emite o ``SubtitleSettings`` completo."""

    settingsChanged = Signal(object)          # SubtitleSettings
    styleRequested = Signal(str)              # valor do enum (preset externo)
    burnChanged = Signal(bool)

    def __init__(self, ctx: Any = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self._blocked = False                 # evita eco enquanto carregamos
        self._settings = SubtitleSettings()

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(8)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)
        form.setHorizontalSpacing(10)
        form.setVerticalSpacing(7)

        self.combo_style = QComboBox(self)
        for style in STYLE_ORDER:
            self.combo_style.addItem(style.label, style.value)
        self.combo_style.setToolTip(t("Cada estilo é um conjunto de padrões; você pode ajustar depois"))
        self.combo_style.currentIndexChanged.connect(self._on_style)
        form.addRow(t("Estilo"), self.combo_style)

        self.combo_font = QComboBox(self)
        self.combo_font.setEditable(True)
        for name in FONTS:
            self.combo_font.addItem(name)
        self.combo_font.setToolTip(t("Nome da fonte embutida no arquivo de legenda"))
        self.combo_font.currentTextChanged.connect(lambda v: self._commit(font=v))
        form.addRow(t("Fonte"), self.combo_font)

        self.spin_size = QSpinBox(self)
        self.spin_size.setRange(0, 200)
        self.spin_size.setSpecialValueText(t("Auto"))
        self.spin_size.setToolTip(t("0 calcula o tamanho a partir da altura do vídeo"))
        self.spin_size.valueChanged.connect(lambda v: self._commit(size=int(v)))
        form.addRow(t("Tamanho"), self.spin_size)

        self.color_main = ColorButton("#FFFFFF", t("Cor do texto"), self)
        self.color_main.colorChanged.connect(lambda v: self._commit(color=v))
        form.addRow(t("Cor do texto"), self.color_main)

        self.color_hi = ColorButton("#FFE400", t("Cor de destaque"), self)
        self.color_hi.colorChanged.connect(lambda v: self._commit(highlight_color=v))
        form.addRow(t("Cor de destaque"), self.color_hi)

        self.combo_position = QComboBox(self)
        for key, label in POSITIONS:
            self.combo_position.addItem(t(label), key)
        self.combo_position.currentIndexChanged.connect(
            lambda _i: self._commit(position=str(self.combo_position.currentData())))
        form.addRow(t("Posição"), self.combo_position)

        self.chk_upper = QPushButton(t("TODO MAIÚSCULAS"), self)
        self.chk_upper.setCheckable(True)
        self.chk_upper.setToolTip(t("Texto em maiúsculas, como nos cortes virais"))
        self.chk_upper.toggled.connect(lambda on: self._commit(uppercase=bool(on)))
        form.addRow(t("Maiúsculas"), self.chk_upper)

        self.spin_words = QSpinBox(self)
        self.spin_words.setRange(1, 12)
        self.spin_words.setValue(4)
        self.spin_words.setToolTip(t("Máximo de palavras por bloco de legenda"))
        self.spin_words.valueChanged.connect(lambda v: self._commit(max_words_per_line=int(v)))
        form.addRow(t("Palavras por linha"), self.spin_words)

        self.spin_outline = QSpinBox(self)
        self.spin_outline.setRange(0, 12)
        self.spin_outline.valueChanged.connect(lambda v: self._commit(outline=int(v)))
        form.addRow(t("Contorno"), self.spin_outline)

        self.combo_mode = QComboBox(self)
        for key, label in (("yellow", "Amarelo"), ("green", "Verde"),
                           ("multicolor", "Multicolor")):
            self.combo_mode.addItem(t(label), key)
        self.combo_mode.currentIndexChanged.connect(
            lambda _i: self._commit(highlight_mode=str(self.combo_mode.currentData())))
        form.addRow(t("Modo de destaque"), self.combo_mode)

        self.chk_bg = QPushButton(t("Caixa de fundo"), self)
        self.chk_bg.setCheckable(True)
        self.chk_bg.toggled.connect(self._on_bg)
        form.addRow(t("Fundo"), self.chk_bg)

        self.color_bg = ColorButton("#0E0F14", t("Cor da caixa"), self)
        self.color_bg.colorChanged.connect(self._on_bg_color)
        form.addRow(t("Cor da caixa"), self.color_bg)

        outer.addLayout(form)

        hint = QLabel(t("As mudanças valem para este corte; o preview ao lado usa exatamente estes valores."),
                      self)
        hint.setProperty("role", "faint")
        hint.setWordWrap(True)
        outer.addWidget(hint)

        # ------------------------------------------------- queimar legenda (export)
        burn_row = QFrame(self)
        burn_row.setProperty("panel", "surface2")
        braw = QHBoxLayout(burn_row)
        braw.setContentsMargins(10, 6, 10, 6)
        braw.addWidget(QLabel(t("Queimar legendas no arquivo final"), burn_row))
        self.btn_burn = QPushButton(t("Ligado"), burn_row)
        self.btn_burn.setCheckable(True)
        self.btn_burn.setChecked(True)
        self.btn_burn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_burn.setToolTip(t("Desligado: a legenda é salva como arquivo .ass separado"))
        self.btn_burn.toggled.connect(self._on_burn)
        braw.addWidget(self.btn_burn)
        braw.addStretch(1)
        outer.addWidget(burn_row)
        outer.addStretch(1)

    # ---------------------------------------------------------------------- API
    def set_settings(self, settings: SubtitleSettings | None, *, burn: bool | None = None) -> None:
        """Carrega valores no formulário sem disparar ``settingsChanged``."""
        if settings is None:
            return
        self._blocked = True
        self._settings = settings
        index = self.combo_style.findData(settings.style.value)
        self.combo_style.setCurrentIndex(max(0, index))
        if self.combo_font.findText(settings.font) < 0:
            self.combo_font.insertItem(0, settings.font)
        self.combo_font.setCurrentText(settings.font)
        self.spin_size.setValue(int(settings.size or 0))
        self.color_main.setColor(settings.color)
        self.color_hi.setColor(settings.highlight_color)
        pos = self.combo_position.findData(settings.position or "bottom")
        self.combo_position.setCurrentIndex(max(0, pos))
        self.chk_upper.setChecked(bool(settings.uppercase))
        self.spin_words.setValue(int(settings.max_words_per_line or 4))
        self.spin_outline.setValue(int(settings.outline or 0))
        mode = self.combo_mode.findData(settings.highlight_mode or "yellow")
        self.combo_mode.setCurrentIndex(max(0, mode))
        self.chk_bg.setChecked(bool(settings.background))
        if settings.background:
            self.color_bg.setColor(settings.background)
        if burn is not None:
            self.btn_burn.setChecked(bool(burn))
        self._sync_burn_text()
        self._blocked = False

    def settings(self) -> SubtitleSettings:
        """Cópia do estado atual do formulário."""
        base = self._settings
        return SubtitleSettings(
            style=base.style, font=self.combo_font.currentText().strip() or "Arial",
            size=int(self.spin_size.value()), color=self.color_main.color(),
            highlight_color=self.color_hi.color(), highlight_color2=base.highlight_color2,
            outline=int(self.spin_outline.value()), shadow=base.shadow,
            border_style=3 if self.chk_bg.isChecked() else 1,
            position=str(self.combo_position.currentData() or "bottom"),
            background=self.color_bg.color() if self.chk_bg.isChecked() else "",
            bg_opacity=base.bg_opacity,
            max_words_per_line=int(self.spin_words.value()),
            highlight_importance=base.highlight_importance,
            highlight_mode=str(self.combo_mode.currentData() or "yellow"),
            uppercase=self.chk_upper.isChecked(),
            reading_speed_cps=base.reading_speed_cps)

    def burn(self) -> bool:
        return self.btn_burn.isChecked()

    def set_burn(self, on: bool) -> None:
        self.btn_burn.blockSignals(True)
        self.btn_burn.setChecked(bool(on))
        self._sync_burn_text()
        self.btn_burn.blockSignals(False)

    # ------------------------------------------------------------------- eventos
    def _on_style(self, _index: int) -> None:
        value = str(self.combo_style.currentData() or SubtitleStyle.VIRAL.value)
        self._settings = SubtitleSettings(style=SubtitleStyle(value))
        if not self._blocked:
            self.styleRequested.emit(value)      # a página traz o preset real
            self.settingsChanged.emit(self.settings())

    def _commit(self, **changes: Any) -> None:
        """Atualiza o objeto em memória e reemite (usado pelos campos simples)."""
        for key, value in changes.items():
            setattr(self._settings, key, value)
        if not self._blocked:
            self.settingsChanged.emit(self.settings())

    def _on_bg(self, on: bool) -> None:
        self.color_bg.setEnabled(bool(on))
        self._commit(border_style=3 if on else 1,
                     background=self.color_bg.color() if on else "")

    def _on_bg_color(self, value: str) -> None:
        self._commit(background=value if self.chk_bg.isChecked() else "")

    def _on_burn(self, on: bool) -> None:
        self._sync_burn_text()
        self.burnChanged.emit(bool(on))

    def _sync_burn_text(self) -> None:
        self.btn_burn.setText(t("Ligado") if self.btn_burn.isChecked() else t("Desligado"))
        self.color_bg.setEnabled(self.chk_bg.isChecked())
