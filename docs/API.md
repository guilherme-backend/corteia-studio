# API pública do núcleo

Referência das assinaturas usadas pela camada de UI e por scripts. **Todas as
linhas abaixo foram conferidas com `inspect.signature()` no venv do projeto em
2026-11-14** — não são transcrição de um plano. Tipos abreviados:
`ProgressFn = Callable[[float, str], None]` (fração 0..1 + mensagem),
`cancel: threading.Event`.

Regras que valem para tudo aqui:

- Funções caras aceitam `progress=`/`on_progress=` e `cancel=`; checam
  `cancel.is_set()` e levantam `errors.CancelledError`.
- Erros de processamento são `ProcessingError(stage, message, detail)`;
  `ModelMissingError` traz `.hint`. Nada de traceback para o usuário.
- `pathlib.Path` em toda entrada/saída de arquivo.

## corteia.core.media

```python
probe(path: Path) -> VideoInfo
probe_or_raise(path: Path) -> VideoInfo
is_supported(path: Path) -> tuple[bool, str]
thumbnail(path: Path, at: float = -1, size: int = 480, dest: Path | None = None) -> Path
frame_at(path: Path, at: float, width: int = 640) -> np.ndarray | None
extract_wav(path: Path, sr: int = 16000) -> Path
audio_energy_curve(path: Path, hop: float = 0.1, db_floor: float = -70.0,
                   *, max_frames: int = 200000) -> tuple[np.ndarray, np.ndarray]
container_supports_stream_copy(path: Path) -> bool
estimate_export_size(info: VideoInfo, seconds: float, crf: int, res: str) -> int
video_url_or_path(path: str) -> str
invalidate_cache(path: Path | None = None) -> None
```

`SUPPORTED_EXTS` (constante) cobre `.3g2 .3gp .avi .divx .flv .m2ts .m4v .mkv
.mov .mp4 .mpeg .mpg .mts .ogv .ts .vob .webm .wmv`. `probe()` devolve as
dimensões de **exibição** (rotação já aplicada).

## corteia.core.silence

```python
detect_silences(path: Path, *, db: float = -35.0, min_ms: int = 350,
                cancel: threading.Event | None = None, progress=None) -> list[Silence]
parse_silencedetect(text: str, duration: float = 0.0) -> list[Silence]
merge_silences(silences: list[Silence], pad: float = 0.0) -> list[Silence]
gaps(silences: list[Silence], duration: float, min_gap: float = 0.12) -> list[tuple[float, float]]
inside_silence(t: float, silences: list[Silence], pad: float = 0.05) -> bool
cut_points(silences: list[Silence], t0: float, t1: float,
           min_keep: float = 0.35, max_cut: int = 40) -> list[tuple[float, float]]
total_speech(silences: list[Silence], duration: float) -> float
speech_ratio(silences: list[Silence], t0: float, t1: float) -> float
```

Cuidado com o nome: `cut_points()` devolve **faixas de fala a manter**, não
silêncios.

## corteia.core.scenes

```python
detect_scenes(path: Path, *, threshold: float = 27.0, min_scene_len: int = 15,
              cancel: threading.Event | None = None, progress=None) -> list[Scene]
scenes_in(scenes: list[Scene], t0: float, t1: float) -> list[Scene]
scene_count_in(scenes: list[Scene], t0: float, t1: float) -> int
boundaries(scenes: list[Scene], tolerance: float = 0.35) -> list[float]
```

## corteia.core.timeline

```python
@dataclass class TimelineMap:
    ranges: list[tuple[float, float]]

    full(t0: float, t1: float) -> TimelineMap                      # classmethod
    from_silences(silences, t0: float, t1: float, *,
                  min_keep: float = 0.35, pad: float = 0.06,
                  max_cut: int = 40, preserve_between: float = 0.9) -> TimelineMap
    clip_to_src(t: float) -> float
    src_to_clip(t: float) -> float | None
    src_time_at(src_t: float) -> float
    is_identity(t0: float, t1: float) -> bool
    window(s0: float, s1: float) -> tuple[float, float, list[tuple[float, float]]]
    trim_concat_filter(vlabel: str = '0:v', alabel: str = '0:a', *,
                       v_out: str = 'vout', a_out: str = 'aout',
                       fps: float | None = None, sar: int = 1,
                       vf_extra: str = '', af_extra: str = '') -> str
    # propriedades: .cuts, .duration, .ranges, .removed
```

