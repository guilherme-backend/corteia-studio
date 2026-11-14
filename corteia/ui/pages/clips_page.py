"""Página "Cortes": cartões com score explicado, filtros, seleção e reanálise.

O que este módulo faz de fato:

* desenha ``ctx.project.clips`` com ``widgets.cards.ClipCard`` (miniatura real,
  barra de score, aprovar/recusar/editar/exportar);
* traduz ``clip.score.signals`` para português no tooltip, com o peso de cada
  sinal — nenhum número fica mágico;
* filtra por score mínimo, ordena, permite seleção múltipla e aprovação em lote;
* roda "Reanalisar" em ``runner_for(ctx)`` (nunca na GUI), mostrando
  ``StageProgress`` alimentado por ``ctx.pipeline.stages()`` e botão Cancelar
  que chama ``ctx.pipeline.cancel()``.

Convenção de progresso: o núcleo devolve fração 0..1; aqui convertemos para
0..100 na fronteira da interface.
"""

from __future__ import annotations

import logging
from typing import Any

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (QComboBox, QFrame, QHBoxLayout, QLabel, QLineEdit,
                              QPushButton, QScrollArea, QSlider, QVBoxLayout, QWidget)

from .. import theme
from ...i18n import t
from ...models import Clip, ClipStatus, format_ts
from ..widgets.busy import get_runner
from ..widgets.cards import ClipCard, EmptyState
from ..widgets.progress import StageProgress
from ..widgets.segmented import SegmentedControl

log = logging.getLogger("corteia.ui.clips")

#: tradução dos sinais do ScoreBreakdown (o núcleo usa chaves em inglês)
SIGNAL_PT = {
    "hook": "gancho", "emotion": "emoção", "story_arc": "arco de história",
    "completeness": "fechamento", "debate": "potencial de discussão",
    "energy": "energia", "visual": "apelo visual", "density": "densidade de fala",
    "duration_fit": "ajuste de duração", "speakers": "falantes",
    "silence_penalty": "penalidade por silêncio", "gate": "nota do modelo de IA",
}
SORTS = (("score", "Maior score primeiro"), ("duration", "Mais longo primeiro"),
          ("start", "Ordem no vídeo"), ("speakers", "Mais falantes"))

#: ``pipeline.stages()`` devolve ``label="stage.probe"`` quando a chave de i18n
#: não existe; ``t()`` retorna a própria chave, então o texto PT mora aqui.
STAGE_PT = {"probe": "Lendo o arquivo", "thumb": "Miniatura",
            "transcribe": "Transcrevendo fala", "diarize": "Separando falantes",
            "silence": "Caçando silêncios", "scenes": "Detectando planos",
            "llm": "Avaliando com IA", "analyze": "Montando os cortes"}


