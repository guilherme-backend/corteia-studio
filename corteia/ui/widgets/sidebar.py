"""Barra lateral de navegação.

72 px de largura em modo compacto, 220 px expandida (o botão de rodapé
alterna). Os itens usam o gancho ``QPushButton[nav="true"]`` dos QSS e o
indicador ativo é a própria borda esquerda em ``@accent`` — nada de cor
solta no código.
"""

from __future__ import annotations

from typing import Iterable

from PySide6.QtCore import QSize, Qt, QTimer, Signal
from PySide6.QtWidgets import (QButtonGroup, QFrame, QHBoxLayout, QLabel,
                               QPushButton, QSizePolicy, QVBoxLayout, QWidget)

from .. import theme
from ...i18n import t

#: (id de navegação, rótulo em português, ícone, atalho mostrado)
NAV_ITEMS: tuple[tuple[str, str, str, str], ...] = (
    ("dashboard", "Painel", "home", "Ctrl+1"),
    ("import", "Importar", "import", "Ctrl+2"),
    ("clips", "Cortes", "scissors", "Ctrl+3"),
    ("editor", "Editor", "edit", "Ctrl+4"),
    ("export", "Exportar", "export", "Ctrl+5"),
    ("settings", "Configurações", "settings", "Ctrl+6"),
)

COLLAPSED_W = 72
EXPANDED_W = 220