Toda conversão de tempo entre o vídeo original e o corte exportado passa por
aqui. A legenda é remontada com `tmap`, por isso a sincronia continua certa
depois de remover silêncio.

## corteia.core.transcribe

```python
MODELS: dict[str, dict]      # tiny base small medium large-v3 distil-large-v3 auto
MODEL_URLS: dict[str, str]
CLOUD_MODELS: ...
normalize_name(model: str) -> str
resolve_model(model: str = 'auto', audio_seconds: float | None = None,
              ram_gb: float | None = None) -> str
recommended_model(audio_seconds: float | None = None) -> str
model_dir(model: str) -> Path
is_downloaded(model: str) -> bool
model_size_on_disk(model: str) -> int
model_catalog() -> list[dict[str, Any]]
download_model(model: str, *, on_progress: ProgressFn | None = None,
               cancel: threading.Event | None = None) -> Path
unload_model(model: str | None = None) -> int
loaded_model() -> str
transcribe(path: Path, *, model: str = 'base', language: str = 'pt',
           beam_size: int = 5, word_timestamps: bool = True,
           vad_filter: bool = False, device: str = 'auto',
           compute_type: str = 'auto', on_progress: ProgressFn | None = None,
           cancel: threading.Event | None = None, condition_on: str = '',
           initial_prompt: str = '', reuse_cache: bool = True,
           store_cache: bool = True, cpu_threads: int = 0) -> Transcript
transcribe_is_cached(path: Path | str, model: str, language: str) -> bool
transcripts_dir() -> Path
cache_key(path: Path | str, model: str, language: str) -> str
cache_file(path: Path | str, model: str, language: str) -> Path
clear_transcript_cache() -> int
diarize(transcript: Transcript, path: Path, *,
        cancel: threading.Event | None = None, kmax: int = 4,
        on_note: Callable[[str], None] | None = None) -> Transcript
speaker_counts(transcript: Transcript) -> dict[int, float]
apply_text_edits(transcript: Transcript, edits: dict[int, str]) -> Transcript
words_for_range(transcript: Transcript, t0: float, t1: float) -> list[Word]
join_words(words: Iterable[Word | str]) -> str
words_text(segment: Segment) -> str
extract_pcm(path: Path, t0: float, t1: float, *, sr: int = 16000,
            cancel: threading.Event | None = None, max_seconds: float = 120.0) -> np.ndarray
voice_features(x: np.ndarray, sr: int = 16000) -> _VoiceFeatures
perf_file() -> Path
record_speed(name: str, audio_seconds: float, wall_seconds: float, *,
             threads: int, device: str = 'cpu', compute: str = 'int8') -> float
measured_ratio(name: str, threads: int) -> tuple[float, str]
estimate_duration_hours(model: str, audio_seconds: float, cpu_threads: int = 0) -> str
```

`diarize()` é **heurística acústica** (pitch/RMS + espaçamento de timestamps),
determinística e documentada: não há modelo de diarização. `on_note` recebe o
aviso que a UI mostra.

## corteia.core.faces

```python
YUNET_FILE = 'face_detection_yunet_2023mar.onnx'
detector_available() -> bool
detector_name() -> str          # 'yu-net' aqui
detector_note() -> str          # vazio quando nada houve; explica o fallback
detect_faces(frame: np.ndarray, min_size: int = 48,
             *, score_threshold: float = 0.55) -> list[Face]
analyze_clip_track(path, info: VideoInfo, t0: float, t1: float, *,
                   sample_fps: float = 6.0, min_size: int = 48,
                   on_progress: Callable[[float, str], None] | None = None,
                   cancel: threading.Event | None = None) -> list[FaceTrackPoint]
merge_tracks(points: list[FaceTrackPoint]) -> list[FaceTrackPoint]
```

Ordem de fallback: YuNet (ONNX, OpenCV DNN) → `cv2.CascadeClassifier` (só
OpenCV 4) → sem rosto (lista vazia, e `reframe` decide por saliência ou
central). O motivo vai em `detector_note()`.

## corteia.core.reframe

