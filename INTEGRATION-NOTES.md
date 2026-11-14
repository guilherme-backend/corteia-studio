# Notas de integração (verificadas por execução, não por suposição)

Atualizado em 2026-11-14. Estas são as convenções reais do núcleo, provadas por
testes rodados neste computador. Quem divergir delas quebra o app.

## Progresso

- **Todo o core reporta fração 0.0–1.0.** `ffmpeg_cli.run/FFmpegJob`,
  `silence.detect_silences`, `scenes.detect_scenes`, `render.render_clip`.
- `transcribe.transcribe` também emite 0–100 em alguns callbacks; por isso
  `pipeline._frac()` normaliza (valor > 1,5 é tratado como porcentagem).
- A UI converte para 0–100 ao pintar (`StageProgress`, `MiniBar`).
- `pipeline.Pipeline._emit` **não** rebaixa um estágio já `done` para `running`
  (bug real corrigido: o stepper ficava preso em 58%).

## Números medidos neste sandbox

| coisa | valor real |
|---|---|
| Pipeline completo em vídeo de 37,6 s (transcrição em cache, tiny) | 1,8 s |
| Export de 3 cortes 720×1280 (12,9 s + 10,9 s + 7,2 s de saída) | 4,6 s |
| RTF Whisper tiny / base (áudio → parede) | 0,063× / 0,082× |
| `analyze_clip_track` 1080p, 6 amostras/s, 60 s de vídeo | 10,2 s (28 ms/amostra) |
| YuNet 1080p (limite de 960 px) | 11,1 ms/quadro |
| Diarização por pitch, 8 trechos de 2 vozes | 8/8 corretos, 0,97 s |
| Cache de transcrição (2ª chamada) | 0,05 s vs 3,06 s (59×) |
| Silêncio removido no e2e | 8 pausas, 6 s de 37,6 s |
| Corte 0–15,4 s exportado | 12,87 s (bate com o TimelineMap) |

## Contratos que os outros módulos consomem

- `reframe.plan_crop(points, info, aspect, t0, t1, *, follow_speaker, zoom,
  offset_x, offset_y, manual) -> CropPlan`
  `CropPlan.points = [(t, x, y, w, h)]` em **píxeis da fonte**, `w/h`
  **constantes** em todo o plano (só `x,y` variam) — exigência do `concat`.
  `reframe.segments_of(plan)` é a versão contígua; `render.crop_segments` lê
  `.points` diretamente.
- `render.render_clip(clip, *, source, info, transcript, settings, plan, tmap,
  out, progress=, cancel=, threads=, proxy=, ass_path=, fit_mode=)`
- `render.output_size(settings, aspect)`: 1080p 9:16 = **1080×1920** (1080p
  nomeia o lado curto). Bug real corrigido: dava 608×1080.
- `subtitles.build_ass(transcript, t0, t1, settings, dest, *, clip_w, clip_h,
  edits=None, tmap=None, speaker_names=None)`; `SUB.preset(style)`,
  `SUB.STYLE_PRESETS`.
- `silence.cut_points(...)` devolve **faixas de fala**, não silêncios.
- `media.probe` devolve dimensões de **exibição** (rotação aplicada).
- NVENC ≠ CUDA: `hardware.cuda_available` só é verdadeiro com `nvidia-smi`
  respondendo **e** runtime CUDA funcional; senão aparece a nota honesta.
  (Bug real: NVENC no FFmpeg fazia o ASR pedir cuda e crashar.)
- Fontes: `fonts.pick(nome) -> (usado, substituiu)` e
  `fonts.file_for(nome, bold=)` varre `fc-list : file family` e **ranqueia
  faces** (nunca itálico; negrito quando pedido). Sem isso o `drawtext` saía
  em itálico/extra-light.

## Defeitos reais encontrados e corrigidos nesta rodada

1. Linha `Style:` do ASS tinha cores como `&H...&` (formato de override tag) e
   25 campos em vez de 23 → libass não desenhava nada. Agora `assert` nos 23
   campos e cores sem `&` final dentro de Style.
2. Karaokê `\kf` tingia tudo de amarelo no estilo *viral*; restrito a
   podcast/dinâmico, viral usa cor estática por palavra.
3. Similaridade léxica **não** separa tópicos (intra 0,042 vs inter 0,044) →
   sinal removido; fronteiras agora vêm de pausa/falante/cenas/API.
4. Normalização dava percentis diferentes a valores idênticos (empates) →
   "troca de falantes 95%" quando todos tinham 2 falantes. Agora tie-aware.
5. Portão absoluto de engajamento/novidade: trecho técnico sem conteúdo não
   vence mais por variância de áudio.
6. `plan_blocks` forçava contiguidade e **reintroduzia o silêncio removido**.
7. Faltava `-i` antes da origem e os rótulos de pad sem colchetes → FFmpeg
   "Error parsing global options".
8. `stamp_xy` usava expressões de `overlay` dentro de `drawtext` (`w` = largura
   do vídeo) → marca d'água fora da tela. Agora `text=True` usa `text_w/text_h`.
