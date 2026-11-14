# Arquitetura do CorteIA Studio

Documento de referência para quem vai mexer no código. Assinaturas completas e
verificadas estão em `docs/API.md`. Tudo aqui foi lido do código de `corteia/`
em 2026-11-14, não de um plano.

## Camadas

```
run.py                      lançador: valida Python/PySide6 e chama corteia.main
corteia/main.py             CLI (--check/--smoke/--analyze/--export) + gui()
corteia/__init__.py         nome, versão, main()
├── corteia/core/           8.841 linhas  — processamento puro, SEM PySide6
├── corteia/workers/          594 linhas  — ponte para threads (JobRunner)
├── corteia/ui/            11.765 linhas  — tema, widgets, páginas, janela
└── contexto e utilitários
    corteia/context.py      AppContext: config + store + pipeline + exporter + sinais
    corteia/config.py       JSON persistente, getters tipados (str_/int_/float_/bool_)
    corteia/models.py       dataclasses `slots=True` + enums + format_ts
    corteia/paths.py        layout de disco, ciente de build congelado
    corteia/hardware.py     CPU/RAM/GPU/CUDA/FFmpeg; ctranslate2_device()
    corteia/project.py      ProjectStore (salvar, abrir, recents, stats, histórico)
    corteia/i18n.py         PT-BR é a língua-fonte; en.json é tabela de tradução
```

Regras transversais que o código segue (`ARCHITECTURE.md` é o contrato):

1. **Nunca** carregar um vídeo inteiro na RAM. Sempre streaming por linha de
   comando FFmpeg (`core/ffmpeg_cli.py` é o único lugar que abre subprocess de
   FFmpeg/ffprobe).
2. Toda função cara aceita `progress` e `cancel: threading.Event`, checa
   `cancel.is_set()` nos laços e levanta `errors.CancelledError`.
3. Erros viram `ProcessingError(stage, message, detail)`; a UI mostra a frase
   em português (`exporter._humanise`, `render._explain_failure`), nunca um
   traceback.
4. Caminhos sempre `pathlib.Path`. Idioma do usuário: PT-BR; código e
   comentários técnicos em inglês.
5. Páginas da UI falam com o núcleo **só** por `ctx.pipeline` / `ctx.exporter`
   / `ctx.*`. Nada de importar `corteia.core.*` direto de um widget (exceção:
   `corteia.core.errors`, usado em `workers/jobs.py:11` para o `except`
   chegar ao usuário).

## Fluxo de um corte, da importação ao MP4

```
ImportPage ──Ctrl+G──► Pipeline.analyze(source, options)
   │
   ├─ media.probe_or_raise(path)            → VideoInfo (duração, largura,
   │                                            altura, fps, rot, has_audio)
   ├─ media.thumbnail(path, dur*0.10, 480)  → PNG em cache_dir()/thumbs
   ├─ transcribe.transcribe(path, model, language="pt", word_timestamps=True)
   │                                        → Transcript (Segment[] com Word[])
   │    └─ transcribe.download_model() se o modelo não estiver em
   │       data_dir()/models; e cache em cache_dir()/transcripts
   ├─ transcribe.diarize(transcript, path)  → atribui `speaker` por heurística
   │                                          de pitch/RMS (sem modelo extra)
   ├─ silence.detect_silences(path, db, min_ms)  → Silence[]  (ffmpeg silencedetect)
   ├─ scenes.detect_scenes(path, threshold, min_scene_len) → Scene[] (PySceneDetect)
   ├─ llm.get_client(cfg).judge(...)        → notas extras (só se api.provider ≠ off)
   └─ analyze.plan_clips(transcript, options=, silences=, scenes=, llm_scores=)
                                            → (Candidate[], Clip[])
   depois: ctx.replace_clips_for(path, clips) e ctx.store.save_current()
            (checkpoint após cada estágio; Esc cancela no meio sem perder nada)

EditorPage ──► Clip.edit (trim, zoom, offsets, aspect, subtitles, música,
                marca d'água, título, remove_silences, manual_text_edits)

ExportPage ──► Exporter.export_all()
   └─ ThreadPoolExecutor(max_workers ≤ 4) sobre Exporter._run(job, settings, cb)
        ├─ media.probe_or_raise(source)
        ├─ Pipeline.timeline_for(clip)      → TimelineMap (silêncios removidos)
        ├─ Pipeline.crop_plan(clip, aspect)
        │     └─ faces.analyze_clip_track(...)  → FaceTrackPoint[]
        │        reframe.plan_crop(points, info, aspect, t0, t1) → CropPlan
        ├─ Exporter._ass_for(...)           → subtitles.build_ass(...)  (arquivo .ass)
        ├─ render.render_clip(clip, source=, info=, settings=, plan=, tmap=,
        │                     out=, ass_path=, progress=cb, cancel=)
        │     └─ render.plan_blocks(tmap, plan, info, fps=)
        │        render.build_graph(blocks, ...)  → -filter_complex_script
        │        ffmpeg_cli.run(cmd, total_duration=, on_progress=cb, cancel=)
        ├─ Exporter._verify(out, tmap.duration, ...)  → re-probe com ffprobe
        └─ Exporter._history(...) + ctx.signals.export_state/export_progress
```