```python
crop_for_aspect(src_w: int, src_h: int, aspect: AspectRatio) -> tuple[int, int]
static_center_crop(src_w: int, src_h: int, aspect: AspectRatio) -> tuple[int, int, int, int]
saliency_center(path, info: VideoInfo, t0: float, t1: float, aspect: AspectRatio,
                *, frames: int = 6) -> tuple[float, float] | None
plan_crop(points: list[FaceTrackPoint], info: VideoInfo, aspect: AspectRatio,
          t0: float, t1: float, *, follow_speaker: bool = True, zoom: float = 1.0,
          offset_x: float = 0, offset_y: float = 0, manual: str = '') -> CropPlan
crop_expr_at(plan: CropPlan, t: float) -> tuple[int, int, int, int]
segments_of(plan: CropPlan) -> list[tuple[float, float, int, int, int, int]]
ffmpeg_reframe_filter(plan: CropPlan, out_w: int, out_h: int, fps: float,
                      *, mode: str = 'filtergraph') -> str
blur_fill_filter(out_w: int, out_h: int, *, sigma: float = 24.0) -> str
needs_blur_fill(src_w: int, src_h: int, aspect: AspectRatio) -> bool
```

Contrato do `CropPlan`: `.points = [(t, x, y, w, h)]` em **píxeis da fonte**,
com `w`/`h` **constantes** em todo o plano (só `x,y` variam) — exigência do
`concat` do FFmpeg.

## corteia.core.subtitles

```python
preset(style: SubtitleStyle) -> SubtitleSettings
STYLE_PRESETS: dict[SubtitleStyle, SubtitleSettings]      # viral, professional,
                                                          # podcast, dynamic,
                                                          # minimalist, none
cues(transcript | None, t0: float, t1: float, settings: SubtitleSettings,
     edits: dict[int, str] | None = None, tmap: TimelineMap | None = None,
     *, limit_lines: int = 2) -> list[Cue]
auto_font_size(settings: SubtitleSettings, height: int) -> int
ass_document(cue_list: list[Cue], settings: SubtitleSettings, width: int, height: int,
             *, title: str = 'CorteIA',
             speaker_names: dict[int, str] | None = None) -> str
build_ass(transcript | None, t0: float, t1: float, settings: SubtitleSettings,
          dest: Path, *, clip_w: int, clip_h: int,
          edits: dict[int, str] | None = None, tmap: TimelineMap | None = None,
          speaker_names: dict[int, str] | None = None) -> Path
to_srt(cue_list: list[Cue], *, max_lines: int = 2) -> str
to_vtt(cue_list: list[Cue], *, max_lines: int = 2) -> str
plain_transcript(cue_list: list[Cue]) -> str
sync_report(cue_list: list[Cue], tmap: TimelineMap) -> dict[str, float]
```

`Cue(start, end, words, lines, highlight, speaker)` e `AssDoc(...).text()`. O
bloco `Style:` do ASS tem **exatamente 23 campos**, com cores `&HBBGGRR&` só
dentro de overrides (`\c&...&`); karaokê `\kf` restrito a podcast/dinâmico.

## corteia.core.render

```python
output_size(settings: ExportSettings, aspect: AspectRatio | None) -> tuple[int, int]
render_clip(clip: Clip, *, source: Path, info: VideoInfo,
            transcript: Transcript | None, settings: ExportSettings,
            plan: CropPlan | None, tmap: TimelineMap, out: Path, progress=None,
            cancel=None, threads: int = 0, proxy: bool = False,
            ass_path: Path | None = None, fit_mode: str = 'auto') -> Path
preview_render(clip: Clip, dest: Path, *, seconds: float = 6.0, width: int = 480,
               progress=None, cancel=None) -> Path
plan_blocks(tmap: TimelineMap, plan: CropPlan | None, info: VideoInfo, *,
            fps: float, max_blocks: int = 80) -> list[Block]
crop_segments(plan: CropPlan | None, t0: float, t1: float) -> list[tuple[...]]
build_graph(blocks: list[Block], *, src: VideoInfo, out_w: int, out_h: int,
            has_audio: bool, ass: Path | None, clip_w: int, clip_h: int, edit,
            volume: float, music_index: int | None, duration: float,
            fit_mode: str, wm_index: int | None) -> str
video_graph(blocks, src, out_w, out_h, *, fit_mode='auto') -> list[str]
audio_graph(blocks) -> list[str]
audio_post_graph(label, *, duration, volume, music_index, music_volume, duck,
                 out='aout') -> list[str]
overlay_graph(label, *, ass, clip_w, clip_h, watermark_image, watermark_text,
              wm_pos, wm_opacity, wm_scale, title_text, title_style,
              wm_input_index) -> tuple[list[str], str]
fit_filter(crop, src, out_w, out_h, *, index, label_in, mode='auto') -> list[str]
drawtext(text, *, size, x, y, color='white', alpha=1.0, outline=0, bold=False) -> str
stamp_xy(pos, out_w, out_h, obj_w, obj_h, *, text: bool = False) -> tuple[str, str]
filter_path(path) -> str ; quoted(path) -> str ; text_escape(text) -> str
```

