# CorteIA Studio — contrato de arquitetura (obrigatório seguir)

App desktop **PySide6** para cortes automáticos com IA. Python 3.12, FFmpeg real.
**Proibido**: mocks, dados fictícios, botões sem função, `TODO` no lugar de implementação.

## Regras transversais

1. **Nunca** carregar um vídeo inteiro na RAM. Sempre streaming por linha de comando FFmpeg.
2. Toda função cara recebe `progress: Callable[[float, str], None] | None` e
   `cancel: threading.Event | None`. Checar `cancel.is_set()` em cada laço e levantar
   `CancelledError` (`corteia/core/errors.py`).
3. Erros viram `ProcessingError(stage, message, detail)` — a UI mostra o texto em PT,
   nunca um traceback. Nunca `print`; usar `logging.getLogger("corteia")`.
4. Caminhos sempre `pathlib.Path`. Nada de `/` fixo: `Path` resolve em Windows.
5. Toda função pública com docstring curta. Sem comentários óbvios.
6. Imports de `corteia.models` / `corteia.config` / `corteia.paths` / `corteia.hardware`
   já existem e **não devem ser modificados**.
7. Idioma do texto para o usuário: **português do Brasil**. Código e comentários técnicos em inglês.

## Módulos existentes (não reescrever)

`corteia/paths.py`, `corteia/config.py`, `corteia/models.py`, `corteia/hardware.py`,
`corteia/project.py`, `corteia/context.py`, `corteia/i18n.py`.

`models` exporta: `VideoInfo, Word, Segment, Transcript, Scene, Silence, FaceTrackPoint,
CropPlan, ScoreBreakdown, SubtitleSettings, ClipEdit, Clip, ExportSettings, ExportJob,
AnalysisOptions, SourceVideo, Project, Stage, AspectRatio, SubtitleStyle, Resolution,
Codec, ClipStatus, JobState, format_ts`.

## Núcleo — assinatura exata (pacote `corteia/core/`)

### errors.py
```python
class CorteiaError(Exception): ...
class CancelledError(CorteiaError): ...
class ProcessingError(CorteiaError):          # .stage .message .detail
class MediaError(ProcessingError): ...
class TranscriptionError(ProcessingError): ...
class RenderError(ProcessingError): ...
class ModelMissingError(ProcessingError):     # .hint
class FFmpegMissingError(CorteiaError):
```

### ffmpeg_cli.py — executor único de FFmpeg
```python
def ffmpeg_bin() -> str;  def ffprobe_bin() -> str
def run(args: list[str], *, timeout: float | None = None, cancel: Event | None = None,
        total_duration: float | None = None,
        on_progress: Callable[[float, str], None] | None = None) -> str   # stdout
def run_json(args: list[str]) -> dict
class FFmpegJob:            # processo vivo, monitorado em thread própria
    def start(cmd: list[str], *, total_duration, on_progress, on_error) -> FFmpegJob
    def wait(timeout=None) -> int
    def cancel();  prop .alive;  .percent
def parse_progress_line(line: str, total: float) -> float | None   # out_time_ms / frame= / time=
def encode_cmd(inputs: list[str], global_opts: list[str], vfilter: str | None,
               afilter: str | None, out: Path, *, codec, crf, preset, fps,
               audio_bitrate, extra: list[str] | None = None,
               threads: int = 0) -> list[str]
```
* Parser lê stderr em stream, calcula % de `out_time_ms`/`time=` vs `total_duration`.
* `cancel.set()` → `proc.terminate()` → 1,5 s → `kill()`. Em Windows usar
  `creationflags=CREATE_NO_WINDOW`.
* `run` levanta `MediaError` com as últimas 15 linhas do stderr quando o código ≠ 0.