9. `Recent.to_dict` usava `self.__dict__` em dataclass `slots=True` → TypeError
   ao salvar projeto. Agora `asdict`.
10. `Config.bool_(k, default)` não aceitava default → export quebrava. Os
    getters tipados agora aceitam default opcional.
11. `clip.edit` não tem `follow_speaker` (quem decide é `AnalysisOptions`).
12. CUDA falso-positivo por NVENC (ver acima).

## O que ainda não existe / não foi validado

- Build Windows real (`.bat`, `.ps1`, Inno Setup, PyInstaller) nunca executado:
  não há Windows aqui.
- Composição transparente do `CropOverlay` fora do recorte não é verificável
  em offscreen (sem RHI) — precisa revalidar em desktop.
- Rotação de celular real: o FFmpeg local transpõe o pixel, então só o parse de
  `side_data_list` foi validado com JSON real de ffprobe.
- Download por URL validado com payloads sintéticos do yt-dlp, sem rede.

## Estado verificado nesta rodada (CLI + núcleo fechado)

- `python run.py --version` / `--check` / `--smoke` / `--analyze V` / `--export V 1 8` **todos
  rodando de verdade** neste sandbox. `--check` saiu 0 com: FFmpeg 6.1.1 em `/usr/bin/ffmpeg`,
  libx264 ok, libx265 ok, **aac ok**, libass ok, NVENC/Qsv listados, Whisper tiny+base
  instalados, 1629 fontes, caminhos de config/cache/temp/projetos.
- `--smoke`: gera 22 s de vídeo com **fala PT-BR real (espeak-ng)** + silêncio 9–11 s, roda o
  pipeline inteiro e exporta. Saída medida: 2 cortes (score 62,8 e 17,1), 9,00 s e 9,17 s de
  vídeo final em 720×1280, 24 eventos de progresso, 0 falhas. Todos os seis estágios terminam
  `done` 100%.
- `--analyze` escreve `<video>.cortes.json` com start/end/title/description/score/reason/keywords.
- `--export VIDEO INÍCIO SEGUNDOS` grava em `<HOME>/Videos/CorteIA/01-Corte 1s-corteia.mp4`.
- Regressão após minhas mudanças no núcleo: `_smoke_media` **102/102**, `_smoke_vision`
  **56/56**, `_test_render` "TODAS AS ASSERÇÕES PASSARAM", `_test_analyze` durações dentro do
  intervalo, `_test_subs` ASS válido.
- Defeito meu corrigido: o parser de `ffmpeg -encoders` usava `line.split()[0].startswith(" A")`,
  mas `split()` come o espaço inicial — por isso `aac` aparecia "AUSENTE". Agora compara o
  campo de flags de 6 caracteres.
- `corteia/__init__.py`, `corteia/main.py`, `run.py`, `corteia/ui/app.py` existem. `app.py`
  precisa de `corteia/ui/windows/main_window.py` (`MainWindow(ctx)`) e usa, em smoke,
  `win.page_ids()` se existir.

## Lacunas conhecidas no momento deste registro

UI: faltavam `main_window.py`, `sidebar.py`, `droparea.py`, `stylepanel.py`,
`subtitle_overlay.py` e as seis páginas (`dashboard`, `import_page`, `settings_page`,
`clips_page`, `editor_page`, `export_page`). Já existiam: `theme.py`, `cards.py` (552),
`timeline.py` (868), `player.py` (851), `busy.py` (509), `progress.py`, `toast.py`,
`switch.py`, `segmented.py`, `workers/jobs.py`.
Docs/pacote: faltavam `README.md`, `LICENSE.txt`, `CHANGELOG.md`, `.gitignore`, `docs/*`,
`corteia.spec`, `installer/corteia.iss`, `build_windows.bat`, `build_linux.sh`.
Testes: `pytest` ainda sem suíte em `tests/`.

## Correções que os agentes NÃO devem desfazer

