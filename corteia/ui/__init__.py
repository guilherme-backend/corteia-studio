"""CorteIA Studio — camada de interface (PySide6).

Contém o tema (``theme``), os estilos QSS (``resources/qss``), os widgets
reutilizáveis (``widgets``), as páginas navegáveis (``pages``) e a janela
principal (``main_window``).

Regras desta camada:

* Toda interação pesada passa por ``corteia.workers.jobs.JobRunner`` — o thread
  de interface nunca executa FFmpeg, transcrição nem download.
* Nenhum widget conhece o núcleo: as páginas falam apenas com ``AppContext``
  (``ctx.pipeline`` / ``ctx.exporter`` / ``ctx.store`` / ``ctx.config``).
* Nenhum texto aparece pronto em inglês no código: tudo passa por ``t()`` de
  ``corteia.i18n``, cujo idioma base é o português do Brasil.
"""
