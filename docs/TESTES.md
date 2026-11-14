# Testes do CorteIA Studio

## Antes de qualquer coisa

```bash
cd corteia-studio
bash install.sh --dev          # instala requirements-dev.txt (pytest, pytest-qt, ruff, black)
. .venv/bin/activate           # Windows: .venv\Scripts\activate
```

No Windows, `install.bat --dev` faz o mesmo.

## Suíte unitária (pytest)

```bash
pytest -q                          # tudo
pytest -q -m "not slow"            # o padrão do dia a dia: sem render/ASR longos
pytest -q -m "not slow and not ui" # pula os widgets Qt
pytest -q -m ffmpeg                # só o que exige FFmpeg real no PATH
pytest -q -m network               # exige API de linguagem (dorme por padrão)
pytest -q tests/test_media.py -k probe
```

Os marcadores estão declarados em `pyproject.toml` (`[tool.pytest.ini_options]`,
linhas 99–108): `ffmpeg`, `network`, `slow`; `addopts = "-ra -q --strict-markers"`
então um marcador não registrado faz o pytest falhar, não passar escondido.
`testpaths = ["tests"]` e `python_files = ["test_*.py"]`.

Cobertura:

```bash
pytest -q -m "not slow" --cov=corteia --cov-report=term-missing
```

O `[tool.coverage.run]` já exclui `corteia/resources/*` e `tests/*`.

**Qt é sempre offscreen.** `tests/conftest.py` define
`QT_QPA_PLATFORM=offscreen` antes da primeira importação do Qt, para `pytest`
funcionar em CI e em SSH sem display. Se você rodar um teste de UI por conta
própria, exporte a mesma variável.

Um ponto honesto sobre o estado desta entrega: a pasta `tests/` estava sendo
escrita em paralelo quando este documento foi redigido e **não existia** no
disco no momento da escrita. Os comandos acima são o contrato (marcadores e
`testpaths` já registrados em `pyproject.toml`); rode `pytest -q` e confira o
número de itens coletados antes de confiar num "verde" silencioso.

## Smoke de produto, sem mocks

```bash
python run.py --check      # ambiente: FFmpeg, codecs, libass, fontes, modelos, caminhos
python run.py --smoke      # pipeline real ponta a ponta + exportação, sai 0/1
```

`--smoke` **não** usa mock: gera um vídeo de 22 s com `testsrc2` e fala
portuguesa de verdade sintetizada por `espeak-ng` (com silêncio entre 9 e 11 s
para exercitar a remoção), roda `Pipeline.analyze`, aprova os cortes, exporta
pelo `Exporter` e re-mede cada MP4 com `ffprobe`. Saída esperada (medida
neste ambiente): 2 cortes (score 62,8 e 17,1), vídeos finais de 9,00 s e 9,17 s
em 720×1280, 24 eventos de progresso, 0 falhas, todos os seis estágios `done`
em 100%. Ele deixa a pasta de trabalho em `/tmp/corteia-smoke-*` de propósito,
para você inspecionar o resultado.

`scripts/smoke.py` é a versão extendida do mesmo roteiro:

```bash
python scripts/smoke.py                      # importa tudo + ambiente + pipeline
python scripts/smoke.py --model tiny --keep  # modelo explicito, nao apaga nada
python scripts/smoke.py --quick              # so import e ambiente
python scripts/smoke.py --json               # relatorio final em JSON
```

Precisa de `/tmp/asr_test.mp4` (37,587 s, 640×360, h264+aac) para a etapa 3;
sem ele o resultado é declarado *parcial*, não sucesso — e as etapas 1 e 2
continuam obrigatórias.

## Scripts de verificação em `scripts/`

São testes de bancada, com asserções duras, escritos durante o desenvolvimento.
Rode-os depois de mexer no núcleo:

| script | cobre |
|---|---|
| `scripts/_smoke_media.py` | probe, miniatura, wav extraído, curva de energia (102 asserções) |
| `scripts/_smoke_vision.py` | YuNet, `analyze_clip_track`, `plan_crop`, limites de recorte (56) |
| `scripts/_smoke_asr.py` | transcrição, cache, diarização heurística, `words_for_range` |
| `scripts/_test_render.py` | grafo de filtros, blocos, concat, `-i` na ordem certa |
| `scripts/_test_analyze.py` | fronteiras, durações dentro do alvo, dedupe/IoU |
| `scripts/_test_subs.py` | ASS válido: 23 campos em `Style:`, cores, karaokê |
| `scripts/_test_player.py` | `VideoPlayer`/`TimelineMap` |
| `scripts/_smoke_ui_shell.py`, `_smoke_ui_clips.py` | casca da janela e página de cortes, offscreen |
| `scripts/_probe_core_ui.py` | contrato de assinaturas core ↔ UI |

```bash
for s in _smoke_media _smoke_vision _test_render _test_analyze _test_subs; do
    echo "--- $s"; python scripts/$s.py || break
done
```

Os vídeos e capturas que esses scripts geram ficam em `scripts/_media`,
`scripts/_shots` e `scripts/_shots_clips` — todos no `.gitignore`.

## Qualidade estática

```bash
ruff check corteia assets
black --check corteia
python -m py_compile run.py assets/make_icons.py
bash -n build_linux.sh install.sh run.sh
```

## O que ainda não é testável aqui

- **Windows:** instalador, `build_windows.bat`, `install.bat` e o `--check` do
  `.spec` nunca foram executados — não há Windows no ambiente de referência. A
  lista do que verificar numa máquina limpa está em `docs/WINDOWS.md`.
- **GPU real:** não há CUDA ativo; NVENC aparece listado no FFmpeg sem placa.
- **Rede:** download por URL (yt-dlp) foi validado com payloads sintéticos.
- **Composição transparente** do `CropOverlay` fora do recorte não é
  verificável em offscreen (sem RHI); precisa revalidar em desktop.

---

## Auditoria de 2026-11-14 (rodada de finalização)

O relatório completo, com as tabelas medidas, está em
[`AUDIT-2026-11-14.md`](../AUDIT-2026-11-14.md). Resumo do que foi executado:

| Bateria | Como rodar | Resultado |
|---|---|---|
| Suíte unitária | `python -m pytest -q tests/` | 63 passando (inclui 15 de segurança de dados e 11 de orçamento de memória) |
| Qualidade em vídeo real | `python scripts/quality_real.py --model base` | 3 vídeos CC, 27 cortes; 26 fecham ideia completa, IoU máx. 0,20, ρ 0,6–0,9 |
| Auditoria da interface | `QT_QPA_PLATFORM=offscreen python scripts/ui_audit.py` | 6 páginas, 1119 widgets, 29 botões, 13 atalhos, progresso 0→100 %, 0 exceções |
| Matriz de exportação | `python scripts/_matrix_export.py` | 21 casos; resoluções e durações conferidas por ffprobe |
| Pacote congelado | `pyinstaller corteia.spec` + rodar o binário | `--check` e `--smoke-ui` com exit 0 dentro do pacote |

Nada disso foi executado em Windows: veja §6 do relatório e `docs/WINDOWS.md`.