### media.py
```python
def probe(path: Path) -> VideoInfo                    # ffprobe -show_format -show_streams
def probe_or_raise(path: Path) -> VideoInfo           # valida existência + duração > 0
def is_supported(path: Path) -> tuple[bool, str]      # extensão/mime; mensagem em PT
def thumbnail(path: Path, at: float = -1, size: int = 480, dest: Path | None = None) -> Path
    # at<0 -> 10% do vídeo; usa ffmpeg -ss; cache em paths.cache_dir()/thumbs
def frame_at(path: Path, at: float, width: int = 640) -> np.ndarray | None    # cv2 BGR
def extract_wav(path: Path, sr: int = 16000) -> Path                          # mono pcm_s16le
def audio_energy_curve(path: Path, hop: float = 0.1, db_floor: float = -70.0) -> tuple[np.ndarray, np.ndarray]
    # retorna (tempos, dB rms por quadro) via astats/ebur128 em blocos; nunca decodifica tudo em RAM
def container_supports_stream_copy(path: Path) -> bool
def estimate_export_size(info: VideoInfo, seconds: float, crf: int, res: str) -> int
def video_url_or_path(path: str) -> str               # escapa para uso em filtergraph
```

### silence.py
```python
def detect_silences(path: Path, *, db: float = -35.0, min_ms: int = 350,
                    cancel=None, progress=None) -> list[Silence]   # ffmpeg silencedetect=n=XdB:d=Y
def gaps(silences: list[Silence], duration: float, min_gap: float = 0.12) -> list[tuple[float, float]]
def inside_silence(t: float, silences: list[Silence], pad: float = 0.05) -> bool
def cut_points(silences, t0, t1, min_keep: float = 0.35, max_cut: int = 40) -> list[tuple[float, float]]
def total_speech(silences, duration) -> float
def speech_ratio(silences, t0, t1) -> float
```

### scenes.py
```python
def detect_scenes(path: Path, *, threshold: float = 27.0, min_scene_len: int = 15,
                  cancel=None, progress=None) -> list[Scene]   # PySceneDetect AdaptiveDetector
def scenes_in(scenes: list[Scene], t0, t1) -> list[Scene]
def scene_count_in(scenes, t0, t1) -> int
```
* Use `scenedetect.detect()`/`ContentDetector`/`AdaptiveDetector` com `show_list=False`;
  converter frames→segundos com o FPS do `VideoInfo`. Não abrir GUI do scenedetect.

### transcribe.py
```python
MODEL_URLS / MODELS = {"tiny","base","small","medium","large-v3","distil-large-v3","whisper-1"}
def model_dir(model: str) -> Path
def is_downloaded(model: str) -> bool
def download_model(model: str, *, on_progress=None, cancel=None) -> Path   # faster-whisper/HF
def transcribe(path: Path, *, model: str = "base", language: str = "pt",
               beam_size: int = 5, word_timestamps: bool = True,
               vad_filter: bool = False, device: str = "auto", compute_type: str = "auto",
               on_progress: Callable[[float, str], None] | None = None,
               cancel=None, condition_on: str = "", initial_prompt: str = "") -> Transcript
def diarize(transcript: Transcript, path: Path, *, cancel=None) -> Transcript
    # Heurística real: agrupa segmentos por mudanças de pitch/RMS (sem modelo extra) e
    # atribui `speaker` 0..N. Deve ser determinística e documentada.
def apply_text_edits(transcript: Transcript, edits: dict[int, str]) -> Transcript
def words_for_range(transcript, t0, t1) -> list[Word]   # clamp nas bordas
```
* `faster_whisper.WhisperModel(download_root=paths.models_dir(), device=..., compute_type=...)`.
* Progresso: `segment.end / duration`. `ModelMissingError` com hint quando offline.
* Converter `seg.words` em `Word(start, end, word.strip(), probability)`; **preservar a
  pontuação original** e o espaçamento (juntar tokens com leading space).

### faces.py  (OpenCV ≥ 4.5 / 5.x)
```python
YUNET_FILE = "face_detection_yunet_2023mar.onnx"   # paths.bundled_model(YUNET_FILE)
def detector_available() -> bool
def detect_faces(frame: np.ndarray, *, min_size: int = 48) -> list[Face]  # Face dataclass
class Face:  x,y,w,h,score, landmarks (5x2 np)   prop .cx,.cy,.area
def analyze_clip_track(path, info, t0, t1, *, sample_fps=6.0,
                       on_progress=None, cancel=None) -> list[FaceTrackPoint]
    # amostra frames por cv2.VideoCapture com CAP_PROP_POS_MSEC, agrupa por track_id
    # (IoU + centroid EMA), mede "speaking" = variância temporal da boca (região
    # inferior do bbox) normalizada. Retorna pontos ordenados por t.
def merge_tracks(points: list[FaceTrackPoint]) -> list[FaceTrackPoint]
```
* **Fallback em ordem**: YuNet ONNX → `cv2.CascadeClassifier` (só existe no OpenCV 4) →
  sem rosto (retorna lista vazia; `reframe` decide o resto). Registrar o motivo em `note`.
