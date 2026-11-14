# Notas de contrato (verificadas no código, 2026-11-14)

Ambiente: `cd /home/ubuntu/corteia-studio && . .venv/bin/activate`; GUI sem display `QT_QPA_PLATFORM=offscreen`.
Vídeo de teste: `/tmp/asr_test.mp4` (37,587 s, 640x360, h264+aac). Projetos já analisados em
`~/.config/corteia-studio/projects/*.json` (ex.: `de9e5e637f5a.json`, 3 clips, 8 segmentos, 8 silêncios, 1 cena).
Modelo whisper `tiny` e `base` já baixados (`TR.is_downloaded`).

## Assinaturas reais confirmadas
- `AppContext`: `project, signals, config, pipeline, exporter, store, clip(id), save(), toast(msg,level), go(page),
  add_source(path,info,*,origin), source_for(path), transcript_for(path), active_source(), current_source_path(),
  replace_clips_for(path,clips), export_target(clip), mark_export(id,state,out), is_busy(), new_project(name),
  load_project(path), drop_source(path), set_active_source(src)`
- `Signals`: project_changed, project_loaded(object), recents_changed, navigate(str), toast(str,str),
  sources_changed, active_source_changed, stage_started(str,str), stage_progress(str,str,float,bool),
  stage_finished(str,bool,str), busy_changed(bool,str), clips_changed, clip_updated(str), clip_selected(str),
  export_progress(str,float,str), export_state(str,str), exports_finished(int,int), history_changed
- `Pipeline.analyze(source_path=None, options=None, *, on_progress, on_stage, cancel, force=False) -> list[Clip]`
  (on_progress recebe fração 0..1; raise ProcessingError se já rodando). `.stages()` -> list de
  `{key,label,state in idle|running|done|error|cancelled,progress(0..1),detail,elapsed}`. STAGE_ORDER =
  probe,thumb,transcribe,silence,scenes,analyze (RANGES tem também diarize/llm).
  `.timeline_for(clip, *, remove_silences=None) -> TimelineMap(ranges)` — **não tem `.removals`**;
  tem `.duration .removed(float) .cuts .src_to_clip .clip_to_src .src_time_at .window`.
  `.crop_plan(clip, aspect=None, *, on_progress, cancel, force)` → lê `zoom/offset_x/offset_y/manual_crop/
  follow_speaker` de `clip.edit` (ClipEdit só tem `zoom`; offsets/manual/seguir-falante são ignorados →
  por isso a UI aplica offsets e position_override por conta própria).
- `Exporter.export_all(clips=None, settings=None, *, on_progress) -> dict(ok,failed,outputs)`;
  `export_one(clip_id, settings=None, *, on_progress) -> str`; `.queue: list[ExportJob]`; `.cancel()`;
  `corteia.core.exporter.estimate(ctx, clips=None) -> dict(clips,mb,minutes,measured,speed,out_dir)`.
  `_history` grava `project.exports` como **dicts** (não ExportJob) e depois `ctx.save()` quebra em
  `Project.to_dict()` (dict sem `.to_dict`) → páginas devem gravar o próprio histórico e chamar save()
  antes/atualizar sem depender disso. `export_one` emite export_state/export_progress/clip_updated.
- `render.preview_render(clip, dest, *, seconds=6.0, width=480, progress, cancel)` → **BUG do núcleo:
  NameError: `Resolution` não importado em core/render.py**. Workbook: `render.render_clip(..., proxy=True)`
  com `ExportSettings(resolution=Resolution.R_720, quality_crf=32, preset="ultrafast")` + `TimelineMap.full`.
- `render.output_size(settings, aspect) -> (w,h)` ok.
- `media.thumbnail(path, at=-1, size=480, dest=None) -> Path` ok.
- `SUB.preset(style)`, `SUB.STYLE_PRESETS`, `SUB.cues(transcript,t0,t1,settings,edits,tmap,*,limit_lines=2)`,
  `SUB.build_ass(transcript,t0,t1,settings,dest,*,clip_w,clip_h,edits=None,tmap=None,speaker_names=None)` ok.
  Cue(start,end,words[Word],lines,highlight(set de índices),speaker).