Nomes que não existem: não há `core/audiocheck.py`, não há `resources/icons/`.
A UI desenha ícones vetorialmente com `QPainter` em `ui/theme.py icon()`.

## Contrato de progresso (0..1 vs 0..100)

Este é o ponto que mais gera bug, e é a única regra com exceção escrita:

- **Todo o `corteia/core` reporta fração 0.0–1.0.** `ffmpeg_cli.run`/`FFmpegJob`,
  `silence.detect_silences`, `scenes.detect_scenes`, `transcribe.transcribe`,
  `render.render_clip`, `Pipeline.analyze(on_progress=)`.
- **Exceção histórica:** alguns callbacks de `transcribe.transcribe` emitem
  0–100. Por isso `pipeline._frac()` normaliza: valor `> 1.5` é tratado como
  porcentagem e dividido por 100 (`corteia/core/pipeline.py:46`).
- `Pipeline._emit()` mapeia a fração do estágio para a fatia global da barra
  (`RANGES`: transcribe 0,06–0,58; silence 0,64–0,74; scenes 0,74–0,82;
  llm 0,82–0,90; analyze 0,90–1,00) **e** aceita callback que só receba
  porcentagem: se der `TypeError` com `(float, str)`, reenvia `frac * 100`
  (`pipeline.py:110`). Um estágio já `done` nunca volta para `running` — foi o
  bug do stepper preso em 58%.
- **`workers.JobRunner` é o 0..100.** `ProgressReporter.__call__(percent, label)`
  faz clamp em 0–100 (`jobs.py:115`) e emite `JobRunner.progress(key, percent,
  label)`. Quem chama o núcleo a partir da UI converte:
  `import_page.py:725` → `percent = frac * 100 if 0.0 <= frac <= 1.0 else frac`.
- Widgets de barra (`ui/widgets/progress.py MiniBar/StageProgress`) aceitam os
  dois: valor `≤ 1.5` é multiplicado por 100 (`progress.py:41`).

Resumo operacional: **núcleo = fração, fila de jobs = porcentagem, widget =
tolerante.** Ao escrever uma função nova de core, emita fração. Ao escrever uma
worker function chamada pelo `JobRunner`, passe por `ProgressReporter`.

## Cache

| o que | onde | chave | efeito |
|---|---|---|---|
| transcrição | `cache_dir()/transcripts/<md5>.json` | (arquivo, modelo, idioma) via `transcribe.cache_key()` | segunda execução ~59× mais rápida (0,05 s vs 3,06 s medidos) |
| miniaturas | `cache_dir()/thumbs/` | arquivo + instante + tamanho | cartões dos cortes pintam sem ffmpeg |
| velocidade do Whisper | `cache_dir()/perf.json` | modelo+device+compute+threads | alimenta `estimate_duration_hours()`; sem histórico, a UI avisa que a estimativa é conservadora |
| plano de recorte | `_CROP_CACHE` em memória (`pipeline.py:314`, máx. 64 entradas) | (arquivo, t0, t1, aspect, zoom, offsets, manual) | mover o slider no Editor não re roda o YuNet |
| projeto | `data_dir()/projects/*.json` + `recents.json` + `stats.json` | id do projeto | reabrir não retranscreve (`SourceVideo.transcript` serializado) |