`output_size()` trata “1080p” como o **lado curto**: 9:16 vira 1080×1920, 4:5
vira 1080×1350, 1:1 vira 1080×1080, 16:9 vira 1920×1080.

## corteia.core.exporter

```python
class Exporter:
    def __init__(self, ctx) -> None
    queue: list[ExportJob]
    running: bool                                   # propriedade
    export_all(clips=None, settings=None, *, on_progress=None) -> dict
        # {'ok': int, 'failed': int, 'outputs': list[str]} — bloqueante por
        #   projeto; a UI chama dentro de um JobRunner
    export_one(clip_id: str, settings=None, *, on_progress=None) -> str
        # devolve o caminho do MP4 ou levanta CorteiaError
    cancel() -> None

estimate(ctx, clips=None) -> dict
    # {'clips', 'mb', 'minutes', 'measured', 'out_dir', 'speed'}
    # 'measured' diz se a velocidade veio do seu histórico ou é o chute padrão
```

Internamente, `Exporter._run()` é a esteira: `timeline_for` → `crop_plan` →
`_ass_for` → `render.render_clip` → `_verify` (ffprobe) → `_history`.

## corteia.core.pipeline

```python
class Pipeline:
    def __init__(self, ctx) -> None
    running: bool                                   # propriedade
    analyze(source_path=None, options: AnalysisOptions | None = None, *,
            on_progress: Callable[[float, str], None] | None = None,
            on_stage: Callable[[str, str], None] | None = None,
            cancel: threading.Event | None = None,
            force: bool = False) -> list[Clip]
    stages() -> list[dict]      # {key,label,state,progress(0..1),detail,elapsed}
    cancel() -> None
    crop_plan(clip, aspect=None, *, on_progress=None, cancel=None,
              force: bool = False)
    timeline_for(clip, *, remove_silences: bool | None = None) -> TimelineMap

crop_plan_for(path, info: VideoInfo, t0: float, t1: float, aspect, options,
              *, on_progress=None, cancel=None, force: bool = False)
STAGE_ORDER = ['probe', 'thumb', 'transcribe', 'silence', 'scenes', 'analyze']
RANGES      # fatia de 0..1 da barra global por estágio
```

## corteia.core.analyze

```python
plan_clips(transcript: Transcript, *, options: AnalysisOptions, source_path: str = '',
           duration: float = 0.0, silences=None, scenes=None, curve=None,
           llm_scores: dict[str, float] | None = None, topic_breaks=None,
           progress=None) -> tuple[list[Candidate], list[Clip]]
build_blocks(transcript, silences=None, *, gap=1.25, max_len=0.0, topic_breaks=None,
             scene_breaks=None, merge_speaker_change=True) -> list[Block]
enumerate_candidates(blocks, options, *, max_span_blocks=16) -> list[Candidate]
compute_signals(cands, blocks, options, *, silences=None, scenes=None, curve=None) -> None
score(cands, llm=None) -> None
select(cands, options) -> list[Candidate]
dedupe(cands, *, threshold=0.42, max_per_start=1) -> list[Candidate]
snap_boundaries(c, blocks, duration, silences, *, smart=True, lead_in=0.12,
                tail_out=0.22, word_starts=None) -> tuple[float, float]
scene_boundaries(scenes, *, min_len=3.0) -> list[float]
target_range(options) -> tuple[float, float]
explain(c: Candidate, top: int = 3) -> str
to_clip(c: Candidate, source_path: str, options: AnalysisOptions, *,
        rank: int = 1, total: int = 1) -> Clip
```

Sinais removidos de propósito: similaridade léxica não separa tópicos (intra
0,042 vs inter 0,044), então a fronteira vem de pausa, falante, cenas ou API.

## corteia.core.llm