class ClipsPage(QWidget):
    """Lista navegável dos cortes sugeridos pelo pipeline."""

    def __init__(self, ctx: Any, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self.selected: set[str] = set()
        self.current_id = ""
        self._cards: dict[str, ClipCard] = {}
        self._analysing = False

        root = QVBoxLayout(self)
        root.setContentsMargins(18, 14, 18, 12)
        root.setSpacing(10)

        # ---------------------------------------------------------------- cabeçalho
        head = QHBoxLayout()
        title = QLabel(t("Cortes sugeridos"), self)
        title.setProperty("role", "title")
        self.lbl_count = QLabel("", self)
        self.lbl_count.setProperty("role", "muted")
        head.addWidget(title)
        head.addSpacing(10)
        head.addWidget(self.lbl_count, 1)
        head.addWidget(QLabel(t("Ordenar por"), self))
        self.combo_sort = QComboBox(self)
        for key, label in SORTS:
            self.combo_sort.addItem(t(label), key)
        self.combo_sort.setToolTip(t("Muda apenas a exibição; a ordem gravada não muda"))
        head.addWidget(self.combo_sort)
        root.addLayout(head)

        # ------------------------------------------------------------------ filtros
        filters = QFrame(self)
        filters.setProperty("panel", "surface")
        flay = QHBoxLayout(filters)
        flay.setContentsMargins(12, 8, 12, 8)
        flay.setSpacing(10)
        flay.addWidget(QLabel(t("Score mínimo"), filters))
        self.slider_score = QSlider(Qt.Orientation.Horizontal, filters)
        self.slider_score.setRange(0, 90)
        # Este controle é apenas de VISUALIZAÇÃO. O piso de score do núcleo já
        # foi aplicado na análise; começar com ele aqui faria o único corte
        # "melhor disponível" (abaixo do mínimo) nascer invisível na lista.
        self.slider_score.setValue(0)
        self.slider_score.setToolTip(t("Esconde os cortes abaixo deste score (nada é apagado)"))
        self.slider_value = QLabel(f"{self.slider_score.value()}", filters)
        self.slider_value.setProperty("role", "mono")
        self.slider_value.setMinimumWidth(28)
        flay.addWidget(self.slider_score, 1)
        flay.addWidget(self.slider_value)
        flay.addSpacing(10)
        self.seg_status = SegmentedControl(
            [("all", "Todos"), ("suggested", "Sugeridos"), ("approved", "Aprovados"),
             ("rejected", "Recusados")], "all", filters, size="sm")
        flay.addWidget(self.seg_status)
        flay.addStretch(1)
        self.btn_select_all = QPushButton(t("Selecionar visíveis"), filters)
        self.btn_select_all.setProperty("variant", "ghost")
        self.btn_select_all.setProperty("size", "sm")
        flay.addWidget(self.btn_select_all)
        self.lbl_sel = QLabel("", filters)
        self.lbl_sel.setProperty("role", "muted")
        flay.addWidget(self.lbl_sel)
        root.addWidget(filters)

        # ------------------------------------------------------- título editável
        rename = QHBoxLayout()
        rename.addWidget(QLabel(t("Título do corte"), self))
        self.edit_title = QLineEdit(self)
        self.edit_title.setPlaceholderText(t("Selecione um corte para renomear"))
        self.edit_title.setEnabled(False)
        self.btn_apply_title = QPushButton(t("Aplicar título"), self)
        self.btn_apply_title.setProperty("variant", "outline")
        self.btn_apply_title.setEnabled(False)
        rename.addWidget(self.edit_title, 1)
        rename.addWidget(self.btn_apply_title)
        root.addLayout(rename)

        # ------------------------------------------------------------- ações
        actions = QHBoxLayout()
        self.btn_approve_sel = QPushButton(t("Aprovar seleção"), self)
        self.btn_approve_sel.setProperty("variant", "primary")
        self.btn_reject_sel = QPushButton(t("Rejeitar seleção"), self)
        self.btn_reject_sel.setProperty("variant", "danger")
        self.btn_editor = QPushButton(t("Abrir no editor"), self)
        self.btn_editor.setProperty("variant", "outline")
        self.btn_export_sel = QPushButton(t("Exportar seleção"), self)
        self.btn_export_sel.setProperty("variant", "outline")
        for btn in (self.btn_approve_sel, self.btn_reject_sel, self.btn_editor,
                    self.btn_export_sel):
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            actions.addWidget(btn)
        actions.addStretch(1)
        self.btn_reanalyse = QPushButton(t("Reanalisar"), self)
        self.btn_reanalyse.setProperty("variant", "primary")
        self.btn_reanalyse.setIcon(theme.icon("refresh", "on_accent", 16))
        self.btn_reanalyse.setToolTip(
            t("Recalcula os cortes deste vídeo; o que você já aprovou continua valendo"))
        self.btn_cancel = QPushButton(t("Cancelar"), self)
        self.btn_cancel.setProperty("variant", "danger")
        self.btn_cancel.setVisible(False)
        actions.addWidget(self.btn_reanalyse)
        actions.addWidget(self.btn_cancel)
        root.addLayout(actions)

        # ----------------------------------------------------- progresso da análise
        self.stage = StageProgress(ctx, self)
        self.stage.set_title(t("Etapas da análise"))
        self.stage.setVisible(False)
        root.addWidget(self.stage)

        # ---------------------------------------------------------------- lista
        self.scroll = QScrollArea(self)
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.list_host = QWidget(self.scroll)
        self.list_lay = QVBoxLayout(self.list_host)
        self.list_lay.setContentsMargins(0, 0, 8, 0)
        self.list_lay.setSpacing(10)
        self.empty = EmptyState(t("Nenhum corte ainda"),
                                t("Importe um vídeo e rode a análise para ver sugestões aqui."),
                                icon="magic", parent=self.list_host)
        self.empty.set_action(t("Ir para Importar"), lambda: self.ctx.go("import"))
        self.list_lay.addWidget(self.empty)
        self.scroll.setWidget(self.list_host)
        root.addWidget(self.scroll, 1)

        self.timer = QTimer(self)
        self.timer.setInterval(400)
        self.timer.timeout.connect(self._tick_stages)

        # ---------------------------------------------------------------- sinais
        self.slider_score.valueChanged.connect(self._on_filter)
        self.seg_status.changed.connect(lambda _k: self.refresh())
        self.combo_sort.currentIndexChanged.connect(lambda _i: self.refresh())
        self.btn_select_all.clicked.connect(self._select_visible)
        self.btn_apply_title.clicked.connect(self._apply_title)
        self.edit_title.returnPressed.connect(self._apply_title)
        self.btn_approve_sel.clicked.connect(lambda: self._bulk(ClipStatus.APPROVED))
        self.btn_reject_sel.clicked.connect(lambda: self._bulk(ClipStatus.REJECTED))
        self.btn_editor.clicked.connect(self._open_editor)
        self.btn_export_sel.clicked.connect(self._export_selection)
        self.btn_reanalyse.clicked.connect(self._reanalyse)
        self.btn_cancel.clicked.connect(self._cancel_analysis)
        ctx.signals.clips_changed.connect(self.refresh)
        ctx.signals.project_loaded.connect(lambda _p: self.refresh())

    # --------------------------------------------------------------------- filtros
    def _on_filter(self, value: int) -> None:
        self.slider_value.setText(str(value))
        self.refresh()

    def _visible_clips(self) -> list[Clip]:
        status = self.seg_status.current()
        minimum = float(self.slider_score.value())
        items = list(self.ctx.project.clips)
        if status != "all":
            items = [c for c in items if str(getattr(c.status, "value", c.status)) == status]
        items = [c for c in items if float(c.score.total or 0.0) >= minimum]
        mode = self.combo_sort.currentData() or "score"
        if mode == "score":
            items.sort(key=lambda c: -float(c.score.total or 0.0))
        elif mode == "duration":
            items.sort(key=lambda c: -c.duration)
        elif mode == "start":
            items.sort(key=lambda c: c.start)
        elif mode == "speakers":
            items.sort(key=lambda c: (-int(c.speaker_count or 0), -float(c.score.total or 0.0)))
        return items

    # ---------------------------------------------------------------------- render
    def refresh(self) -> None:
        clips = self._visible_clips()
        proj = self.ctx.project
        self.lbl_count.setText(t("{vis} de {tot} cortes · {ok} aprovados").format(
            vis=len(clips), tot=len(proj.clips), ok=len(proj.approved_clips)))
        seen: set[str] = set()
        for clip in clips:
            seen.add(clip.id)
            card = self._cards.get(clip.id)
            if card is None:
                card = self._make_card(clip)
                self._cards[clip.id] = card
                self.list_lay.insertWidget(self.list_lay.count() - 1, card)
            card.set_clip(clip, self._thumb_for(clip))
            card.setVisible(True)
            self._decorate(card, clip)
        for cid, card in self._cards.items():
            if cid not in seen:
                card.setVisible(False)
        self.empty.setVisible(not clips)
        self._update_sel_label()

    def _make_card(self, clip: Clip) -> ClipCard:
        card = ClipCard(clip, parent=self.list_host)
        card.approved.connect(lambda cid: self._set_status(cid, ClipStatus.APPROVED))
        card.rejected.connect(lambda cid: self._set_status(cid, ClipStatus.REJECTED))
        card.edited.connect(self._edit_one)
        card.exported.connect(self._export_one)
        card.selected.connect(self._toggle_selected)
        card.previewRequested.connect(self._edit_one)
        return card

    def _decorate(self, card: ClipCard, clip: Clip) -> None:
        """Tooltip com sinais traduzidos + contadores de falantes/planos."""
        tip = self.score_explanation(clip)
        card.score_bar.setToolTip(tip)
        card.score_value.setToolTip(tip)
        card.desc.setText(t("{sp} falante(s) · {sc} plano(s)").format(
            sp=int(clip.speaker_count or 0), sc=int(clip.scene_count or 0))
            + ("\n" + (clip.description or clip.reason or "")
               if (clip.description or clip.reason) else ""))
        card.desc.setToolTip(t("Por que a IA escolheu este trecho") + f"\n{clip.reason}")
        card.setToolTip(f"{clip.title}\n{format_ts(clip.start)} → {format_ts(clip.end)}"
                        f" · {clip.duration:.1f} s\n{tip}")
        if getattr(clip, "below_floor", False):
            # Nenhum trecho passou do piso: o cartão precisa dizer que isto é o
            # melhor que existe, e não uma sugestão que bateu a barra pedida.
            aviso = t("Abaixo do score mínimo — melhor disponível")
            card.desc.setText(aviso + "\n" + card.desc.text())
            card.setProperty("warn", "true")
            card.style().unpolish(card)
            card.style().polish(card)

    @staticmethod
    def score_explanation(clip: Clip) -> str:
        """Explicação legível: cada sinal traduzido, seu valor e peso."""
        signals = dict(clip.score.signals or {})
        weights = dict(clip.score.weights or {})
        lines = [t("Score {v}/100").format(v=f"{float(clip.score.total or 0.0):.0f}")]
        for key, value in sorted(signals.items(), key=lambda kv: -abs(kv[1] or 0.0)):
            label = SIGNAL_PT.get(key, key)
            weight = float(weights.get(key, 0.0) or 0.0)
            bar = "█" * max(0, min(10, int(round(abs(float(value)) * 10))))
            lines.append(f"· {label}: {float(value):.2f} ({t('peso')} {weight:.2f}) {bar}")
        if clip.score.llm_used:
            lines.append(t("Nota do modelo de IA: {r}").format(r=clip.score.llm_reason or "—"))
        if clip.reason:
            lines += ["", clip.reason]
        return "\n".join(lines)

    def _thumb_for(self, clip: Clip) -> str:
        """Miniatura real já produzida pelo pipeline (sem IO na thread da GUI)."""
        source = self.ctx.source_for(clip.source_path)
        thumb = str(getattr(source, "thumb", "") or "")
        return thumb or str(getattr(clip, "exported_path", "") or "")

    # -------------------------------------------------------------------- seleção
    def _toggle_selected(self, clip_id: str) -> None:
        if clip_id in self.selected:
            self.selected.discard(clip_id)
        else:
            self.selected.add(clip_id)
        self.current_id = clip_id
        self._sync_title_editor()
        self._update_sel_label()

    def _select_visible(self) -> None:
        ids = {c.id for c in self._visible_clips()}
        if ids and ids <= self.selected:
            self.selected -= ids
        else:
            self.selected |= ids
        if ids:
            self.current_id = sorted(ids)[0] if not self.current_id else self.current_id
            self._sync_title_editor()
        self._update_sel_label()

    def _update_sel_label(self) -> None:
        self.lbl_sel.setText(t("{n} selecionado(s)").format(n=len(self.selected))
                             if self.selected else t("nada selecionado"))
        for btn in (self.btn_approve_sel, self.btn_reject_sel, self.btn_export_sel):
            btn.setEnabled(bool(self.selected))
        self.btn_editor.setEnabled(bool(self.current_id))

    def _sync_title_editor(self) -> None:
        clip = self.ctx.clip(self.current_id) if self.current_id else None
        self.edit_title.setEnabled(clip is not None)
        self.btn_apply_title.setEnabled(clip is not None)
        if clip is not None:
            self.edit_title.setText(clip.title)

    def _apply_title(self) -> None:
        clip = self.ctx.clip(self.current_id) if self.current_id else None
        if clip is None:
            self.ctx.toast(t("Selecione um corte antes de renomear."), "warn")
            return
        text = self.edit_title.text().strip()
        if not text:
            self.ctx.toast(t("O título não pode ficar vazio."), "warn")
            return
        clip.title = text
        self.ctx.save()
        self.ctx.signals.clip_updated.emit(clip.id)
        self.refresh()
        self.ctx.toast(t("Título atualizado."), "ok")

    # --------------------------------------------------------------------- status
    def _set_status(self, clip_id: str, status: ClipStatus) -> None:
        clip = self.ctx.clip(clip_id)
        if clip is None:
            return
        clip.status = status
        self.ctx.save()
        self.ctx.signals.clips_changed.emit()
        self.ctx.toast(t("Corte marcado como {s}").format(
            s=t(str(status.value))), "ok" if status is ClipStatus.APPROVED else "info")

    def _bulk(self, status: ClipStatus) -> None:
        changed = 0
        for cid in list(self.selected):
            clip = self.ctx.clip(cid)
            if clip is not None:
                clip.status = status
                changed += 1
        if changed:
            self.ctx.save()
            self.ctx.signals.clips_changed.emit()
            self.ctx.toast(t("{n} corte(s) → {s}").format(n=changed, s=t(str(status.value))),
                           "ok")
        else:
            self.ctx.toast(t("Nenhum corte selecionado."), "warn")

    # ----------------------------------------------------------------- navegação
    def _open_editor(self) -> None:
        cid = self.current_id or next(iter(self.selected), "")
        if not cid:
            self.ctx.toast(t("Selecione um corte para editar."), "warn")
            return
        self.current_id = cid
        self.ctx.go("editor")

    def _edit_one(self, clip_id: str) -> None:
        self.current_id = clip_id
        self._sync_title_editor()
        self.ctx.go("editor")

    def _export_one(self, clip_id: str) -> None:
        clip = self.ctx.clip(clip_id)
        if clip is None:
            return
        clip.status = ClipStatus.APPROVED
        self.selected = {clip_id}
        self.ctx.save()
        self.ctx.go("export")

    def _export_selection(self) -> None:
        if not self.selected:
            self.ctx.toast(t("Selecione ao menos um corte."), "warn")
            return
        self.ctx.go("export")

    # ------------------------------------------------------------------ reanálise
    def _reanalyse(self) -> None:
        if self._analysing:
            return
        path = self.ctx.current_source_path()
        if not path:
            self.ctx.toast(t("Importe um vídeo primeiro."), "warn")
            return
        self._analysing = True
        self.btn_reanalyse.setEnabled(False)
        self.btn_reanalyse.setText(t("Analisando…"))
        self.btn_cancel.setVisible(True)
        self.btn_cancel.setEnabled(True)
        self.btn_cancel.setText(t("Cancelar"))
        self.stage.setVisible(True)
        self.stage.reset_clocks()
        self._push_stages()
        self.timer.start()
        opts = self.ctx.project.options

        def work(progress, cancel):        # roda no JobRunner, nunca na GUI
            return self.ctx.pipeline.analyze(
                source_path=path, options=opts,
                on_progress=lambda frac, label="": progress(
                    max(0.0, min(100.0, float(frac) * 100.0), ), str(label)),
                on_stage=None, cancel=cancel, force=False)

        get_runner(self.ctx).run(work, name=t("Reanálise"),
                                 on_done=self._analysis_done,
                                 on_error=self._analysis_failed)

    def _finish_analysis(self) -> None:
        self.timer.stop()
        self._analysing = False
        self.btn_reanalyse.setEnabled(True)
        self.btn_reanalyse.setText(t("Reanalisar"))
        self.btn_cancel.setVisible(False)
        self._push_stages()

    def _analysis_done(self, result: Any = None) -> None:
        self._finish_analysis()
        self.refresh()
        self.ctx.toast(t("{n} corte(s) recalculado(s).").format(n=len(result or [])), "ok")

    def _analysis_failed(self, text: str = "") -> None:
        self._finish_analysis()
        self.refresh()
        self.ctx.toast(t("A análise não concluiu: ") + str(text), "error")

    def _cancel_analysis(self) -> None:
        """Cancelamento cooperativo real: evento do job + ``pipeline.cancel()``."""
        self.btn_cancel.setEnabled(False)
        self.btn_cancel.setText(t("Cancelando…"))
        try:
            self.ctx.pipeline.cancel()
        except Exception:  # noqa: BLE001 - cancelar nunca pode estourar na GUI
            log.exception("pipeline.cancel falhou")
        runner = get_runner(self.ctx)
        try:
            runner.cancel(t("Reanálise"))
        except AttributeError:      # equivalente local antigo só tem cancel_all()
            runner.cancel_all()

    def _tick_stages(self) -> None:
        if self._analysing:
            self._push_stages()

    #: estado do núcleo -> texto honesto para o usuário
    STATE_PT = {"idle": "na fila", "running": "em execução", "done": "concluída",
                "error": "falhou", "cancelled": "cancelada"}

    def _push_stages(self) -> None:
        """Converte ``pipeline.stages()`` no formato que ``StageProgress`` lê.

        O núcleo devolve ``key/state/progress(0..1)/detail``; o widget espera
        ``id/done/percent(0..100)/note``. A conversão acontece aqui, na
        fronteira da UI, e estados de erro/cancelamento recebem texto próprio
        em vez de um check verde mentiroso.
        """
        try:
            raw = list(self.ctx.pipeline.stages())
        except Exception as exc:  # noqa: BLE001 - núcleo pode ainda não existir
            self.stage.set_message(t("Não foi possível ler as etapas") + f": {exc}")
            return
        rows = []
        for st in raw:
            state = str(st.get("state", "idle"))
            key = str(st.get("key") or "")
            label = str(st.get("label") or "")
            if not label or label.startswith("stage."):
                label = STAGE_PT.get(key, t("Etapa"))
            rows.append({"id": key, "label": label,
                         "done": state == "done",
                         "percent": float(st.get("progress") or 0.0) * 100.0,
                         "note": f"{self.STATE_PT.get(state, state)} · {st.get('detail') or ''}".strip(" ·")})
        self.stage.set_stages(rows)
        for row, st in zip(self.stage.rows(), raw):
            state = str(st.get("state", "idle"))
            if state in ("error", "cancelled"):
                row.set_percent_text("—" if state == "error" else t("parada"))
                row.show_note(f"{self.STATE_PT.get(state)} · {st.get('detail') or ''}".strip(" ·"))

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self.refresh()