* Nunca decodificar o vídeo inteiro; pular frames (`step = fps/sample_fps`).

### reframe.py
```python
def crop_for_aspect(src_w, src_h, aspect: AspectRatio) -> tuple[int,int]     # maior centro
def plan_crop(points: list[FaceTrackPoint], info: VideoInfo, aspect: AspectRatio,
              t0: float, t1: float, *, follow_speaker=True, zoom=1.0,
              offset_x=0, offset_y=0, manual="") -> CropPlan
    # 1) escolhe trilha (falante ativo > maior rosto > centrado)
    # 2) suaviza cx/cy com EMA + clamp de velocidade (px/s) para não "pular"
    # 3) gera keyframes (t,x,y,w,h) espaçados <= 0,5 s, todos dentro dos limites
    # 4) sem rosto: saliency (Sobel) ou corte estático central; mode != "face"
def crop_expr_at(plan: CropPlan, t: float) -> tuple[int,int,int,int]   # interpola keyframes
def ffmpeg_reframe_filter(plan, out_w, out_h, fps, *, mode="filtergraph") -> str
    # se poucos keyframes -> zoompan/crop estático; senão constrói Expressions com
    # 'if(between(t,..))' limitadas, OU (preferido) gera um motion path
    # aplicado via -vf "crop=...,scale=...,setsar=1" por segmento e concatena.
def static_center_crop(src_w, src_h, aspect) -> tuple[int,int,int,int]
def blur_fill_filter(out_w, out_h) -> str    # fundo desfocado para vídeo estreito
```
* Importante: a estratégia final adotada é **por segmentos** — o `render` corta o vídeo
  em N blocos (um por keyframe range), aplica `crop` estático em cada bloco e junta com
  `concat`. Isso evita expressões gigantes e é o que o FFmpeg faz de forma confiável.
  `plan_crop` deve produzir no máximo `max_keyframes=40` pontos (agrupando quando excede).

### downloader.py
```python
def can_download(url: str) -> tuple[bool, str]     # valida scheme + consentimento config
def download(url: str, dest_dir: Path, *, on_progress=None, cancel=None) -> Path
    # yt-dlp, melhor mp4 <=1080p, sem subtítulos; respeita robots/ToS; mensagem em PT
def best_format_url(url: str) -> dict
```
* `net.download_consent` precisa ser `True`; caso contrário `ProcessingError` explicando
  que o usuário deve aceitar em Configurações → Download.

## UI (pacote `corteia/ui/`)

Tema escuro por QSS (`ui/theme.py`, `resources/qss/dark.qss`). Paleta:
fundo `#0E0F14`, superfícies `#15171E`/`#1B1E27`, texto `#E8EAF2`/`#9AA0B5`,
acento `#7C5CFF`, sucesso `#22E07A`, alerta `#FFB020`, erro `#FF5C7A`.
Raio 10 px, animações `QPropertyAnimation` de 160–260 ms (opacidade/posição),
`QPropertyAnimation` no indicador de barra lateral.

`ui/theme.py`:
```python
def apply(app, *, mode="dark", accent="#7C5CFF")
def qss_path(mode) -> Path;  def color(name) -> str;  def icon(name, color=None, size=22) -> QIcon
def font(size=14, weight="normal") -> QFont
```
Ícones: gerados **vetorialmente** por `QPainter` em `theme.icon(name)` (não depender de
pacote de ícones). Nomes necessários: home, import, magic, subtitles, crop, edit, export,
settings, play, pause, stop, check, close, plus, folder, trash, wand, gpu, cpu, clock,
search, refresh, star, download, video, chart, back, forward, warning, info.

Páginas (`QWidget`, construtor `__init__(self, ctx: AppContext, parent=None)`, método
`refresh(self)` obrigatório; id de navegação em minúsculas):