- `JobRunner` (workers/jobs.py): `run(fn, name, on_done, on_error, on_progress) -> Job`; `fn(progress, cancel)`
  (2 params → recebe ProgressReporter + threading.Event); progresso em 0..100; `runner.cancel(name)` existe;
  `SimpleJobRunner` (busy.py fallback) só tem `cancel_all()` → usar try/AttributeError.
  `runner_for(ctx)`, `get_runner(ctx)` (busy) → JobRunner.
- `theme`: color/qcolor/icon/pixmap/font/mono_font/apply/format_duration_long/relative_time/date_time/
  open_location(path, reveal=False)/placeholder_pixmap/rounded_pixmap. Tokens DARK: bg, surface, surface2,
  surface3, border, border_strong, text, text_dim, muted, faint, accent, success, warn, error, info,
  on_accent, hover, press, track, overlay, shadow, scrollbar.
- `StageProgress` lê stages como `{id,label,done,percent(0..100 ou 0..1),note}`; convertemos o formato do
  núcleo para esse shape no page (ver ClipsPage._push_stages).
- `ClipCard(clip, thumb_path, parent)` sinais approved/rejected/edited/exported/selected/previewRequested;
  atributos title/times/desc/score_bar/score_value/score_caption/btn_ok/btn_no/btn_edit/btn_exp.
- `EmptyState(title,message,icon,parent)` + set_action(text, slot).
- `VideoPlayer(ctx,parent)`: set_fps, set_frame_size(w,h), set_source(path,start,end), set_range, seek(s),
  position(), duration(), set_crop_rect((x,y,w,h) px da fonte), set_crop_label, set_loop_range(t0,t1),
  clear_loop_range, speed, current_pixmap(), signals timeChanged(float)/mediaLoaded(bool)/ended(float),
  lbl_status/set_status, btn_play/btn_stop/slider/combo_speed/chk_loop.
- `SwitchButton(text,checked,parent)` checkedChanged/toggled, setChecked(v, notify)
- `SegmentedControl(options[(key,label)], value, parent, size="sm")` changed(str), setValue(v, notify)
- `paths`: data_dir, cache_dir, projects_dir, temp_file(suffix, subdir)
- config keys usados: export.dir, export.resolution, export.codec, export.quality_crf, export.preset,
  export.fps, export.audio_bitrate, export.burn_subtitles, export.faststart, ui.theme, ui.accent,
  proc.max_parallel_jobs. `Config.bool_/int_/float_/str_/set/save`
- `ctx.config.bool_("export.burn_subtitles", True)` aceito com 2 args.

## Modelos
- Clip: id, source_path, start, end, title, description, transcript, keywords, score(ScoreBreakdown:
  total, signals{}, weights{}, llm_used, llm_reason), status(ClipStatus), reason, edit(ClipEdit),
  scene_count, speaker_count, exported_path, duration, rating.
- ClipEdit: trim_start, trim_end, crop_offset_x/y, zoom, position_override("x,y,w,h"), volume, music_path,
  music_volume, duck_music, watermark_path, watermark_text, watermark_pos(tl/tr/bl/br/c), watermark_opacity,
  watermark_scale, title_text, title_style(bar|plain|none), aspect, subtitles, remove_silences,
  manual_text_edits{int:str}.
- SubtitleSettings: style, font, size(0=auto), color, highlight_color, highlight_color2, outline, shadow,
  border_style(1|3), position(bottom|center|top), background, bg_opacity, max_words_per_line,
  highlight_importance, highlight_mode(yellow|green|multicolor), uppercase, reading_speed_cps.
- ExportSettings: codec(Codec), resolution(Resolution), fps, quality_crf, preset, audio_bitrate, aspect,
  out_dir, filename_pattern, max_parallel, burn_subtitles, faststart, target_size.
- ExportJob(clip_id, source_path, out_path, state, progress, message, error, started_at, finished_at, settings)
- Sinais do score em PT: hook→gancho, emotion→emoção, story_arc→arco de história, completeness→fechamento,
  debate→potencial de discussão, energy→energia, visual→apelo visual, density→densidade de fala,
  duration_fit→ajuste de duração, speakers→falantes, silence_penalty→penalidade por silêncio,
  gate→nota do modelo de IA.