## Configuração: chaves reais

`corteia/config.py DEFAULTS` é a lista canônica. As famílias:

- `ui.*` — `language` (`pt`), `theme` (`dark`), `accent`, `first_run`,
  `window_w`, `window_h`; a janela ainda grava `ui.window_x`/`ui.window_y`
  (fora de `DEFAULTS`, escritos por `main_window.py`).
- `subs.*` — `language`, `default_style` (`viral`), `model` (`base`),
  `beam_size` (5), `word_timestamps`, `diarize`, e os extras que a página de
  configurações grava: `font`, `size`, `uppercase`, `outline`, `position`,
  `reading_speed_cps`.
- `analysis.*` — `duration_preset`, `min_seconds`, `max_seconds`, `max_clips`,
  `min_score`, `remove_silences`, `smart_boundaries`, `follow_speaker`,
  `aspect`.
- `proc.*` — `device` (`auto|cpu|cuda`), `compute_type`
  (`auto|float32|float16|int8`), `cpu_threads`, `max_parallel_jobs`,
  `sample_fps_face`, `scene_threshold`, `silence_db`, `silence_min_ms`,
  `temp_limit_gb`.
- `export.*` — `dir`, `codec` (`libx264`), `resolution` (`1080p`), `fps`,
  `quality_crf`, `preset`, `audio_bitrate`, `pattern`, `burn_subtitles`.
- `api.*` — `provider` (`off`, `openai`, `ollama`, `custom`), `base_url`,
  `key` (redigida em `as_dict(redact=True)`), `model`, `temperature`,
  `max_output_tokens`, `timeout`, `offline_fallback`.
- `net.download_consent` — `False` por padrão; sem isto o `downloader` recusa
  e explica.

Cuidado real, documentado em `INTEGRATION-NOTES.md`: o núcleo lê algumas chaves
com **nome diferente** do `DEFAULTS` — `asr.model`, `asr.language`,
`silence.db`, `silence.min_ms`, `silence.pad`, `silence.min_keep`,
`scenes.threshold`, `scenes.min_len`, `performance.cpu_threads` — e por isso o
Editor de Configurações grava o par (`settings_page.py:174`, `190`, `208` usam
`extra=(...)` para escrever as duas). Ao ler, sempre `cfg.float_("silence.db")
or -35.0`: se a chave não existir, o valor padrão do núcleo assume. Não
"corrija" um lado sem checar o outro.

## Camada de UI

- Páginas: `dashboard`, `import`, `clips`, `editor`, `export`, `settings`
  (construtor `__init__(self, ctx, parent=None)` e `refresh()` obrigatórios);
  `MainWindow._ACCELERATORS` faz `Ctrl+1`…`Ctrl+6`.
- `MainWindow` (`ui/windows/main_window.py`) tem `QStackedWidget`, sidebar
  colapsável, barra superior com estado do pipeline e botão global de
  cancelar, barra de status com progresso, overlay não-modal, `ToastStack`,
  arrastar-e-soltar global e geometria salva na configuração.
- Nada bloqueia a GUI: todo trabalho pesado vai para
  `corteia.workers.jobs.FunctionJob` (QRunnable) numa `QThreadPool`, com
  `Event` de cancelamento compartilhado com o core.

## Empacotamento

`corteia.spec` (PyInstaller one-dir, `console=False`, ícone `assets/icon.ico`,
`datas` para `corteia/resources/qss|models|i18n`, `hiddenimports` dos imports
feitos dentro de função) → `build_linux.sh` (Linux/macOS) e
`build_windows.bat` + `installer/corteia.iss` (Windows). Os modelos Whisper
**não** vão no pacote: são baixados em `data_dir()/models` na primeira execução.