| id | arquivo | classe |
|---|---|---|
| `dashboard` | pages/dashboard.py | `DashboardPage` |
| `import` | pages/import_page.py | `ImportPage` |
| `clips` | pages/clips_page.py | `ClipsPage` |
| `editor` | pages/editor_page.py | `EditorPage` |
| `export` | pages/export_page.py | `ExportPage` |
| `settings` | pages/settings_page.py | `SettingsPage` |

* Sidebar: 72 px colapsável para 220 px, botão ativo com barra de aento animada.
* `MainWindow` (`ui/main_window.py`): `QStackedWidget` + sidebar + topbar (nome do
  projeto, estado do pipeline, botão global "Cancelar"), `QStatusBar` com progresso,
  overlay de processamento não-modal, `ToastStack` no canto inferior direito,
  drag-and-drop global (`dragEnterEvent`/`dropEvent` → `ImportPage`), atalhos
  (Ctrl+O, Ctrl+S, Ctrl+I importar, Ctrl+G gerar cortes, Ctrl+E exportar, Ctrl+,  config,
  Esc cancelar), janela mínima 1120x700, salvamento de geometria em config.
* Widgets reutilizáveis em `ui/widgets/`: `sidebar.py` (`Sidebar`), `cards.py`
  (`StatCard`, `ProjectCard`, `ClipCard`, `HistoryRow`), `droparea.py` (`DropArea`),
  `player.py` (`VideoPlayer` com `QMediaPlayer`+`QVideoWidget`, play/pause, seek,
  velocidade, loop de faixa `set_loop_range(t0,t1)`), `timeline.py` (`Timeline`
  marcando cortes/legendas, arrastável, com handles de início/fim),
  `subtitle_overlay.py` (`SubtitlePreview` renderizando o estilo ASS em tempo real com
  QPainter), `progress.py` (`StageProgress` lista de estágios com %), `toast.py`
  (`ToastStack`), `switch.py` (`SwitchButton`), `segmented.py` (`SegmentedControl`),
  `busy.py` (`BusyOverlay`).
* `ClipCard`: thumbnail real (`media.thumbnail`), duração, início→fim no original,
  título/descrição da IA, barra de score 0–100 com tooltip mostrando `signals`,
  botões aprovar/recusar/editar/exportar, checkbox de seleção.
* Editor: player + timeline + campos de início/fim (aceitam `mm:ss`), aba de legendas
  (lista editável de segmentos que grava em `clip.edit.manual_text_edits`),
  aba de estilo (todos os campos de `SubtitleSettings` + preview ao vivo),
  aba de enquadramento (aspecto, zoom, offsets, preview do recorte),
  aba de áudio (volume, música, ducking), aba de identidade (marca d'água, título).
  Tudo escreve em `clip.edit` e marca `clip.edit` → `ctx.save()`.
* Nunca congelar a UI: trabalho em `corteia/workers/jobs.py`
  (`FunctionJob(QRunnable)` + `JobRunner(QObject)` com sinais
  `progress/finished/failed`, fila com `QThreadPool` e cancelamento por `Event`).
* Páginas importam **somente** `corteia.core.*` através de `ctx.pipeline` / `ctx.exporter`.
  Assinaturas em `core/pipeline.py`: `Pipeline.analyze(source_path=None, options=None)`,
  `.cancel()`, `.running`, `.stages()`; `Exporter.export_all()`, `.export_one(clip_id)`,
  `.cancel()`, `.queue`.

## Empacotamento (raiz)

`run.py`, `install.sh`, `install.bat`, `run.bat`, `build_windows.bat`, `build_linux.sh`,
`installer/corteia.iss` (Inno Setup), `corteia.spec` (PyInstaller), `requirements.txt`,
`README.md` (PT-BR, com passo a passo de instalação, primeira execução, GPU/CPU,
limitações honestas), `assets/icon.svg|png|ico`.

## Testes (`tests/`)

`pytest`, sem network. Gerar vídeo sintético **real** com FFmpeg (cor + tom + fala não
necessária) em `tests/conftest.py::sample_video`. Cobrir: probe, silencedetect, scenes,
plan_crop (limites de crop), generate ASS (tags válidas), boundaries do analyze,
encode_cmd, round-trip Project JSON, e um **e2e** que renderiza um clipe de 4 s e
verifica duração/resolução com ffprobe.
