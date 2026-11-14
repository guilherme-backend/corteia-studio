"""Janela principal do CorteIA Studio: sidebar + páginas + barra de status.

Responsabilidades (e nada além delas):

* montar a casca: :class:`Sidebar`, ``QStackedWidget``, topbar com o nome do
  projeto e o botão global **Cancelar**, ``QStatusBar`` com :class:`MiniBar`;
* traduzir os sinais de :class:`corteia.context.Signals` em estado de interface
  (navegação, progresso, avisos, ocupado/livre) — a janela nunca calcula
  número nem roda trabalho pesado;
* atalhos globais ``Ctrl+1``…``Ctrl+6``, ``Ctrl+O``/``Ctrl+S``/``Ctrl+I``/
  ``Ctrl+E``/``Ctrl+,`` e ``Esc`` para cancelar;
* receber arquivos soltos em qualquer página e entregá-los à página Importar.

Páginas em construção (outro agente escreve ``clips_page``/``editor_page``/
``export_page`` agora) não podem derrubar o app: o import é protegido e, se
falhar, mostra um card honesto "página em construção".
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtGui import QIcon, QKeySequence, QPixmap, QShortcut
from PySide6.QtWidgets import (QFileDialog, QFrame, QHBoxLayout, QLabel,
                               QMenu, QMainWindow, QPushButton, QSizePolicy, QLineEdit,
                               QStackedWidget, QStatusBar, QVBoxLayout, QWidget)

from .. import theme
from ..widgets.cards import EmptyState
from ..widgets.progress import MiniBar
from ..widgets.sidebar import NAV_ITEMS, Sidebar
from ..widgets.toast import ToastStack
from ...i18n import set_language, t
from ... import paths

log = logging.getLogger("corteia.ui")

#: id da página -> (atributo em ``corteia.ui.pages``, rótulo, atalho)
PAGE_IDS: tuple[str, ...] = ("dashboard", "import", "clips", "editor", "export", "settings")

_LABELS = {k: lbl for k, lbl, _i, _s in NAV_ITEMS}
_ACCELERATORS = {"dashboard": "Ctrl+1", "import": "Ctrl+2", "clips": "Ctrl+3",
                 "editor": "Ctrl+4", "export": "Ctrl+5", "settings": "Ctrl+6"}

_VIDEO_EXTS = {".mp4", ".m4v", ".mov", ".mkv", ".webm", ".avi", ".mpg", ".mpeg",
               ".mts", ".m2ts", ".ts", ".wmv", ".flv", ".3gp", ".ogv", ".vob", ".divx"}


class MainWindow(QMainWindow):
    """Casca navegável; o conhecimento do projeto fica todo em ``ctx``."""

    def __init__(self, ctx: Any) -> None:
        super().__init__()
        self.ctx = ctx
        self._pages: dict[str, QWidget] = {}
        self._placeholders: set[str] = set()
        self._current = "dashboard"
        self._closing = False

        self.setWindowTitle(t("CorteIA Studio"))
        self.setWindowIcon(self._app_icon())
        self.setMinimumSize(1120, 700)

        central = QWidget(self)
        central.setProperty("transparent", "true")
        self.setCentralWidget(central)
        root = QHBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self.sidebar = Sidebar(ctx, central)
        self.sidebar.navigate.connect(self.show_page)
        root.addWidget(self.sidebar)

        right = QWidget(central)
        right.setProperty("panel", "surface2")
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(0)
        rl.addWidget(self._build_topbar(right))
        root.addWidget(right, 1)

        self.stack = QStackedWidget(right)
        self.stack.setProperty("transparent", "true")
        rl.addWidget(self.stack, 1)
        self._build_pages()

        self.setStatusBar(self._build_statusbar(right))

        # ------------------------------------------------------------ overlays
        self.toasts = ToastStack(central, self)
        self.ctx.signals.toast.connect(self.toasts.on_signal)

        self._wire_signals()
        self._install_shortcuts()
        self._menu = QMenu(self)
        self._menu.addAction(t("Novo projeto"), self.new_project)
        self._menu.addAction(t("Abrir projeto…"), self.open_project_dialog)
        self._menu.addSeparator()
        self._menu.addAction(t("Salvar"), self.save_project)
        self._menu.aboutToShow.connect(
            lambda: _set_lang(self.ctx.config.str_("ui.language", "pt")))
        self.setAcceptDrops(True)
        self._restore_geometry()
        self.show_page("dashboard")
        self.refresh()

    # ------------------------------------------------------------------ ícone
    @staticmethod
    def _app_icon() -> QIcon:
        for cand in (paths.bundle_root().parent / "assets" / "icon.png",
                    paths.bundle_root() / "assets" / "icon.png",
                    Path("assets/icon.png")):
            if cand.is_file():
                pm = QPixmap(str(cand))
                if not pm.isNull():
                    return QIcon(pm)
        return QIcon(theme.pixmap("scissors", "accent", 32))

    # ----------------------------------------------------------------- topbar
    def _build_topbar(self, parent: QWidget) -> QFrame:
        bar = QFrame(parent)
        bar.setObjectName("topbar")
        bar.setFixedHeight(56)
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(16, 8, 14, 8)
        lay.setSpacing(10)

        self.page_title = QLabel(_LABELS["dashboard"], bar)
        self.page_title.setProperty("role", "title")
        lay.addWidget(self.page_title)

        lay.addSpacing(8)
        self.project_name = QLineEditProxy(bar, self.ctx)
        self.project_name.setObjectName("projectName")
        lay.addWidget(self.project_name, 1)

        self.state_pill = QLabel(t("pronto"), bar)
        self.state_pill.setProperty("pill", "idle")
        self.state_pill.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(self.state_pill, 0, Qt.AlignmentFlag.AlignVCenter)

        self.btn_cancel = QPushButton(t("Cancelar"), bar)
        self.btn_cancel.setProperty("variant", "danger")
        self.btn_cancel.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_cancel.setIcon(theme.icon("close", "error", 16))
        self.btn_cancel.setToolTip(t("Interrompe a análise ou a exportação em andamento (Esc)"))
        self.btn_cancel.clicked.connect(self.cancel_everything)
        lay.addWidget(self.btn_cancel, 0, Qt.AlignmentFlag.AlignVCenter)

        self.btn_settings = QPushButton("", bar)
        self.btn_settings.setProperty("variant", "ghost")
        self.btn_settings.setIcon(theme.icon("settings", "muted", 20))
        self.btn_settings.setToolTip(t("Configurações") + " · Ctrl+6")
        self.btn_settings.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_settings.clicked.connect(lambda: self.show_page("settings"))
        lay.addWidget(self.btn_settings, 0, Qt.AlignmentFlag.AlignVCenter)
        return bar

    # -------------------------------------------------------------- statusbar
    def _build_statusbar(self, parent: QWidget) -> QStatusBar:
        bar = QStatusBar(parent)
        bar.setSizeGripEnabled(False)
        self.status_label = QLabel(t("Pronto"), bar)
        bar.addWidget(self.status_label, 1)
        self.status_detail = QLabel("", bar)
        self.status_detail.setProperty("role", "mono")
        bar.addWidget(self.status_detail, 0)
        self.status_bar_widget = MiniBar(bar)
        self.status_bar_widget.setFixedWidth(180)
        self.status_bar_widget.setVisible(False)
        bar.addPermanentWidget(self.status_bar_widget)
        return bar

    # ----------------------------------------------------------------- páginas
    def _build_pages(self) -> None:
        """Importa cada página com fallback — o app abre mesmo faltando módulo."""
        for page_id in PAGE_IDS:
            widget = self._make_page(page_id)
            self._pages[page_id] = widget
            self.stack.addWidget(widget)

    def _make_page(self, page_id: str) -> QWidget:
        class_name = {
            "dashboard": "DashboardPage", "import": "ImportPage", "clips": "ClipsPage",
            "editor": "EditorPage", "export": "ExportPage", "settings": "SettingsPage",
        }[page_id]
        try:
            from corteia import pages as _pkg  # type: ignore[attr-defined]  # noqa: F401
        except Exception:  # noqa: BLE001
            pass
        try:
            from corteia.ui import pages as pages_pkg

            cls = getattr(pages_pkg, class_name)
            widget = cls(self.ctx)
            if not hasattr(widget, "refresh"):
                widget.refresh = lambda: None  # type: ignore[attr-defined]
            return widget
        except Exception as exc:  # noqa: BLE001 - página em construção
            log.warning("página %s indisponível: %s", page_id, exc)
            self._placeholders.add(page_id)
            return self._placeholder(page_id, exc)

    def _placeholder(self, page_id: str, exc: BaseException | None = None) -> QWidget:
        host = QWidget(self.stack)
        host.setProperty("transparent", "true")
        lay = QVBoxLayout(host)
        lay.setContentsMargins(28, 28, 28, 28)
        lay.addStretch(1)
        card = EmptyState(
            t("Página em construção"),
            t("A página “{page}” ainda não está disponível neste build; as demais seguem funcionando.")
            .format(page=_LABELS.get(page_id, page_id)),
            icon="warning",
            action_text=t("Voltar ao painel"),
            parent=host,
        )
        card.set_action(t("Voltar ao painel"), lambda: self.show_page("dashboard"))
        card.setMaximumWidth(560)
        lay.addWidget(card, 0, Qt.AlignmentFlag.AlignHCenter)
        detail = QLabel(f"{type(exc).__name__}: {exc}" if exc else "", host)
        detail.setProperty("role", "faint")
        detail.setAlignment(Qt.AlignmentFlag.AlignCenter)
        detail.setWordWrap(True)
        lay.addSpacing(8)
        lay.addWidget(detail)
        lay.addStretch(1)
        host.refresh = lambda: None  # type: ignore[attr-defined]
        return host

    def page_ids(self) -> list[str]:
        return [pid for pid in PAGE_IDS if pid in self._pages]

    def page(self, page_id: str) -> QWidget | None:
        return self._pages.get(page_id)

    def is_placeholder(self, page_id: str) -> bool:
        return page_id in self._placeholders

    # ----------------------------------------------------------------- sinais
    def _wire_signals(self) -> None:
        sig = self.ctx.signals
        sig.navigate.connect(self.show_page)
        sig.project_changed.connect(self._sync_project)
        sig.project_loaded.connect(lambda _p: self._sync_project())
        sig.busy_changed.connect(self._on_busy)
        sig.stage_started.connect(self._on_stage_started)
        sig.stage_progress.connect(self._on_stage_progress)
        sig.stage_finished.connect(self._on_stage_finished)
        sig.history_changed.connect(lambda: self.refresh())
        sig.clips_changed.connect(lambda: self._on_clips_changed())
        sig.recents_changed.connect(self._on_recents_changed)

        # config → tema/idioma aplicados na hora (SettingsPage só grava a chave)
        self.ctx.config.changed(self._on_config_changed)

    def _on_clips_changed(self) -> None:
        page = self._pages.get("clips")
        if page is not None and page is self.stack.currentWidget():
            _safe_refresh(page)

    def _on_recents_changed(self) -> None:
        if self._current == "dashboard":
            _safe_refresh(self._pages.get("dashboard"))

    def _on_config_changed(self, key: str, value: Any) -> None:
        if key in ("ui.theme", "ui.accent"):
            theme.apply(_app(), self.ctx.config.str_("ui.theme", "dark"),
                        self.ctx.config.str_("ui.accent"))
            self._retranslate()
        elif key == "ui.language":
            from ...i18n import set_language

            set_language(str(value))
            self._retranslate()

    def _retranslate(self) -> None:
        self.sidebar.retranslate()
        self.page_title.setText(t(_LABELS.get(self._current, self._current)))
        self.btn_cancel.setText(t("Cancelar"))
        self.state_pill.setText(t("pronto") if self.state_pill.property("pill") == "idle" else "")
        self.status_label.setText(t("Pronto"))
        self.project_name.retranslate()
        _safe_refresh(self._pages.get(self._current))
        for wid in self._pages.values():
            for child in wid.findChildren(EmptyState):
                child.retranslate()

    # --------------------------------------------------------------- navegação
    def show_page(self, page_id: str) -> None:
        widget = self._pages.get(page_id)
        if widget is None:
            log.debug("navegação para página desconhecida: %s", page_id)
            return
        self._current = page_id
        self.stack.setCurrentWidget(widget)
        self.sidebar.set_active(page_id)
        self.page_title.setText(t(_LABELS.get(page_id, page_id)))
        _safe_refresh(widget)

    def current_page_id(self) -> str:
        return self._current

    # ---------------------------------------------------------------- atalhos
    def _install_shortcuts(self) -> None:
        for page_id, seq in _ACCELERATORS.items():
            QShortcut(QKeySequence(seq), self,
                      activated=lambda _=False, p=page_id: self.show_page(p))
        QShortcut(QKeySequence("Ctrl+O"), self, activated=self.open_project_dialog)
        QShortcut(QKeySequence("Ctrl+S"), self, activated=self.save_project)
        QShortcut(QKeySequence("Ctrl+I"), self,
                  activated=lambda: (self.show_page("import"), self._focus_import()))
        QShortcut(QKeySequence("Ctrl+G"), self, activated=self.trigger_analyze)
        QShortcut(QKeySequence("Ctrl+E"), self, activated=lambda: self.show_page("export"))
        QShortcut(QKeySequence("Ctrl+,"), self, activated=lambda: self.show_page("settings"))
        QShortcut(QKeySequence(Qt.Key.Key_Escape), self, activated=self.cancel_everything)

    def _focus_import(self) -> None:
        page = self._pages.get("import")
        fn = getattr(page, "focus_input", None)
        if callable(fn):
            fn()

    def trigger_analyze(self) -> None:
        """``Ctrl+G``: roda no trabalho real da página Importar (sem duplicar núcleo)."""
        page = self._pages.get("import")
        if self.is_placeholder("import") or page is None:
            self.show_page("import")
            self.ctx.toast(t("A página de importação ainda não está disponível."), "warn")
            return
        fn = getattr(page, "analyze_current", None)
        if callable(fn):
            self.show_page("import")
            fn()
        else:
            self.show_page("import")
            self.ctx.toast(t("Nenhum vídeo selecionado para analisar."), "warn")

    # ----------------------------------------------------------- projeto/arquivo
    def _sync_project(self) -> None:
        self.project_name.sync()
        self._update_title()
        _safe_refresh(self._pages.get(self._current))

    def _update_title(self) -> None:
        name = (self.ctx.project.name or "").strip()
        self.setWindowTitle(f"{t('CorteIA Studio')} — {name}" if name else t("CorteIA Studio"))

    def save_project(self) -> None:
        try:
            self.ctx.save()
            self.ctx.toast(t("Projeto salvo."), "ok")
        except Exception as exc:  # noqa: BLE001
            self.ctx.toast(f"{t('Não foi possível salvar')}: {exc}", "error")

    def open_project_dialog(self) -> None:
        from ... import paths as _p

        start = _p.projects_dir()
        path, _sel = QFileDialog.getOpenFileName(
            self, t("Abrir projeto"), str(start),
            t("Projeto CorteIA (*.json) ;; Todos os arquivos (*)") or "*.json")
        if not path:
            return
        self.load_project_path(Path(path))

    def load_project_path(self, path: Path) -> bool:
        try:
            proj = self.ctx.load_project(path)
        except Exception as exc:  # noqa: BLE001 - ProjectStore levanta OSError explicado
            self.ctx.toast(str(exc), "error")
            return False
        if proj is None:
            self.ctx.toast(t("Esse arquivo não é um projeto do CorteIA."), "error")
            return False
        self.ctx.toast(t("Projeto “{name}” aberto.").format(name=proj.name), "ok")
        self._sync_project()
        return True

    def new_project(self) -> None:
        proj = self.ctx.new_project("")
        self._sync_project()
        self.ctx.toast(t("Novo projeto criado: {name}").format(name=proj.name), "info")

    # --------------------------------------------------------------- cancelamento
    def cancel_everything(self) -> None:
        """Fecha pipeline, exporter e fila de jobs — sempre com resposta na tela."""
        stopped = 0
        ctx = self.ctx
        for attr in ("pipeline", "exporter"):
            obj = getattr(ctx, f"_{attr}", None)      # não instancia o núcleo à toa
            if obj is None:
                continue
            try:
                obj.cancel()
                stopped += 1
            except Exception as exc:  # noqa: BLE001
                log.debug("cancel de %s falhou: %s", attr, exc)
        # dois runners diferentes podem existir (o do overlay e o do contrato);
        # cancelar os dois é o que faz o botão global funcionar de verdade.
        for factory in (lambda: __import__("corteia.ui.widgets.busy", fromlist=["get_runner"]).get_runner(ctx),
                        lambda: __import__("corteia.workers.jobs", fromlist=["runner_for"]).runner_for(ctx)):
            try:
                runner = factory()
                if runner is not None:
                    stopped += int(runner.cancel_all() or 0)
            except Exception as exc:  # noqa: BLE001
                log.debug("cancel_all do runner falhou: %s", exc)
        if stopped:
            self._set_status(t("Cancelando…"), "warn")
            self.ctx.toast(t("Cancelamento solicitado ({n} tarefa(s)).").format(n=stopped), "warn")
            QTimer.singleShot(1200, self._refresh_busy_status)
        else:
            self.ctx.toast(t("Nada em andamento para cancelar."), "info")

    def _refresh_busy_status(self) -> None:
        try:
            busy = bool(self.ctx.is_busy())
        except Exception:  # noqa: BLE001
            busy = False
        if not busy:
            self._on_busy(False, "")

    # ------------------------------------------------------------ estado ocupado
    def _on_busy(self, busy: bool, label: str = "") -> None:
        self.btn_cancel.setEnabled(bool(busy))
        self.state_pill.setProperty("pill", "busy" if busy else "idle")
        self.state_pill.setText(t("processando") if busy else t("pronto"))
        self._polish(self.state_pill)
        self.status_bar_widget.setVisible(bool(busy))
        if not busy:
            self.status_bar_widget.set_percent(0.0)
            self.status_label.setText(t("Pronto"))
        elif label:
            self.status_label.setText(label)

    def _on_stage_started(self, stage: str, label: str) -> None:
        self.status_label.setText(label or stage)
        self.status_bar_widget.setVisible(True)

    def _on_stage_progress(self, stage: str, label: str, percent: float, indeterminate: bool) -> None:
        self.status_bar_widget.set_indeterminate(bool(indeterminate))
        if not indeterminate:
            self.status_bar_widget.set_percent(percent)
        if label:
            self.status_label.setText(label)
        self.status_detail.setText(f"{stage} {percent:.0f}%" if not indeterminate else stage)

    def _on_stage_finished(self, stage: str, ok: bool, message: str) -> None:
        self.status_detail.setText("")
        if ok:
            self._set_status(message or t("Etapa concluída"), "ok")
        else:
            self.status_bar_widget.set_role("error")
            self._set_status(f"{t('Falha na etapa')}: {message or stage}", "error")
            QTimer.singleShot(4000, lambda: self.status_bar_widget.set_role(""))

    def _set_status(self, text: str, pill: str = "idle") -> None:
        self.status_label.setText(text)
        self.state_pill.setProperty("pill", pill)
        tokens = {"ok": t("concluído"), "warn": t("cancelando"), "error": t("erro")}
        self.state_pill.setText(tokens.get(pill, t("pronto")))
        self._polish(self.state_pill)

    @staticmethod
    def _polish(widget: QWidget) -> None:
        widget.style().unpolish(widget)
        widget.style().polish(widget)

    # ------------------------------------------------------------- drag & drop
    def dragEnterEvent(self, event) -> None:  # noqa: N802
        if self._dropped_paths(event.mimeData()):
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)

    def dropEvent(self, event) -> None:  # noqa: N802
        paths_list = self._dropped_paths(event.mimeData())
        url = ""
        if event.mimeData().hasUrls():
            for u in event.mimeData().urls():
                if u.scheme().startswith("http"):
                    url = u.toString()
        if not paths_list and not url:
            super().dropEvent(event)
            return
        event.acceptProposedAction()
        self.show_page("import")
        page = self._pages.get("import")
        if page is not None and not self.is_placeholder("import"):
            if url and not paths_list:
                fn = getattr(page, "handle_url", None)
                if callable(fn):
                    fn(url)
                    return
            fn = getattr(page, "handle_paths", None)
            if callable(fn):
                fn(paths_list)
                return
        self.ctx.toast(t("Abra a página Importar para soltar os arquivos."), "warn")

    @staticmethod
    def _dropped_paths(mime: Any) -> list[str]:
        out: list[str] = []
        if not mime.hasUrls():
            return out
        for url in mime.urls():
            if url.isLocalFile():
                local = Path(url.toLocalFile())
                if local.is_file() and (local.suffix.lower() in _VIDEO_EXTS or not local.suffix):
                    out.append(str(local))
        return out

    # ------------------------------------------------------------------ ciclo
    def refresh(self) -> None:
        self._sync_project()
        self._on_busy(bool(self.ctx.is_busy()) if hasattr(self.ctx, "is_busy") else False, "")

    def _restore_geometry(self) -> None:
        w = self.ctx.config.int_("ui.window_w", 1360)
        h = self.ctx.config.int_("ui.window_h", 820)
        if 700 <= w <= 8000 and 480 <= h <= 6000:
            self.resize(max(w, 1120), max(h, 700))
        else:
            self.resize(1360, 820)
        x = self.ctx.config.int_("ui.window_x", -1)
        y = self.ctx.config.int_("ui.window_y", -1)
        if x >= 0 and y >= 0:
            self.move(x, y)

    def closeEvent(self, event) -> None:  # noqa: N802
        if self._closing:
            return
        self._closing = True
        try:
            size = self.size()
            pos = self.pos()
            self.ctx.config.set("ui.window_w", int(size.width()))
            self.ctx.config.set("ui.window_h", int(size.height()))
            self.ctx.config.set("ui.window_x", int(pos.x()))
            self.ctx.config.set("ui.window_y", int(pos.y()))
        except Exception as exc:  # noqa: BLE001
            log.debug("geometria não salva: %s", exc)
        try:
            self.ctx.save()
        except Exception as exc:  # noqa: BLE001
            log.debug("salvamento final falhou: %s", exc)
        super().closeEvent(event)


class QLineEditProxy(QWidget):
    """Campo do nome do projeto com edição protegida (vazio não vale)."""

    def __init__(self, parent: QWidget | None, ctx: Any) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self.edit = QLineEdit(self)
        self.edit.setObjectName("projectName")
        self.edit.setPlaceholderText(t("Nome do projeto"))
        self.edit.setToolTip(t("Ctrl+S salva o projeto"))
        self.edit.textEdited.connect(self._edited)
        self.edit.editingFinished.connect(self._committed)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.edit)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.sync()

    def sync(self) -> None:
        self.edit.setText(self.ctx.project.name or "")

    def _edited(self, text: str) -> None:
        self.ctx.project.name = text

    def _committed(self) -> None:
        name = (self.edit.text() or "").strip()
        if not name:
            self.edit.setText(self.ctx.project.name or "")
            self.ctx.toast(t("O projeto precisa de um nome."), "warn")
            return
        self.ctx.project.name = name
        try:
            self.ctx.save()
        except Exception as exc:  # noqa: BLE001
            log.debug("rename save: %s", exc)
        self.window().setWindowTitle(f"{t('CorteIA Studio')} — {name}")

    def retranslate(self) -> None:
        self.edit.setPlaceholderText(t("Nome do projeto"))
        self.edit.setToolTip(t("Ctrl+S salva o projeto"))


def _set_lang(lang: str) -> None:
    set_language(lang)


def _app() -> Any:
    from PySide6.QtWidgets import QApplication

    return QApplication.instance()


def _safe_refresh(widget: QWidget | None) -> None:
    fn = getattr(widget, "refresh", None)
    if callable(fn):
        try:
            fn()
        except Exception as exc:  # noqa: BLE001 - uma página ruim não derruba a janela
            log.exception("refresh de %s falhou", type(widget).__name__)
            if _app() is not None:
                try:
                    toast = getattr(widget, "ctx", None)
                    if toast is not None:
                        toast.toast(f"{t('Erro ao atualizar a página')}: {exc}", "error")
                except Exception:  # noqa: BLE001
                    pass


# ``QUrl`` importado para leitura de mime em futuras extensões; mantém o nome vivo.
_ = QUrl
