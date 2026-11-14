"""Persistent settings (JSON in the app data dir) with change notifications.

Kept deliberately dependency-free so the processing core can read it without Qt.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any, Callable

from . import paths

# key -> default. Types are inferred from the default value on load.
DEFAULTS: dict[str, Any] = {
    # --- interface -------------------------------------------------------
    "ui.language": "pt",                 # pt | en
    "ui.theme": "dark",                  # dark | light
    "ui.accent": "#7C5CFF",
    "ui.first_run": True,
    "ui.window_w": 1360,
    "ui.window_x": -1,
    "ui.window_y": -1,
    "ui.window_h": 820,
    # --- subtitles -------------------------------------------------------
    "subs.language": "pt",               # language used for ASR
    "subs.default_style": "viral",
    "subs.model": "base",                # tiny|base|small|medium|large-v3|distil-large-v3
    "subs.beam_size": 5,
    "subs.word_timestamps": True,
    "subs.uppercase": True,
    "subs.font": "",
    "subs.size": 42,
    "subs.outline": 4,
    "subs.position": "bottom",
    "subs.reading_speed_cps": 17.0,
    "subs.diarize": False,
    # --- analysis --------------------------------------------------------
    "analysis.duration_preset": 60,
    "analysis.min_seconds": 15.0,
    "analysis.max_seconds": 60.0
    ,
    "analysis.max_clips": 12,
    "analysis.min_score": 25.0,
    "analysis.remove_silences": True,
    "analysis.smart_boundaries": True,
    "analysis.follow_speaker": True,
    "analysis.aspect": "9:16",
    # --- processing ------------------------------------------------------
    "proc.device": "auto",               # auto | cpu | cuda
    "proc.compute_type": "auto",         # auto | float32 | float16 | int8
    "proc.cpu_threads": 0,               # 0 = auto
    "proc.max_parallel_jobs": 2,
    "proc.sample_fps_face": 6.0,
    "proc.scene_threshold": 27.0,
    "proc.silence_db": -35.0,
    "proc.silence_min_ms": 350,
    "proc.temp_limit_gb": 12.0,
    "proc.scene_min_len": 15,
    #: margem de respiração mantida ao descartar silêncio (segundos) e o
    #: menor trecho conservado; o núcleo lê as duas
    "silence.pad": 0.06,
    "silence.min_keep": 0.35,
    # --- export ----------------------------------------------------------
    "export.dir": str(paths.exports_dir_default()),
    "export.codec": "libx264",
    "export.resolution": "1080p",
    "export.fps": 30,
    "export.quality_crf": 20,
    "export.burn_subtitles": True,
    "export.preset": "fast",
    "export.audio_bitrate": "192k",
    "export.pattern": "{index:02d}-{title}-corteia",
    # --- external APIs ---------------------------------------------------
    "api.provider": "off",               # off | openai | ollama | custom
    "api.base_url": "https://api.openai.com/v1",
    "api.key": "",
    "api.model": "gpt-4o-mini",
    "api.temperature": 0.3,
    "api.max_output_tokens": 4000,
    "api.timeout": 60,
    "api.offline_fallback": True,        # use the local scorer if the API fails
    # --- misc ------------------------------------------------------------
    "net.download_consent": False,
    #: caminho explícito dos binários, quando não estão no PATH
    "ffmpeg.path": "",
    "ffprobe.path": "",       # must be granted before yt-dlp is used
    "misc.check_updates": False,
    "misc.telemetry": False,
}

_SENSITIVE = {"api.key"}


class Config:
    """Thread-safe settings store."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or (paths.data_dir() / "config.json")
        self._lock = threading.RLock()
        self._listeners: list[Callable[[str, Any], None]] = []
        self._data: dict[str, Any] = {}
        self.load()

    # ------------------------------------------------------------------ io
    def load(self) -> None:
        with self._lock:
            data = dict(DEFAULTS)
            if self._path.exists():
                try:
                    raw = json.loads(self._path.read_text(encoding="utf-8"))
                    if isinstance(raw, dict):
                        for k, v in raw.items():
                            data[k] = _coerce(k, v)
                except (OSError, json.JSONDecodeError):
                    pass        # corrupt file -> defaults, never crash the app
            self._data = data

    def save(self) -> None:
        with self._lock:
            # Config(path=...) apontando para um diretório que ainda não
            # existe é o padrão dos testes e do --smoke: sem o mkdir abaixo o
            # primeiro save() dava FileNotFoundError.
            try:
                self._path.parent.mkdir(parents=True, exist_ok=True)
            except OSError:
                pass
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._data, indent=2, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self._path)

    def reset(self) -> None:
        with self._lock:
            self._data = dict(DEFAULTS)
            self.save()

    # --------------------------------------------------------------- access
    def get(self, key: str, fallback: Any = None) -> Any:
        with self._lock:
            if key in self._data:
                return self._data[key]
            return DEFAULTS.get(key, fallback)

    def set(self, key: str, value: Any, *, save: bool = True) -> None:
        with self._lock:
            value = _coerce(key, value)
            old = self._data.get(key)
            if old == value:
                return
            self._data[key] = value
            if save:
                self.save()
        for fn in list(self._listeners):
            try:
                fn(key, value)
            except Exception:                                  # noqa: BLE001
                pass

    def update(self, mapping: dict[str, Any], *, save: bool = True) -> None:
        for k, v in mapping.items():
            self.set(k, v, save=save)
        if save:
            self.save()

    def as_dict(self, *, redact: bool = False) -> dict[str, Any]:
        with self._lock:
            out = dict(self._data)
        if redact:
            for k in _SENSITIVE:
                if out.get(k):
                    out[k] = "•" * 8 + str(out[k])[-4:]
        return out

    def changed(self, fn: Callable[[str, Any], None]) -> None:
        self._listeners.append(fn)

    def on(self, *keys: str) -> Callable[[Callable[[str, Any], None]], Callable]:
        """Decorator form of :meth:`changed` filtered to specific keys."""

        def deco(fn: Callable[[str, Any], None]) -> Callable[[str, Any], None]:
            def wrapper(key: str, value: Any) -> None:
                if not keys or key in keys:
                    fn(key, value)
            self.changed(wrapper)
            return fn
        return deco

    # ------------------------------------------------------------ typed get
    # ``default`` is honoured even when the key is unknown to DEFAULTS: several
    # call sites pass a key that only exists in a newer build, and silently
    # returning 0/"" there hides bugs (a 0-thread FFmpeg, a 0-second fade).
    def str_(self, k: str, default: str = "") -> str:
        v = self.get(k, None)
        if v is None or (isinstance(v, str) and not v.strip()):
            return default
        return str(v)

    def int_(self, k: str, default: int = 0) -> int:
        try:
            v = self.get(k, None)
            return default if v is None else int(v)
        except (TypeError, ValueError):
            return default

    def float_(self, k: str, default: float = 0.0) -> float:
        try:
            v = self.get(k, None)
            return default if v is None else float(v)
        except (TypeError, ValueError):
            return default

    def bool_(self, k: str, default: bool = False) -> bool:
        v = self.get(k, None)
        if v is None:
            return default
        if isinstance(v, str):
            return v.strip().lower() in ("1", "true", "sim", "on", "yes")
        return bool(v)

    @property
    def path(self) -> Path:
        return self._path


def _coerce(key: str, value: Any) -> Any:
    default = DEFAULTS.get(key)
    if isinstance(default, bool):
        if isinstance(value, str):
            return value.lower() in ("1", "true", "sim", "on")
        return bool(value)
    if isinstance(default, int) and not isinstance(default, bool):
        if isinstance(value, bool):
            return int(value)
        try:
            return int(value)
        except (TypeError, ValueError):
            return default
    if isinstance(default, float):
        try:
            return float(value)
        except (TypeError, ValueError):
            return default
    if isinstance(default, str) and not isinstance(value, str):
        return "" if value is None else str(value)
    return value


_config: Config | None = None


def get_config() -> Config:
    global _config
    if _config is None:
        _config = Config()
    return _config