class Sidebar(QFrame):
    """Navegação + marca; emite :attr:`navigate` com o id da página."""

    navigate = Signal(str)
    collapseToggled = Signal(bool)          # True = expandida

    def __init__(self, ctx=None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("sidebar")
        self.ctx = ctx
        self._expanded = False
        self._buttons: dict[str, QPushButton] = {}
        self.setFixedWidth(COLLAPSED_W)
        self.setMinimumHeight(320)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 12, 8, 10)
        lay.setSpacing(4)

        # ------------------------------------------------------------ marca
        brand = QHBoxLayout()
        brand.setSpacing(8)
        self.brand_icon = QLabel(self)
        self.brand_icon.setFixedSize(32, 32)
        self.brand_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        pm = theme.pixmap_from_file(_icon_path(), (28, 28), radius=6)
        if pm is not None:
            self.brand_icon.setPixmap(pm)
        else:                                        # ícone ausente: não mente
            self.brand_icon.setPixmap(theme.pixmap("scissors", "accent", 24))
        brand.addWidget(self.brand_icon, 0, Qt.AlignmentFlag.AlignVCenter)
        self.brand_name = QLabel(t("CorteIA Studio"), self)
        self.brand_name.setObjectName("brandName")
        self.brand_name.setVisible(False)
        self.brand_sub = QLabel(t("CORTE AUTOMÁTICO"), self)
        self.brand_sub.setObjectName("brandSub")
        self.brand_sub.setVisible(False)
        bcol = QVBoxLayout()
        bcol.setSpacing(0)
        bcol.addWidget(self.brand_name)
        bcol.addWidget(self.brand_sub)
        brand.addLayout(bcol, 1)
        lay.addLayout(brand)
        lay.addSpacing(10)

        # -------------------------------------------------------- navegação
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        for key, label, icon_name, shortcut in NAV_ITEMS:
            btn = QPushButton(self)
            btn.setProperty("nav", "true")
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setCheckable(True)
            btn.setIcon(theme.icon(icon_name, "muted", 20))
            btn.setIconSize(QSize(20, 20))
            btn.setToolTip(f"{t(label)} · {shortcut}")
            btn.setAccessibleName(t(label))
            btn.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            self._group.addButton(btn)
            self._buttons[key] = btn
            btn.clicked.connect(lambda _=False, k=key: self.navigate.emit(k))
            lay.addWidget(btn)
        lay.addStretch(1)

        sep = QFrame(self)
        sep.setObjectName("sidebarDivider")
        sep.setFixedHeight(1)
        lay.addWidget(sep)

        # ------------------------------------------------------ expandir
        self.btn_toggle = QPushButton(self)
        self.btn_toggle.setProperty("variant", "ghost")
        self.btn_toggle.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_toggle.setIcon(theme.icon("chevron_right", "muted", 18))
        self.btn_toggle.setToolTip(t("Expandir menu"))
        self.btn_toggle.setText("")
        self.btn_toggle.clicked.connect(self.toggle_expanded)
        lay.addWidget(self.btn_toggle)

        # width nao e propriedade gravavel no Qt: animamos em passos.
        self._anim_target = COLLAPSED_W
        self._anim_steps = QTimer(self)
        self._anim_steps.setInterval(16)
        self._anim_steps.timeout.connect(self._step_width)

    # ------------------------------------------------------------------- API
    def items(self) -> list[str]:
        return list(self._buttons)

    def set_active(self, page_id: str) -> None:
        for key, btn in self._buttons.items():
            active = key == page_id
            btn.setChecked(active)
            btn.setProperty("active", "true" if active else "false")
            btn.style().unpolish(btn)
            btn.style().polish(btn)
            btn.setIcon(theme.icon(self._icon_for(key),
                                   "accent" if active else "muted", 20))

    def set_items(self, items: Iterable[tuple[str, str, str, str]]) -> None:
        """Troca os rótulos (usado ao reconstruir a janela em outro idioma)."""
        for key, label, _icon, shortcut in items:
            btn = self._buttons.get(key)
            if btn is not None:
                btn.setToolTip(f"{t(label)} · {shortcut}")
                btn.setAccessibleName(t(label))
                btn.setText(t(label) if self._expanded else "")

    def _icon_for(self, key: str) -> str:
        for k, _label, icon_name, _sc in NAV_ITEMS:
            if k == key:
                return icon_name
        return "dot"

    def is_expanded(self) -> bool:
        return self._expanded

    def set_expanded(self, expanded: bool, animate: bool = True) -> None:
        self._expanded = bool(expanded)
        target = EXPANDED_W if self._expanded else COLLAPSED_W
        self.btn_toggle.setIcon(
            theme.icon("chevron_down" if not self._expanded else "chevron_up", "muted", 18))
        self.btn_toggle.setToolTip(t("Expandir menu") if not self._expanded else t("Recolher menu"))
        for btn in self._buttons.values():
            text = t(self._label_for_key(btn)) if self._expanded else ""
            btn.setText(text)
        self.brand_name.setVisible(self._expanded)
        self.brand_sub.setVisible(self._expanded)
        if animate and target != self.width():
            self._anim_target = target
            self._anim_steps.start()
        else:
            self.setFixedWidth(target)
        self.collapseToggled.emit(self._expanded)

    def _step_width(self) -> None:
        current = self.width()
        target = self._anim_target
        if current == target:
            self._anim_steps.stop()
            return
        step = 24 if target > current else -24
        nxt = target if abs(target - current) <= abs(step) else current + step
        self.setFixedWidth(nxt)

    def _label_for_key(self, btn: QPushButton) -> str:
        for key, label, _icon, _sc in NAV_ITEMS:
            if self._buttons.get(key) is btn:
                return label
        return ""

    def toggle_expanded(self) -> None:
        self.set_expanded(not self._expanded)

    def retranslate(self) -> None:
        """Renomeia tudo no idioma corrente (chamado quando o idioma muda)."""
        for key, btn in self._buttons.items():
            label = next((l for k, l, _i, _s in NAV_ITEMS if k == key), key)
            btn.setToolTip(f"{t(label)} · {next((s for k, _l, _i, s in NAV_ITEMS if k == key), '')}")
            btn.setAccessibleName(t(label))
            if self._expanded:
                btn.setText(t(label))
        self.brand_name.setText(t("CorteIA Studio"))
        self.brand_sub.setText(t("CORTE AUTOMÁTICO"))
        self.btn_toggle.setToolTip(t("Expandir menu") if not self._expanded else t("Recolher menu"))

    # ------------------------------------------------------------- eventos
    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)

    def sizeHint(self) -> QSize:
        return QSize(EXPANDED_W if self._expanded else COLLAPSED_W, 600)


def _icon_path() -> str:
    from ... import paths

    return str(paths.bundle_root().parent / "assets" / "icon.png")