1. `render.py`: `-i` antes da origem; rótulos de pad sempre entre colchetes; numeração de
   inputs por ordem de aparição (música/mark d'água); `stamp_xy(text=True)` usa `text_w/text_h`.
2. `subtitles.py`: bloco `Style` com exatamente 23 campos, cores `&HBBGGRR&` só dentro de
   overrides (`\c&...&`), karaokê restrito a podcast/dinâmico.
3. `fonts.py`: ranqueamento de faces (nunca Italic; Bold quando pedido) via índice real do
   `fc-list`.
4. `config.py`: getters tipados aceitam `default` opcional.
5. `project.py`: serialização com `asdict` (dataclasses têm `slots=True`).
6. `pipeline.py`: `_emit` não rebaixa estágio `done`; progresso do core em fração.
7. `analyze.py`: portão absoluto de engajamento; normalização aware de empates; sem sinal de
   "troca de tópico" por similaridade léxica.
8. `hardware.py`: NVENC no FFmpeg **não** implica CUDA; precisa de `nvidia-smi` + runtime.


## Rodada de integração da interface (verificado por execução)

Estado: MainWindow 1360x820 com 6 páginas reais; `python run.py --smoke` (UI) retorna 0 com
993 widgets construídos; cada página constrói sozinha (Dashboard 57, Import 117, Clips 50,
Editor 191, Export 119, Settings 366 widgets).

Defeitos reais encontrados e corrigidos nesta rodada:
1. `ClipsPage`/`ExportPage` passavam o widget pai na posição de `action_text` de
   `EmptyState` → `setText(QWidget)` derrubava a página inteira. Corrigido nos dois
   chamados e `EmptyState` ficou tolerante (um estado vazio nunca pode fechar uma página).
2. `ExportPage` conectava `exports_finished` a um método `_on_finished` que não existia.
   Implementado (relança fila/histórico, sem toast duplicado).
3. `ImportPage` fazia o probe mas nunca gravava `VideoInfo` na fonte do projeto: Painel
   mostrava "--" mesmo com o vídeo lido. Agora chama `ctx.add_source(path, info)`.
4. `ProgressReporter` despejava traceback ("falha ao entregar progresso", "Signal source
   has been deleted") quando a página morria antes do job. `FunctionJob._safe_emit` cobre
   agora finished/failed/cancelled/started e o progresso loga em debug.
5. Pipeline consultava chaves de configuração INVENTADAS (`asr.model`, `silence.db`,
   `scenes.threshold`, `performance.cpu_threads`, `silence.min_keep`), então nada que o
   usuário mudasse em Configurações tinha efeito. Chaves reais: `subs.model`,
   `subs.language`, `subs.beam_size`, `subs.word_timestamps`, `subs.diarize`,
   `proc.device`, `proc.compute_type`, `proc.cpu_threads`, `proc.silence_db`,
   `proc.silence_min_ms`, `proc.scene_threshold`, `proc.sample_fps_face`,
   `analysis.*`, `api.*`, `export.*`, `ui.*`, `net.download_consent`.
6. Modelos Whisper ficavam dentro do diretório de configuração (~/.config). Movidos para
   o cache com detecção da instalação antiga (nada é baixado duas vezes).
7. `crop_plan_for` referenciava `cfg` que não existia naquele escopo (NameError ao
   exportar com "seguir falante" ligado).
8. Resto de rascunho no pipeline (`opts.follow_speaker = clip.edit.follow_speaker`, bloco
   `if ... is not None: pass`) removido.

Fatos do produto: `ExportSettings.burn_subtitles` é a escolha por projeto e manda; o
Editor também grava `export.burn_subtitles` na configuração global. O combo de modelo da
importação traz tiny/base/small/medium/large-v3/distil-large-v3/auto com o tamanho em MB e
descrição no tooltip; `auto` escolhe por RAM (16 GB→small, 24 GB→medium) e desce uma faixa
para áudio >60 min.

## Fechamento (rodada seguinte, tudo por execução)

- **Trava do YuNet**: `cv2.FaceDetectorYN` é uma rede única e mutável; sem `with _lock`
  em `setInputSize`+`detect`, duas exportações paralelas (padrão `proc.max_parallel_jobs=2`)
  corrompiam as shapes e o OpenCV lançava `buf.shape() == m.shape()`. Na prática: 72 avisos
  de "detecção facial falhou" num fluxo pela UI e o recorte caía para saliência. Depois da
  trava: 4 threads simultâneas, 0 falhas, plano `mode=face` com 9 keyframes e 2 trilhas.
- **`min_score` era decorativo**: `select()` tinha `pool = [acima do piso] or [melhor]`,
  então um corte 5,9 era entregue com piso 70 sem aviso. Agora o piso filtra de verdade e,
  quando nada passa, o corte único vem com `Clip.below_floor=True`, o texto do motivo ganha
  "abaixo do mínimo de N (melhor disponível)" e o cartão mostra o aviso.
- **Filtro de score da página de cortes** começava no valor de `analysis.min_score`, o que
  escondia exatamente o corte "melhor disponível". Passou a começar em 0: é filtro de
  visualização, não o piso do núcleo.
- **`python run.py --smoke-ui`** existe: abre a janela real, constrói as 6 páginas e fecha
  (993 widgets, exit 0). Usa `QT_QPA_PLATFORM=offscreen` quando a variável não está definida,
  porque `DISPLAY` existir não garante que o plugin `xcb` consiga abrir (neste container não
  há `libxcb-cursor`, e o Qt aborta o processo).
- **Prova final de produto** com vídeo sintético que tem rostos E fala (`/tmp/rosto_fala.mp4`,
  12 s, 1280x720): análise em 2,2 s, 1 corte, exportação 1080×1920 h264 de 10,30 s (12 s
  menos 1,7 s de pausas), legenda queimada com destaque palavra a palavra confirmada em
  quadros (`/tmp/prova_final.png`).
- **i18n**: `en.json` tinha 361 entradas e 207 strings do código sem tradução (a UI caía em
  português no idioma inglês). Tradução completa entregue por equipe separada.
- Regressão verde nesta rodada: `_smoke_media` 102/102, `_smoke_vision` 56/56,
  `_test_render` todas as asserções, `_test_analyze` durações ok, `run.py --check` exit 0,
  `run.py --smoke` 2 exportados 0 falhas, fluxo da UI completo exit 0.
