"""Casca da interface offscreen: a janela constrói e as 6 páginas navigam."""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

PAGE_IDS = ("dashboard", "import", "clips", "editor", "export", "settings")


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def window(qapp, isolated_ctx):
    from corteia.ui.windows.main_window import MainWindow
    win = MainWindow(isolated_ctx)
    yield win
    win.close()


@pytest.mark.ui
def test_main_window_constroi_com_paginas_reais(window):
    from corteia.ui.windows.main_window import PAGE_IDS as real_ids
    assert tuple(real_ids) == PAGE_IDS
    assert window.page_ids() == list(PAGE_IDS), "faltam páginas registradas"
    assert window.current_page_id() == "dashboard"
    assert window.stack.count() == len(PAGE_IDS)
    assert not window._placeholders, f"páginas em construção: {sorted(window._placeholders)}"
    # casca completa: sidebar + topbar + statusbar + todas as páginas montadas
    assert window.sidebar is not None and window.statusBar() is not None
    assert len(window.findChildren(object)) > 50, "árvore de widgets suspeitamente vazia"
    for pid in PAGE_IDS:
        w = window.page(pid)
        assert w is not None
        assert callable(getattr(w, "refresh", None)), f"{pid} sem refresh() obrigatório"


@pytest.mark.ui
def test_ctx_go_navega_pelas_seis_paginas_sem_lancar_excecao(qapp, window):
    seen = []
    window.stack.currentChanged.connect(lambda i: seen.append(i))
    for pid in PAGE_IDS:
        window.ctx.go(pid)          # o mesmo caminho que a sidebar e os atalhos usam
        qapp.processEvents()
        assert window.current_page_id() == pid
        assert window.stack.widget(window.stack.currentIndex()) is window.page(pid)
        assert window.stack.currentWidget() is window.page(pid)
    assert len(seen) >= len(PAGE_IDS) - 1, f"trocas de página não aconteceram: {seen}"
    assert window.page_title.text(), "o título da página ficou vazio"
    # ir para uma página que não existe é ignorado, não derruba a janela
    window.ctx.go("pagina_inexistente")
    qapp.processEvents()
    assert window.current_page_id() == PAGE_IDS[-1]