```python
class LLMClient:
    def __init__(self, config=None) -> None
    chat(messages: list[dict], *, json_mode: bool = False,
         temperature: float | None = None, max_tokens: int | None = None,
         cancel: threading.Event | None = None) -> str
    judge(items: list[tuple[str, str, float]], *, progress=None,
          cancel: threading.Event | None = None,
          chunk_chars: int = 9000) -> dict[str, float]
    titles(items: list[tuple[str, str]], *, cancel=None) -> dict[str, str]
    topics(transcript: Transcript, duration: float, *, cancel=None,
           max_points: int = 14) -> list[float]
    status() -> LLMStatus
    test_connection() -> tuple[bool, str]
    usable() -> bool

get_client(config=None) -> LLMClient
LLMStatus(provider, model, base_url, usable, reason='')
```

Saída de `corteia.core.downloader`:

```python
is_url(text: str) -> bool
can_download(url: str) -> tuple[bool, str]
build_opts(dest_dir: Path, on_progress: ProgressFn | None = None,
           cancel: threading.Event | None = None) -> dict[str, Any]
download(url: str, dest_dir: Path | None = None, *,
         on_progress: ProgressFn | None = None,
         cancel: threading.Event | None = None) -> Path
best_format_url(url: str) -> dict
```

Exige `net.download_consent` verdadeiro; senão levanta `ProcessingError`
explicando onde ligar. A escolha de formato é mp4 até 1080p, sem subtítulos.

## corteia.core.fonts

```python
installed() -> list[str]              # lru_cache; usa fc-list / registro do Windows
has(name: str) -> bool
pick(preferred: str, *, extra: list[str] | None = None) -> tuple[str, bool]
bold_variants(name: str) -> list[str]
file_index() -> dict[str, list[str]]
file_for(name: str, *, bold: bool = False) -> str
invalidate() -> None
SUBSTITUTES / SAFE_FALLBACKS          # ordem de substituição honesta
```

`pick()` devolve `(fonte_usada, foi_substituida)`. A UI usa o segundo valor
para avisar: “A fonte “Montserrat” não está instalada; a legenda vai usar
“Noto Sans”.” Aqui, `pick('Montserrat') → ('Noto Sans', True)` e
`pick('Arial Black') → ('DejaVu Sans', True)`.

## corteia.core.ffmpeg_cli (executor único de FFmpeg)

```python
ffmpeg_bin() -> str ; ffprobe_bin() -> str ; available() -> bool
run(args, *, timeout=None, cancel=None, total_duration=None, on_progress=None) -> str
capture(args, *, ...) -> tuple[str, str]
run_json(args) -> dict
stderr_lines(args, *, ...) -> list[str]
spawn_reader(args, *, ...) -> PipedProcess
parse_progress_line(line: str, total: float) -> float | None
extract_seconds(line: str) -> float | None
as_percent(p: float) -> int
encode_cmd(inputs, global_opts, vfilter, afilter, out, *, codec, crf, preset,
           fps, audio_bitrate, extra=None, threads=0) -> list[str]
copy_cmd(inputs, out, *, start=None, end=None, extra=None, fast=False) -> list[str]
silence_detect_cmd(path: Path, db: float, min_duration_s: float) -> list[str]

class FFmpegJob: start(cmd, *, total_duration, on_progress, on_error, cancel) ; wait() ; cancel()
```

`run()` levanta `MediaError` com as últimas 15 linhas do stderr quando o
código de saída é diferente de zero. Em Windows usa `CREATE_NO_WINDOW`.

## Fora do core

`corteia.config` (`DEFAULTS`, `Config.get/set/update/as_dict/str_/int_/float_/
bool_/changed/on/reset/save/load`, `get_config()`), `corteia.paths`
(`data_dir`, `cache_dir`, `projects_dir`, `logs_dir`, `models_dir`,
`bundled_model`, `temp_root`, `temp_sub`, `temp_file`, `clean_temp`,
`dir_size`, `human_bytes`, `exports_dir_default`, `bundle_root`, `is_frozen`),
`corteia.hardware` (`probe() -> HardwareReport`, `find_ffmpeg`,
`find_ffprobe`, `ctranslate2_device()`, `parallel_limit()`,
`invalidate_cache()`), `corteia.models` (dataclasses e enums com `to_dict`/
`from_dict` round-trip), `corteia.project.ProjectStore` (`save`,
`save_current`, `load`, `new`, `recents`, `delete`, `global_stats`,
`export_history`, `missing_sources`) e `corteia.context.AppContext` /
`Signals` (descritos em `docs/ARQUITETURA.md`).
