"""Painel: números reais do disco + projetos recentes + últimas exportações.

Nada aqui é simulado: todos os valores vêm de
``ctx.store.global_stats()``, ``ctx.store.recents()`` e
``ctx.store.export_history()``, que são relidos do filesystem a cada
:meth:`refresh`. Se um arquivo de vídeo existe, a miniatura vem de
``corteia.core.media.thumbnail`` — quando não existe, o card diz
"sem miniatura" em vez de inventar imagem.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QPushButton,
                               QScrollArea, QSizePolicy, QVBoxLayout, QWidget)

from .. import theme
from ..widgets.busy import run_job
from ..widgets.cards import EmptyState, HistoryRow, ProjectCard, StatCard
from ..widgets.progress import MiniBar
from ...i18n import t

log = logging.getLogger("corteia.ui.dashboard")


class DashboardPage(QWidget):
    """Primeira tela: estado do projeto atual + visão global do que já foi feito."""

    def __init__(self, ctx: Any, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self._cards: dict[str, StatCard] = {}
        self._recent_widgets: list[ProjectCard] = []
        self._history_widgets: list[HistoryRow] = []
        self._thumb_jobs_running = False

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        self.scroll = QScrollArea(self)
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        outer.addWidget(self.scroll)

        body = QWidget(self.scroll)
        body.setProperty("transparent", "true")
        self.scroll.setWidget(body)
        lay = QVBoxLayout(body)
        lay.setContentsMargins(24, 20, 24, 24)
        lay.setSpacing(18)

        # -------------------------------------------------------- cabeçalho
        head = QHBoxLayout()
        head.setSpacing(12)
        self.title = QLabel(t("Painel"), body)
        self.title.setProperty("role", "title")
        head.addWidget(self.title)
        head.addStretch(1)
        self.btn_import = _Button(t("Importar vídeo"), "import", "primary", body)
        self.btn_import.clicked.connect(lambda: ctx.go("import"))
        self.btn_export = _Button(t("Exportar"), "export", "outline", body)
        self.btn_export.clicked.connect(lambda: ctx.go("export"))
        self.btn_refresh = _Button(t("Atualizar"), "refresh", "ghost", body)
        self.btn_refresh.clicked.connect(self.refresh)
        head.addWidget(self.btn_import)
        head.addWidget(self.btn_export)
        head.addWidget(self.btn_refresh)
        lay.addLayout(head)

        # ------------------------------------------------- projeto em aberto
        self.current_card = QFrame(body)
        self.current_card.setProperty("card", "accent")
        cur = QHBoxLayout(self.current_card)
        cur.setContentsMargins(16, 12, 16, 12)
        cur.setSpacing(14)
        self.current_text = QLabel("", self.current_card)
        self.current_text.setProperty("role", "sub")
        self.current_text.setWordWrap(True)
        cur.addWidget(self.current_text, 1)
        self.current_bar = MiniBar(self.current_card)
        self.current_bar.setFixedWidth(150)
        self.current_bar.setVisible(False)
        cur.addWidget(self.current_bar, 0, Qt.AlignmentFlag.AlignVCenter)
        lay.addWidget(self.current_card)

        # ----------------------------------------------------------- números
        lay.addWidget(_section(t("Números reais deste computador"), body))
        grid = QHBoxLayout()
        grid.setSpacing(12)
        for key, icon in (("projects", "folder"), ("videos", "video"),
                          ("clips", "scissors"), ("exported", "export"),
                          ("source_seconds", "clock"), ("storage", "chart")):
            card = StatCard("", "0", icon, parent=body)
            card.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            self._cards[key] = card
            grid.addWidget(card)
        lay.addLayout(grid)

        # ---------------------------------------------------------- recentes
        self.recents_head = _section(t("Projetos recentes"), body)
        lay.addWidget(self.recents_head)
        self.recents_box = QVBoxLayout()
        self.recents_box.setSpacing(10)
        lay.addLayout(self.recents_box)

        self.empty_recents = EmptyState(
            t("Nenhum projeto ainda"),
            t("Importe um vídeo para começar. Assim que uma análise rodar, o projeto "
              "aparece aqui com a miniatura real do arquivo."),
            icon="video", action_text=t("Importar vídeo"), parent=body)
        self.empty_recents.set_action(t("Importar vídeo"), lambda: ctx.go("import"))
        lay.addWidget(self.empty_recents)

        # --------------------------------------------------------- exports
        self.history_head = _section(t("Últimas exportações"), body)
        lay.addWidget(self.history_head)
        self.history_box = QVBoxLayout()
        self.history_box.setSpacing(6)
        lay.addLayout(self.history_box)
        self.empty_history = EmptyState(
            t("Nada exportado ainda"),
            t("Os cortes aprovados aparecem aqui com tamanho, tempo gasto e o caminho real "
              "do arquivo depois da exportação."),
            icon="export", action_text=t("Ir para Exportar"), parent=body)
        self.empty_history.set_action(t("Ir para Exportar"), lambda: ctx.go("export"))
        lay.addWidget(self.empty_history)

        lay.addStretch(1)

        sig = ctx.signals
        sig.project_changed.connect(self.refresh)
        sig.recents_changed.connect(self._debounced_refresh)
        sig.history_changed.connect(self._debounced_refresh)
        sig.clips_changed.connect(self._debounced_refresh)
        sig.exports_finished.connect(self._debounced_refresh)

        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(160)
        self._debounce.timeout.connect(self.refresh)

    # ------------------------------------------------------------------- API
    def _debounced_refresh(self, *_args: Any) -> None:
        self._debounce.start()

    def refresh(self) -> None:
        try:
            stats = self.ctx.store.global_stats()
        except Exception as exc:  # noqa: BLE001 - o painel precisa abrir mesmo assim
            log.exception("global_stats falhou")
            self.current_text.setText(f"{t('Não foi possível ler os números')}: {exc}")
            return
        self._apply_stats(stats)
        self._apply_current_project()
        self._apply_recents()
        self._apply_history()

    # ---------------------------------------------------------------- números
    def _apply_stats(self, stats: dict) -> None:
        from ... import paths

        def n(key: str) -> int:
            try:
                return int(stats.get(key, 0) or 0)
            except (TypeError, ValueError):
                return 0

        seconds = float(stats.get("source_seconds", 0.0) or 0.0)
        temp_b = n("temp_bytes")
        data_b = n("data_bytes")
        free_b = n("disk_free_bytes")

        self._cards["projects"].set_value(
            n("projects"), t("projetos salvos"))
        self._cards["projects"].set_label(t("Projetos"))
        self._cards["videos"].set_value(
            n("videos"), t("vídeos já analisados"))
        self._cards["videos"].set_label(t("Vídeos importados"))
        self._cards["clips"].set_value(
            n("clips"), t("cortes sugeridos"))
        self._cards["clips"].set_label(t("Cortes"))
        self._cards["exported"].set_value(
            n("exported"), t("arquivos finais gerados"))
        self._cards["exported"].set_label(t("Exportados"))
        self._cards["source_seconds"].set_value(
            theme.format_duration_long(seconds), t("de material bruto"))
        self._cards["source_seconds"].set_label(t("Tempo de vídeo"))
        self._cards["storage"].set_value(
            paths.human_bytes(temp_b + data_b),
            t("temp {temp} · dados {data} · livre {free}").format(
                temp=paths.human_bytes(temp_b), data=paths.human_bytes(data_b),
                free=paths.human_bytes(free_b) if free_b else t("desconhecido")))
        self._cards["storage"].set_label(t("Espaço usado"))
        for card in self._cards.values():
            card.setToolTip(t("Contado agora, direto do disco."))

    # -------------------------------------------------------- projeto atual
    def _apply_current_project(self) -> None:
        proj = self.ctx.project
        st = proj.stats() if hasattr(proj, "stats") else {}
        busy = False
        try:
            busy = bool(self.ctx.is_busy())
        except Exception:  # noqa: BLE001
            busy = False
        missing = self.ctx.store.missing_sources(proj) if hasattr(self.ctx.store, "missing_sources") else []
        parts = [
            t("Projeto atual: {name}").format(name=proj.name or t("Sem título")),
            t("{v} vídeo(s), {c} corte(s), {e} exportado(s)").format(
                v=st.get("sources", 0), c=st.get("clips", 0), e=st.get("exported", 0)),
        ]
        if st.get("total_clip_seconds"):
            parts.append(t("{d} de cortes").format(d=theme.format_duration_long(st["total_clip_seconds"])))
        if missing:
            parts.append(t("{n} arquivo(s) de vídeo não encontrados no disco.").format(n=len(missing)))
        self.current_text.setText(" · ".join(parts))
        self.current_bar.setVisible(busy)
        if busy:
            self.current_bar.set_indeterminate(True)

    # ------------------------------------------------------------ recentes
    def _apply_recents(self) -> None:
        for w in self._recent_widgets:
            w.setParent(None)
            w.deleteLater()
        self._recent_widgets.clear()

        try:
            recents = self.ctx.store.recents()
        except Exception as exc:  # noqa: BLE001
            self.empty_recents.set_text(t("Não foi possível listar os recentes"), str(exc))
            self.empty_recents.setVisible(True)
            return

        self.empty_recents.setVisible(not recents)
        self.recents_head.setVisible(bool(recents))
        for recent in recents[:8]:
            card = ProjectCard(recent, self)
            card.openRequested.connect(self._open_project)
            card.showRequested.connect(lambda path: theme.open_location(Path(path).parent))
            self._recent_widgets.append(card)
            self.recents_box.addWidget(card)
            self._ensure_thumbnail(recent, card)

    def _ensure_thumbnail(self, recent: Any, card: ProjectCard) -> None:
        """Miniatura **real**: usa a do projeto ou gera do vídeo fora da GUI."""
        thumb = str(getattr(recent, "thumb", "") or "")
        if thumb and Path(thumb).is_file():
            card.apply_thumb(thumb)
            return
        project_path = str(getattr(recent, "path", "") or "")
        if not project_path or self._thumb_jobs_running:
            card.apply_thumb("")
            return
        self._thumb_jobs_running = True

        def work(progress) -> str:
            from ...core import media
            from ...models import Project

            try:
                data = Path(project_path).read_text(encoding="utf-8")
            except OSError:
                return ""
            import json

            try:
                proj = Project.from_dict(json.loads(data))
            except (ValueError, OSError):
                return ""
            for src in proj.sources:
                if src.thumb and Path(src.thumb).is_file():
                    return src.thumb
            for src in proj.sources:
                p = Path(src.path or "")
                if not p.is_file():
                    continue
                try:
                    progress(30.0, t("gerando miniatura de {name}").format(name=p.name))
                    return str(media.thumbnail(p, size=320))
                except Exception as exc:  # noqa: BLE001 - sem miniatura é resposta honesta
                    log.debug("thumbnail de %s falhou: %s", p, exc)
            return ""

        def done(result: Any) -> None:
            self._thumb_jobs_running = False
            if not card.is_alive():
                return
            if isinstance(result, str) and result:
                try:
                    recent.thumb = result
                except Exception:  # noqa: BLE001
                    pass
                card.apply_thumb(result)
            else:
                card.apply_thumb("")

        def error(_msg: str) -> None:
            self._thumb_jobs_running = False
            if card.is_alive():
                card.apply_thumb("")

        run_job(self.ctx, work, name=t("miniatura"), on_done=done, on_error=error, busy=False)

    def _open_project(self, path: str) -> None:
        p = Path(path)
        if not p.is_file():
            self.ctx.toast(t("O arquivo do projeto não existe mais: {path}").format(path=path), "error")
            return
        try:
            proj = self.ctx.load_project(p)
        except Exception as exc:  # noqa: BLE001
            self.ctx.toast(str(exc), "error")
            return
        if proj is None:
            self.ctx.toast(t("Não foi possível abrir esse projeto."), "error")
            return
        self.ctx.toast(t("Projeto “{name}” aberto.").format(name=proj.name), "ok")
        self.ctx.go("clips")

    # -------------------------------------------------------------- exports
    def _apply_history(self) -> None:
        for w in self._history_widgets:
            w.setParent(None)
            w.deleteLater()
        self._history_widgets.clear()

        rows: list[dict] = []
        try:
            rows = list(self.ctx.store.export_history(limit=8))
        except Exception as exc:  # noqa: BLE001
            log.debug("export_history falhou: %s", exc)

        # falhas e cancelamentos ficam no projeto: o painel global só lista os
        # concluídos, então acrescentamos as linhas ruins do projeto em aberto.
        for job in reversed(getattr(self.ctx.project, "exports", []) or []):
            state = str(getattr(job.state, "value", job.state) or "")
            if state == "done":
                continue
            rows.insert(0, {"project": self.ctx.project.name, "clip_id": job.clip_id,
                            "path": job.out_path, "finished_at": job.finished_at,
                            "seconds": max(0.0, job.finished_at - job.started_at),
                            "state": state, "error": job.error})

        self.empty_history.setVisible(not rows)
        self.history_head.setVisible(bool(rows))
        for row in rows:
            widget = HistoryRow(row, self)
            widget.openRequested.connect(self._reveal_export)
            self._history_widgets.append(widget)
            self.history_box.addWidget(widget)

    def _reveal_export(self, path: str) -> None:
        p = Path(path)
        if not p.exists():
            self.ctx.toast(t("Esse arquivo não existe mais: {path}").format(path=path), "warn")
            return
        ok, why = theme.open_location(p, reveal=True)
        if not ok:
            self.ctx.toast(why, "error")

    # ------------------------------------------------------------------ texto
    def retranslate(self) -> None:
        self.refresh()


def _section(text: str, parent: QWidget) -> QLabel:
    label = QLabel(text, parent)
    label.setProperty("role", "section")
    return label


class _Button(QPushButton):
    def __init__(self, text: str, icon_name: str, variant: str, parent: QWidget) -> None:
        super().__init__(text, parent)
        self._icon_name = icon_name
        self.setProperty("variant", variant)
        self.setProperty("size", "sm" if variant == "ghost" else "")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setIcon(theme.icon(icon_name, "muted", 18))
