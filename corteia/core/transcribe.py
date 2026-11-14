"""Automatic speech recognition (Whisper) for CorteIA Studio.

The whole module is built around one rule: **never load a full video into RAM**.
``faster-whisper`` decodes audio in streaming through PyAV when it receives a
*path*, so :func:`transcribe` always hands the file path over and lets the
decoder pull one 30 s window at a time. Diarisation goes one step further and
seeks FFmpeg per segment (``-ss`` before ``-i``), so a two hour video costs the
same memory as a 30 s one.

Contract notes
--------------
* Progress callbacks receive ``0..100`` (percent), exactly as the pipeline
  ``StageProgress`` widget expects: ``on_progress(segment.end / duration * 100,
  "transcrevendo ...")``.
* ``cancel`` is a :class:`threading.Event`, checked for every decoded segment,
  every downloaded file and every FFmpeg chunk; it raises
  :class:`~corteia.core.errors.CancelledError`.
* Failures are :class:`TranscriptionError` / :class:`ModelMissingError` with a
  Portuguese ``message`` and the technical text in ``detail``/``hint``.
* Only one model stays warm in memory (LRU of size 1, see :func:`unload_model`).
"""

from __future__ import annotations

import gc
import hashlib
import json
import logging
import os
import re
import subprocess
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

import numpy as np

from .. import paths
from ..config import get_config
from ..hardware import ctranslate2_device
from ..models import Segment, Transcript, Word, format_ts
from .errors import (
    CancelledError,
    ModelMissingError,
    ProcessingError,
    TranscriptionError,
)

log = logging.getLogger("corteia")

ProgressFn = Callable[[float, str], None]

# --------------------------------------------------------------------------- #
# model catalogue
# --------------------------------------------------------------------------- #
#: Local Whisper models usable by CTranslate2. ``mb`` is the real download size
#: measured from the Hugging Face Hub tree (``model.bin`` + tokenizer + config);
#: ``peak_mb`` is the rough RAM footprint once loaded as ``int8``.
MODELS: dict[str, dict[str, Any]] = {
    "tiny": {
        "repo": "Systran/faster-whisper-tiny",
        "mb": 78,
        "peak_mb": 380,
        "desc": "tiny — 78 MB · o mais rápido, ideal para rascunhos e testes",
    },
    "base": {
        "repo": "Systran/faster-whisper-base",
        "mb": 148,
        "peak_mb": 620,
        "desc": "base — 148 MB · melhor custo/benefício em CPU (padrão)",
    },
    "small": {
        "repo": "Systran/faster-whisper-small",
        "mb": 486,
        "peak_mb": 1500,
        "desc": "small — 486 MB · precisão bem maior, cerca de 3× mais lento",
    },
    "medium": {
        "repo": "Systran/faster-whisper-medium",
        "mb": 1531,
        "peak_mb": 3600,
        "desc": "medium — 1,5 GB · exige 16 GB de RAM ou placa de vídeo",
    },
    "large-v3": {
        "repo": "Systran/faster-whisper-large-v3",
        "mb": 3091,
        "peak_mb": 6200,
        "desc": "large-v3 — 3,1 GB · máxima precisão, recomendado com GPU",
    },
    "distil-large-v3": {
        "repo": "Systran/faster-distil-whisper-large-v3",
        "mb": 1517,
        "peak_mb": 3200,
        "desc": "distil-large-v3 — 1,5 GB · quase o large-v3 por bem menos CPU",
    },
    "auto": {
        "repo": "",
        "mb": 0,
        "peak_mb": 0,
        "desc": "auto — escolhe pelo total de RAM e pela duração do vídeo",
        "auto": True,
    },
}

#: Web page of every local model, shown in the settings page.
MODEL_URLS: dict[str, str] = {
    k: (f"https://huggingface.co/{v['repo']}" if v["repo"] else "")
    for k, v in MODELS.items()
}

#: Model names that only exist behind a paid API. They are deliberately *not*
#: in :data:`MODELS` (there is nothing to download) but are recognised so the
#: user gets a clear Portuguese message instead of a cryptic ``ValueError``.
CLOUD_MODELS: frozenset[str] = frozenset({"whisper-1", "gpt-4o-transcribe"})

#: Files a CTranslate2 Whisper directory needs to be loadable offline.
REQUIRED_FILES: tuple[str, ...] = ("model.bin", "config.json", "tokenizer.json")

#: Files pulled from the hub (the same set faster-whisper uses internally).
ALLOW_PATTERNS: tuple[str, ...] = (
    "config.json",
    "preprocessor_config.json",
    "model.bin",
    "tokenizer.json",
    "tokenizer_config.json",
    "normalizer.json",
    "added_tokens.json",
    "vocabulary.txt",
    "vocabulary.json",
)

#: Aliases accepted from old project files / UI strings.
_ALIASES: dict[str, str] = {
    "large": "large-v3",
    "large-v1": "large-v3",
    "large-v2": "large-v3",
    "distil": "distil-large-v3",
    "whisper-tiny": "tiny",
    "faster-whisper-tiny": "tiny",
    "faster-whisper-base": "base",
}

#: Order used when the automatic choice has to step down for lack of RAM.
_TIER: tuple[str, ...] = ("tiny", "base", "small", "medium", "large-v3")

#: Whisper only punctuates reliably when the target orthography is stated;
#: without this hint fast speech comes out as one long run without commas.
DEFAULT_INITIAL_PROMPT = "Utilize pontuação correta em português do Brasil."

#: Whisper's ``initial_prompt`` is capped at 224 tokens (~440 chars).
MAX_PROMPT_CHARS = 430

#: Above this many characters of conditioning text ``condition_on_previous_text``
#: is switched off (see :func:`_build_prompt`).
LONG_CONDITION_CHARS = 220

_SAMPLE_RATE = 16000          # diarisation analysis rate
_FRAME = 1024                 # 64 ms
_HOP = 512                    # 32 ms
_F0_MIN, _F0_MAX = 70.0, 350.0

#: Audio decoded per pass: 300 s of 16 kHz float32 is ~19 MB of RAM.
BLOCK_SECONDS = 300.0


# --------------------------------------------------------------------------- #
# text helpers
# --------------------------------------------------------------------------- #
_WS_RE = re.compile(r"[ \t\r\n\u00a0\u2007\u202f]+")

#: Tokens that must *not* be preceded by a space when re-joining words.
_NO_SPACE_BEFORE = ".,;:!?%)]}»”’…·-–—"

#: Characters that must not be *followed* by a space (they open a group).
_NO_SPACE_AFTER = "([«“‘"


def _norm_text(text: str) -> str:
    """Collapse any whitespace run to a single ASCII space and trim the ends."""
    return _WS_RE.sub(" ", (text or "").replace("\ufffd", "")).strip()


def _clean_token(raw: str) -> str:
    """faster-whisper tokens carry their spacing (``" Olá"``) -> ``"Olá"``."""
    return _norm_text(raw).strip("\u00a0")


def join_words(words: Iterable[Word | str]) -> str:
    """Rebuild a readable string from normalised word tokens.

    ``faster-whisper`` stores the spacing inside each token (``" Olá"``); the
    :class:`~corteia.models.Word` list keeps only the bare token, so joining has
    to decide the spacing again. The rules, documented on purpose:

    * a space separates two ordinary tokens;
    * no space before a token starting with punctuation or a close bracket;
    * no space after an opening bracket or quote.

    Accepts :class:`~corteia.models.Word` objects or plain strings and keeps
    every punctuation mark the model produced.
    """
    out: list[str] = []
    for item in words:
        token = _clean_token(item if isinstance(item, str) else getattr(item, "text", ""))
        if not token:
            continue
        if out:
            glued = token[0] in _NO_SPACE_BEFORE or out[-1][-1] in _NO_SPACE_AFTER
            out.append("" if glued else " ")
        out.append(token)
    return "".join(out)


def words_text(segment: Segment) -> str:
    """Segment text rebuilt from the word timestamps (subtitle rendering)."""
    return join_words(segment.words)


# --------------------------------------------------------------------------- #
# model resolution
# --------------------------------------------------------------------------- #
def _ram_total_gb() -> float:
    try:
        import psutil

        return psutil.virtual_memory().total / 1024**3
    except Exception:                                       # noqa: BLE001
        return 0.0


def _ram_available_gb() -> float:
    try:
        import psutil

        return psutil.virtual_memory().available / 1024**3
    except Exception:                                       # noqa: BLE001
        try:
            for line in Path("/proc/meminfo").read_text().splitlines():
                if line.startswith(("MemAvailable:", "MemFree:")):
                    return int(line.split()[1]) / 1024**2
        except (OSError, ValueError):
            pass
        return 0.0


# --------------------------------------------------------------------------- #
# orçamento de memória
# --------------------------------------------------------------------------- #
#: fração da VRAM que o app aceita ocupar com um modelo; o resto fica para o
#: decoder de vídeo e para qualquer outra coisa aberta
_GPU_BUDGET = 0.85
#: fração da RAM livre aceita no caminho de CPU (com folga para o resto)
_RAM_BUDGET = 0.70


def _vram_total_gb() -> float:
    """VRAM da placa NVIDIA, ou 0.0 quando não há GPU detectável."""
    try:
        from ..hardware import probe

        return float(probe().gpu_vram_gb or 0.0)
    except Exception:                                       # noqa: BLE001
        return 0.0


def footprint_gb(name: str, compute: str = "int8") -> float:
    """RAM/VRAM que o modelo ocupa carregado, em GB.

    ``peak_mb`` do catálogo é o pico em ``int8`` (1 byte por peso).  Em
    ``float16`` são 2 bytes, então o mesmo modelo pede quase o dobro -- é por
    isso que um ``medium`` cabe em 16 GB de RAM mas não nos 6 GB de VRAM de uma
    RTX 3050.
    """
    mb = float(MODELS.get(name, {}).get("peak_mb") or 0)
    if mb <= 0:
        return 0.0
    factor = 1.9 if compute in ("float16", "float32") else 1.0
    return (mb * factor) / 1024.0


def memory_budget(device: str, compute: str = "int8") -> tuple[float, str]:
    """``(limite_em_GB, explicação)`` para o par dispositivo/tipo de cálculo."""
    if device == "cuda":
        vram = _vram_total_gb()
        if vram <= 0:
            return 0.0, "sem GPU CUDA detectável"
        return vram * _GPU_BUDGET, f"{vram:.1f} GB de VRAM × {int(_GPU_BUDGET * 100)}%"
    avail = _ram_available_gb() or _ram_total_gb()
    if avail <= 0:
        return 0.0, "RAM não pôde ser medida"
    return avail * _RAM_BUDGET, f"{avail:.1f} GB de RAM livre × {int(_RAM_BUDGET * 100)}%"


def largest_fitting(device: str = "cpu", compute: str = "int8") -> str:
    """Maior nome do catálogo que cabe no orçamento de ``device``."""
    budget, _ = memory_budget(device, compute)
    best = "tiny"
    for name in _TIER:
        if footprint_gb(name, compute) <= max(0.3, budget):
            best = name
    return best


def check_fit(name: str, device: str = "auto",
              compute: str = "auto") -> tuple[bool, str]:
    """Diz se ``name`` roda neste hardware, e por quê não quando não roda.

    Usado pela interface, pelo ``--check`` e pelo carregador antes de estourar
    memória.
    """
    from ..hardware import ctranslate2_device

    if device in ("", "auto") or compute in ("", "auto"):
        try:
            auto_dev, auto_ct = ctranslate2_device()
        except Exception:                                   # noqa: BLE001
            auto_dev, auto_ct = "cpu", "int8"
        device = device if device not in ("", "auto") else auto_dev
        compute = compute if compute not in ("", "auto") else auto_ct
    if name not in MODELS or name == "auto":
        return True, ""
    need = footprint_gb(name, compute)
    budget, why = memory_budget(device, compute)
    if need <= max(0.3, budget):
        return True, f"{name} em {device}/{compute}: {need:.1f} GB de {budget:.1f} GB ({why})"
    fits = largest_fitting(device, compute)
    extra = ""
    if device == "cuda":
        b_cpu, _w = memory_budget("cpu", "int8")
        if need <= max(0.3, b_cpu):
            extra = f"; em CPU/int8 caberia ({b_cpu:.1f} GB livres)"
    return False, (f"{name} em {device}/{compute} pede ~{need:.1f} GB e há só "
                   f"{budget:.1f} GB ({why}). O maior que cabe aí é '{fits}'{extra}.")


def normalize_name(model: str) -> str:
    """Canonical catalogue key for anything the UI or a config file holds."""
    name = (model or "base").strip().lower().replace("_", "-")
    if name in MODELS:
        return name
    if name in _ALIASES:
        return _ALIASES[name]
    for key in MODELS:                                      # 'large v3', 'largev3'
        if name in (key.replace("-", " "), key.replace("-", "")):
            return key
    return name


def resolve_model(model: str = "auto", audio_seconds: float | None = None,
                  ram_gb: float | None = None) -> str:
    """Concrete catalogue key for ``model`` (``"auto"`` is expanded).

    The ``auto`` policy, documented and deterministic:

    1. tier by **total RAM**: below 8 GB -> ``base``, 8 to 16 GB -> ``small``,
       above 16 GB -> ``medium``;
    2. long sources are penalised: more than 60 min of audio steps **one tier
       down** (``small`` -> ``base``) so a batch does not run all night;
    3. in any case the choice is stepped down while the model's int8 footprint
       does not fit the RAM *currently available* (never below ``tiny``).

    ``large-v3``/``distil-large-v3`` are never picked automatically: they are
    the user's explicit choice, otherwise a 3 GB download would surprise people.

    Raises :class:`TranscriptionError` for unknown names and explains cloud-only
    names such as ``whisper-1``.
    """
    name = normalize_name(model)
    if name in CLOUD_MODELS:
        raise TranscriptionError(
            "transcribe",
            f"'{model}' é um modelo de nuvem e exige chave de API. Escolha um modelo "
            "local: tiny, base, small, medium, large-v3 ou distil-large-v3.",
        )
    if name not in MODELS:
        raise TranscriptionError(
            "transcribe",
            f"Modelo de transcrição desconhecido: '{model}'.",
            "Opções: " + ", ".join(sorted(k for k in MODELS if k != "auto")) + ", auto.",
        )
    if not MODELS[name].get("auto"):
        return name

    total = ram_gb if ram_gb is not None else _ram_total_gb()
    if total <= 0:
        total = 8.0                                         # unknown -> conservative
    chosen = "base" if total < 8.0 else ("small" if total <= 16.0 else "medium")

    if audio_seconds and audio_seconds > 3600.0:
        chosen = _TIER[max(0, _TIER.index(chosen) - 1)]

    avail = _ram_available_gb() or total
    while (_TIER.index(chosen) > 0
           and MODELS[chosen]["peak_mb"] / 1024.0 > max(1.0, avail * _RAM_BUDGET)):
        chosen = _TIER[_TIER.index(chosen) - 1]

    # GPU: float16 dobra a pegada.  Uma placa de 6 GB de VRAM não segura
    # "medium" em float16, então o auto desce um degrau aqui em vez de deixar
    # o carregador estourar e cair para CPU no susto.
    try:
        from ..hardware import ctranslate2_device

        dev, ct = ctranslate2_device()
    except Exception:                                       # noqa: BLE001
        dev, ct = "cpu", "int8"
    if dev == "cuda":
        budget, _why = memory_budget("cuda", ct)
        while _TIER.index(chosen) > 0 and footprint_gb(chosen, ct) > max(0.3, budget):
            chosen = _TIER[_TIER.index(chosen) - 1]
    log.debug("auto model: ram=%.1f GB avail=%.1f GB audio=%.0f s -> %s",
              total, avail, audio_seconds or 0.0, chosen)
    return chosen


def model_dir(model: str) -> Path:
    """Directory that holds the CTranslate2 files of ``model`` (may not exist)."""
    name = normalize_name(model)
    if name not in MODELS:
        raise TranscriptionError("transcribe", f"Modelo de transcrição desconhecido: '{model}'.")
    if name == "auto":
        name = resolve_model("auto")
    return paths.models_dir() / name


def is_downloaded(model: str) -> bool:
    """True when every file needed to load ``model`` offline is on disk."""
    try:
        d = model_dir(model)
    except TranscriptionError:
        return False
    if not d.is_dir():
        return False
    for fname in REQUIRED_FILES:
        if not (d / fname).is_file():
            return False
    try:
        return (d / "model.bin").stat().st_size > 1_000_000
    except OSError:
        return False


def model_size_on_disk(model: str) -> int:
    """Bytes a downloaded model really occupies (0 when absent)."""
    return paths.dir_size(model_dir(model)) if is_downloaded(model) else 0


# --------------------------------------------------------------------------- #
# downloading
# --------------------------------------------------------------------------- #
_NET_TOKENS = ("connection", "timed out", "timeout", "unauthorized", "resolve",
               "network", "ssl", "401", "403", "404", "temporarily unavailable")


def _looks_offline(exc: BaseException) -> bool:
    return any(t in f"{type(exc).__name__}: {exc}".lower() for t in _NET_TOKENS)


class _ByteProgress:
    """Accumulator shared by the per-file downloads -> percent of total bytes."""

    __slots__ = ("total", "done", "callback", "last_pct", "current_total", "current_done")

    def __init__(self, total: float, callback: ProgressFn | None) -> None:
        self.total = max(1.0, float(total))
        self.done = 0.0
        self.callback = callback
        self.last_pct = -1.0
        self.current_total = 0.0
        self.current_done = 0.0

    def emit(self) -> None:
        if self.callback is None:
            return
        pct = min(99.5, 100.0 * (self.done + self.current_done) / self.total)
        if abs(pct - self.last_pct) < 0.4:
            return
        self.last_pct = pct
        got = self.done + self.current_done
        msg = f"baixando modelo — {paths.human_bytes(got)} de {paths.human_bytes(self.total)}"
        try:
            self.callback(pct, msg)
        except Exception:                                   # noqa: BLE001
            log.exception("on_progress callback failed during download")

    def file_finished(self) -> None:
        self.done += self.current_total or self.current_done
        self.current_done = 0.0
        self.current_total = 0.0
        self.emit()


def _make_tqdm_class(tracker: _ByteProgress, cancel: threading.Event | None):
    """A tqdm subclass handed to huggingface_hub as ``tqdm_class``.

    HF drives it with byte counts, which is exactly what the UI progress bar
    needs; it also turns the user's cancel flag into an exception in the middle
    of the transfer instead of waiting for the file to finish.
    """
    from tqdm import tqdm

    class _HubTqdm(tqdm):
        def update(self, n=1):                               # noqa: D102
            if cancel is not None and cancel.is_set():
                raise CancelledError()                      # aborts the transfer
            ret = super().update(n)
            tracker.current_done = float(self.n or 0.0)
            if self.total:
                tracker.current_total = float(self.total)
            tracker.emit()
            return ret

    return _HubTqdm


def _list_remote_files(repo_id: str) -> list[tuple[str, int]]:
    """``(filename, bytes)`` for every needed file of a hub repository."""
    from huggingface_hub import model_info

    info = model_info(repo_id, files_metadata=True)
    out: list[tuple[str, int]] = []
    for sib in info.siblings or []:
        fname = sib.rfilename
        if fname in ALLOW_PATTERNS or fname.startswith("vocabulary"):
            out.append((fname, int(sib.size or 0)))
    if not any(f == "model.bin" for f, _ in out):
        raise TranscriptionError(
            "model", f"O repositório '{repo_id}' não contém um modelo CTranslate2.",
        )
    return out


def download_model(model: str, *, on_progress: ProgressFn | None = None,
                   cancel: threading.Event | None = None) -> Path:
    """Download ``model`` into ``models_dir()/<name>`` and return that directory.

    :mod:`huggingface_hub` is driven directly with ``local_dir`` so the app
    always knows where the files are — that is what makes ``is_downloaded()``
    and offline use work, and what lets :func:`transcribe` load with
    ``local_files_only``. Already complete directories are left untouched, and
    interrupted transfers are safe to retry because HF writes to temporary
    ``.incomplete`` files and only publishes a file when it is whole.
    """
    _check_cancel(cancel)
    name = resolve_model(model)
    dest = model_dir(name)
    repo = MODELS[name]["repo"]
    if is_downloaded(name):
        _emit(on_progress, 100.0, f"modelo {name} já está instalado")
        return dest
    try:
        from huggingface_hub import hf_hub_download, snapshot_download
    except Exception as exc:                                # noqa: BLE001
        raise TranscriptionError(
            "model", "A biblioteca de download do Hugging Face não está disponível.",
            f"{type(exc).__name__}: {exc}",
        ) from exc

    dest.mkdir(parents=True, exist_ok=True)
    files: list[tuple[str, int]] = []
    try:
        files = _list_remote_files(repo)
    except TranscriptionError:
        raise
    except Exception as exc:                                # noqa: BLE001
        if _looks_offline(exc):
            raise ModelMissingError(
                "model",
                f"O modelo '{name}' não está instalado e a internet não respondeu.",
                f"{type(exc).__name__}: {exc}",
                hint="Conecte-se à internet e baixe o modelo em Configurações → "
                     "Modelos; depois disso a transcrição funciona 100% offline.",
            ) from exc
        log.debug("model_info failed for %s (%s); falling back to snapshot_download", repo, exc)

    tracker = _ByteProgress(sum(max(1, s) for _, s in files) or 1.0, on_progress)
    log.info("downloading whisper model %s from %s to %s", name, repo, dest)
    try:
        if files:
            for fname, size in files:
                _check_cancel(cancel)
                tracker.current_total = float(size or 0)
                tracker.current_done = 0.0
                hf_hub_download(repo_id=repo, filename=fname, local_dir=str(dest),
                                tqdm_class=_make_tqdm_class(tracker, cancel))
                tracker.file_finished()
        else:
            snapshot_download(repo_id=repo, local_dir=str(dest),
                              allow_patterns=list(ALLOW_PATTERNS),
                              tqdm_class=_make_tqdm_class(tracker, cancel))
            tracker.done, tracker.current_done = tracker.total, 0.0
            tracker.emit()
    except CancelledError:
        log.info("model download cancelled (%s)", name)
        raise
    except Exception as exc:                                # noqa: BLE001
        if isinstance(exc, (ModelMissingError, TranscriptionError)):
            raise
        offline = _looks_offline(exc) or type(exc).__name__ in (
            "LocalEntryNotFoundError", "EntryNotFoundError", "OfflineModeIsEnabled",
            "GatedRepoError", "RepositoryNotFoundError",
        )
        if offline:
            raise ModelMissingError(
                "model",
                f"O modelo '{name}' não está instalado e não foi possível baixá-lo agora.",
                f"{type(exc).__name__}: {exc}",
                hint="Verifique a conexão e baixe em Configurações → Modelos ("
                     f"{MODEL_URLS.get(name, 'huggingface.co')}). Depois disso o "
                     "programa funciona sem internet.",
            ) from exc
        raise TranscriptionError(
            "model", f"Falha ao baixar o modelo '{name}'.",
            f"{type(exc).__name__}: {exc}",
        ) from exc

    if not is_downloaded(name):
        raise ModelMissingError(
            "model", f"O download do modelo '{name}' terminou incompleto.",
            f"Faltam arquivos em {dest}",
            hint="Apague a pasta do modelo e baixe novamente em Configurações → Modelos.",
        )
    _emit(on_progress, 100.0,
          f"modelo {name} pronto ({paths.human_bytes(model_size_on_disk(name))})")
    return dest


# --------------------------------------------------------------------------- #
# model loading (LRU of exactly one model)
# --------------------------------------------------------------------------- #
_FW: Any = None
_FW_LOCK = threading.Lock()


def _faster_whisper():
    """Import faster-whisper lazily: it costs ~1 s and the UI must start fast."""
    global _FW
    if _FW is None:
        with _FW_LOCK:
            if _FW is None:
                try:
                    import faster_whisper
                except Exception as exc:                    # noqa: BLE001
                    raise TranscriptionError(
                        "model",
                        "O motor de transcrição (faster-whisper) não está disponível.",
                        f"{type(exc).__name__}: {exc}",
                    ) from exc
                _FW = faster_whisper
    return _FW


class _Holder:
    """Keeps one WhisperModel alive and releases it deterministically."""

    __slots__ = ("key", "model", "threads", "device", "compute")

    def __init__(self, key: tuple, model: Any, device: str, compute: str, threads: int) -> None:
        self.key = key
        self.model = model
        self.device = device
        self.compute = compute
        self.threads = threads

    def close(self) -> None:
        self.model = None

    def __del__(self) -> None:                              # pragma: no cover
        self.close()


_CACHE: "OrderedDict[tuple, _Holder]" = OrderedDict()
_CACHE_LOCK = threading.RLock()
_CACHE_MAX = 1                                              # never keep more than one


#: motivo de um downgrade de memória decidido no último carregamento; a UI
#: mostra isto em vez de deixar o usuário adivinhar por que o modelo mudou
_last_memory = threading.local()


def memory_note() -> str:
    """Último aviso de orçamento de memória daquela thread, ou vazio."""
    return getattr(_last_memory, "note", "") or ""


def _set_memory_note(text: str) -> None:
    _last_memory.note = text


def _cache_key(device: str, compute: str, threads: int, name: str) -> tuple:
    return (name, device, compute, int(threads))


def _cpu_threads(override: int | None = None) -> int:
    """Thread count for ctranslate2: config value, else one less than the cores."""
    cfg = get_config()
    n = int(override if override is not None else cfg.int_("proc.cpu_threads") or 0)
    if n > 0:
        return n
    try:
        import psutil

        n = psutil.cpu_count(logical=True) or os.cpu_count() or 4
    except Exception:                                       # noqa: BLE001
        n = os.cpu_count() or 4
    return max(1, int(n) - (1 if int(n) > 2 else 0))


def _get_model(name: str, device: str, compute: str, threads: int):
    """Load (or reuse) a WhisperModel from the local directory, LRU of one."""
    key = _cache_key(device, compute, threads, name)
    with _CACHE_LOCK:
        holder = _CACHE.get(key)
        if holder is not None and holder.model is not None:
            _CACHE.move_to_end(key)
            return holder.model, holder.device, holder.compute
        while _CACHE:                                       # LRU of one: free the rest
            _k, old = _CACHE.popitem(last=False)
            old.close()
        gc.collect()

    fw = _faster_whisper()
    directory = str(model_dir(name))
    if not is_downloaded(name):
        try:
            directory = str(download_model(name))
        except ModelMissingError:
            raise
    started = time.monotonic()
    # --- guarda de memória --------------------------------------------------
    # Um modelo que não cabe no dispositivo escolhido não é tentado: a tentativa
    # morria com um erro opaco do CTranslate2 (ou com o processo inteiro morto
    # pelo gerenciador de memória do Windows).  A decisão é tomada antes, com
    # números, e a alternativa honesta é usada -- registrada para a UI mostrar.
    _set_memory_note("")
    fits_here, why_here = check_fit(name, device, compute)
    if not fits_here:
        alt_dev, alt_ct = "cpu", "int8"
        fits_cpu, _w = check_fit(name, alt_dev, alt_ct)
        if fits_cpu:
            log.warning("memória: %s", why_here)
            _set_memory_note(why_here)
            device, compute = alt_dev, alt_ct
        else:
            menor = largest_fitting(alt_dev, alt_ct)
            raise TranscriptionError(
                "transcribe",
                f"O modelo '{name}' não cabe na memória desta máquina. {why_here}",
                f"Escolha '{menor}' (ou menor) em Configurações > Transcrição.",
            )
    # ``hardware.ctranslate2_device()`` reports CUDA whenever the FFmpeg build
    # exposes NVENC, and an old or missing driver then dies at load time. Retry
    # once on CPU/int8 rather than handing the user an error they cannot fix.
    attempts: list[tuple[str, str]] = [(device, compute)]
    if device == "cuda":
        attempts.append(("cpu", "int8"))
    holder: _Holder | None = None
    model = None
    last: Exception | None = None
    for dev_i, ct_i in attempts:
        try:
            model = fw.WhisperModel(
                directory,
                device=dev_i,
                compute_type=ct_i,
                cpu_threads=int(threads),
                num_workers=1,
                download_root=str(paths.models_dir()),
                local_files_only=is_downloaded(name),
            )
            device, compute = dev_i, ct_i
            break
        except MemoryError as exc:
            raise TranscriptionError(
                "transcribe",
                f"Memória RAM insuficiente para o modelo '{name}'. Use um modelo menor "
                "(base ou small).",
                str(exc),
            ) from exc
        except Exception as exc:                            # noqa: BLE001
            last = exc
            if (dev_i, ct_i) == attempts[-1]:
                break
            log.warning("whisper não conseguiu usar %s/%s (%s); tentando cpu/int8",
                        dev_i, ct_i, exc)
    if model is None:
        raise TranscriptionError(
            "transcribe",
            f"Não foi possível carregar o modelo '{name}'.",
            f"{type(last).__name__}: {last}",
        ) from last
    log.info("whisper %s loaded in %.2f s (device=%s compute=%s threads=%d)",
             name, time.monotonic() - started, device, compute, threads)
    with _CACHE_LOCK:
        holder = _Holder(key, model, device, compute, int(threads))
        _CACHE[key] = holder
        while len(_CACHE) > _CACHE_MAX:
            _k, old = _CACHE.popitem(last=False)
            old.close()
    return model, device, compute


def unload_model(model: str | None = None) -> int:
    """Release the warm Whisper model(s) from RAM. Returns how many were freed.

    ``model=None`` frees everything. Call it when the settings page closes or
    when a heavy FFmpeg render has to run without competing for memory.
    """
    freed = 0
    with _CACHE_LOCK:
        keys = list(_CACHE.keys())
        for k in keys:
            if model is None or normalize_name(model) == k[0]:
                _CACHE.pop(k).close()
                freed += 1
    gc.collect()
    if freed:
        log.info("whisper model(s) unloaded: %d", freed)
    return freed


def loaded_model() -> str:
    """Name of the model currently kept in RAM ("" when none)."""
    with _CACHE_LOCK:
        return next(iter(_CACHE))[0] if _CACHE else ""


# --------------------------------------------------------------------------- #
# transcript cache  (sha1 of path + mtime + model + language)
# --------------------------------------------------------------------------- #
CACHE_FORMAT = 3


def transcripts_dir() -> Path:
    return paths.cache_dir() / "transcripts"


def cache_key(path: Path | str, model: str, language: str) -> str:
    """Stable key: any change of the source file, model or language invalidates."""
    p = Path(path)
    try:
        st = p.stat()
        stamp = f"{st.st_mtime_ns}:{st.st_size}"
    except OSError:
        stamp = "0:0"
    raw = f"{os.path.abspath(str(p))}|{stamp}|{normalize_name(model)}|{(language or '').lower()}|f{CACHE_FORMAT}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:32]


def cache_file(path: Path | str, model: str, language: str) -> Path:
    return transcripts_dir() / f"{cache_key(path, model, language)}.json"


def _read_cache(path: Path) -> Transcript | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    data = raw.get("transcript") if isinstance(raw, dict) else None
    if not isinstance(data, dict) or raw.get("format") != CACHE_FORMAT:
        return None
    try:
        return Transcript.from_dict(data)
    except Exception:                                       # noqa: BLE001
        log.debug("bad transcript cache %s", path)
        return None


def _write_cache(path: Path, transcript: Transcript, *, source: Path,
                 audio_seconds: float) -> bool:
    payload = {
        "format": CACHE_FORMAT,
        "created_at": time.time(),
        "source": str(source),
        "audio_seconds": round(audio_seconds, 3),
        "model": transcript.model,
        "language": transcript.language,
        "transcript": transcript.to_dict(),
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
        return True
    except OSError as exc:
        log.warning("could not write transcript cache: %s", exc)
        return False


def clear_transcript_cache() -> int:
    """Delete every cached transcription. Returns bytes freed."""
    d = transcripts_dir()
    freed = paths.dir_size(d)
    if d.is_dir():
        for f in d.glob("*.json"):
            try:
                f.unlink()
            except OSError:
                pass
    log.info("transcript cache cleared (%d bytes)", freed)
    return freed


# --------------------------------------------------------------------------- #
# measured speed (perf.json) -> ETA
# --------------------------------------------------------------------------- #
#: Pessimistic fallback factors (wall time / audio time) for a 4-core CPU. Only
#: used when nothing has been measured on this machine yet.
_FALLBACK_RATIO: dict[str, float] = {
    "tiny": 0.55, "base": 1.0, "small": 2.4, "medium": 5.5,
    "large-v3": 12.0, "distil-large-v3": 5.0,
}


def perf_file() -> Path:
    return paths.cache_dir() / "perf.json"


def _perf_load() -> dict[str, Any]:
    try:
        data = json.loads(perf_file().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _perf_bucket(name: str, threads: int, device: str, compute: str) -> str:
    return f"{name}|{device}|{compute}|t{int(threads)}"


def record_speed(name: str, audio_seconds: float, wall_seconds: float, *,
                 threads: int, device: str = "cpu", compute: str = "int8") -> float:
    """Store the measured real-time factor as an exponential moving average."""
    if audio_seconds <= 0 or wall_seconds <= 0:
        return 0.0
    ratio = wall_seconds / audio_seconds
    data = _perf_load()
    models = data.setdefault("models", {})
    key = _perf_bucket(name, threads, device, compute)
    prev = models.get(key) or {}
    samples = int(prev.get("samples", 0)) + 1
    old = float(prev.get("ratio", 0.0))
    ema = ratio if samples == 1 else round(old * 0.4 + ratio * 0.6, 4)
    models[key] = {
        "ratio": ema, "samples": samples, "last_at": time.time(),
        "audio_seconds": round(audio_seconds, 2), "wall_seconds": round(wall_seconds, 2),
        "threads": int(threads), "device": device, "compute": compute,
        "model": name,
    }
    data["updated_at"] = time.time()
    try:
        perf_file().parent.mkdir(parents=True, exist_ok=True)
        perf_file().write_text(json.dumps(data, indent=1), encoding="utf-8")
    except OSError as exc:
        log.debug("perf.json not written: %s", exc)
    return ema


def measured_ratio(name: str, threads: int) -> tuple[float, str]:
    """(ratio, source) where source is ``"medido"`` or ``"estimado"``."""
    data = _perf_load()
    models = data.get("models") or {}
    exact = models.get(_perf_bucket(name, threads, "cpu", "int8"))
    if exact and float(exact.get("ratio", 0)) > 0:
        return float(exact["ratio"]), "medido"
    same_model = [v for k, v in models.items()
                  if str(k).split("|")[0] == name and float(v.get("ratio", 0)) > 0]
    if same_model:
        best = max(same_model, key=lambda v: int(v.get("samples", 1)))
        return float(best["ratio"]), "medido"
    base = _FALLBACK_RATIO.get(name, 1.0)
    factor = max(0.5, min(2.0, 4.0 / max(1, int(threads))))
    return base * factor, "estimado"


def estimate_duration_hours(model: str, audio_seconds: float, cpu_threads: int = 0) -> str:
    """ETA text in Portuguese for the UI, e.g. ``"≈ 1 h 12 min (estimativa)"``.

    Uses the real-time factor measured on *this* computer (moving average kept
    in ``cache_dir()/perf.json``). Without a measurement it falls back to a
    deliberately pessimistic published factor and says so in the string.
    """
    try:
        name = resolve_model(model, audio_seconds)
    except TranscriptionError:
        name = "base"
    threads = _cpu_threads(cpu_threads if cpu_threads else None)
    seconds = max(0.0, float(audio_seconds or 0.0))
    ratio, source = measured_ratio(name, threads)
    eta = seconds * ratio
    text = _format_eta(eta)
    if source == "medido":
        return f"{text} (média medida neste computador: {ratio:.2f}× o tempo real)"
    return f"{text} (estimativa conservadora; rode uma transcrição para calibrar)"


def _format_eta(seconds: float) -> str:
    total = int(round(seconds))
    if total < 60:
        return f"≈ {total} s"
    m, s = divmod(total, 60)
    if m < 60:
        return f"≈ {m} min {s:02d} s"
    h, m = divmod(m, 60)
    return f"≈ {h} h {m:02d} min"


# --------------------------------------------------------------------------- #
# transcription
# --------------------------------------------------------------------------- #
def _check_cancel(cancel: threading.Event | None) -> None:
    if cancel is not None and cancel.is_set():
        raise CancelledError()


def _emit(on_progress: ProgressFn | None, percent: float, message: str) -> None:
    if on_progress is None:
        return
    try:
        on_progress(float(max(0.0, min(100.0, percent))), str(message))
    except Exception:                                       # noqa: BLE001
        log.exception("on_progress callback failed")


def _probe_duration(path: Path) -> tuple[float, bool]:
    """``(duration_seconds, has_audio)`` through the shared FFmpeg executor."""
    cmd = ["ffprobe", "-v", "error", "-print_format", "json",
           "-show_format", "-show_streams", str(path)]
    data: dict | None = None
    try:
        from . import ffmpeg_cli

        data = ffmpeg_cli.run_json(cmd)
    except Exception as exc:                                # noqa: BLE001
        log.debug("ffmpeg_cli probe failed (%s); calling ffprobe directly", exc)
        try:
            from ..hardware import find_ffprobe

            proc = subprocess.run(
                [find_ffprobe() or "ffprobe", "-v", "error", "-print_format", "json",
                 "-show_format", "-show_streams", str(path)],
                capture_output=True, text=True, timeout=60,
            )
            data = json.loads(proc.stdout or "{}")
        except Exception:                                   # noqa: BLE001
            data = None
    if not isinstance(data, dict):
        return 0.0, False
    duration = 0.0
    try:
        duration = float((data.get("format") or {}).get("duration") or 0.0)
    except (TypeError, ValueError):
        duration = 0.0
    has_audio = False
    for s in data.get("streams") or []:
        if s.get("codec_type") == "audio":
            has_audio = True
            try:
                duration = max(duration, float(s.get("duration") or 0.0))
            except (TypeError, ValueError):
                pass
    return max(0.0, duration), has_audio


def _build_prompt(initial_prompt: str, condition_on: str) -> tuple[str, bool]:
    """``(initial_prompt, condition_on_previous_text)``.

    ``condition_on_previous_text`` is the single biggest source of Whisper's
    repetition loops: the previous segment is fed back into the decoder and on
    long or repetitive material the model locks onto its own output and emits
    the same sentence for minutes. It is therefore kept **on** only when the
    conditioning text is short (:data:`LONG_CONDITION_CHARS` chars); with a long
    ``condition_on`` it is switched **off** and the tail of that context goes
    into ``initial_prompt`` instead, which is a one-shot style hint that cannot
    loop. ``initial_prompt`` itself is capped near Whisper's 224-token limit.
    """
    base = _norm_text(initial_prompt) or DEFAULT_INITIAL_PROMPT
    context = _norm_text(condition_on)
    if not context:
        return base[:MAX_PROMPT_CHARS], True
    keep_previous = len(context) <= LONG_CONDITION_CHARS
    tail = context if keep_previous else context[-(MAX_PROMPT_CHARS // 2):]
    prompt = f"{base} {tail}"
    if len(prompt) > MAX_PROMPT_CHARS:
        prompt = prompt[-MAX_PROMPT_CHARS:]                 # keep the freshest words
    return prompt, keep_previous


def _words_from(fw_words: list) -> list[Word]:
    """faster-whisper words -> normalised :class:`corteia.models.Word` list."""
    out: list[Word] = []
    for w in fw_words or []:
        text = _clean_token(getattr(w, "word", "") or "")
        if not text:
            continue
        start = float(getattr(w, "start", 0.0) or 0.0)
        end = float(getattr(w, "end", start) or start)
        if end < start:
            start, end = end, start
        prob = float(getattr(w, "probability", 1.0) or 0.0)
        out.append(Word(round(start, 3), round(end, 3), text,
                        round(max(0.0, min(1.0, prob)), 3)))
    return out


def transcribe(
    path: Path,
    *,
    model: str = "base",
    language: str = "pt",
    beam_size: int = 5,
    word_timestamps: bool = True,
    vad_filter: bool = False,
    device: str = "auto",
    compute_type: str = "auto",
    on_progress: ProgressFn | None = None,
    cancel: threading.Event | None = None,
    condition_on: str = "",
    initial_prompt: str = "",
    reuse_cache: bool = True,
    store_cache: bool = True,
    cpu_threads: int = 0,
) -> Transcript:
    """Transcribe the audio of ``path`` with word-level timestamps.

    ``vad_filter`` defaults to **False** on purpose: silence trimming is the
    job of the ``silence``/``analyze`` stages, and letting Whisper drop silent
    spans here would bias the cut points. ``language`` may be ``""``/``"auto"``
    to let the model detect the spoken language. The file *path* is handed to
    faster-whisper, which decodes it as a stream through PyAV — no full WAV is
    ever loaded into RAM, which is what makes 2 h sources safe on a laptop.

    Results are cached in ``cache_dir()/transcripts`` keyed by
    sha1(source path + mtime + size + model + language); ``reuse_cache=False``
    forces a fresh pass. Progress is ``0..100`` (``segment.end / duration``).
    Raises :class:`TranscriptionError`, :class:`ModelMissingError` or
    :class:`CancelledError`.
    """
    _check_cancel(cancel)
    src = Path(path)
    if not src.is_file():
        raise TranscriptionError(
            "transcribe", f"O arquivo de vídeo não existe: {src.name}.", str(src),
        )

    name = resolve_model(model)
    lang = "" if (language or "").lower() in ("", "auto", "detect") else str(language)
    threads = _cpu_threads(cpu_threads or None)
    want_device, want_compute = (device or "auto"), (compute_type or "auto")
    if want_device == "auto" or want_compute == "auto":
        auto_dev, auto_ct = ctranslate2_device()
        want_device = auto_dev if want_device == "auto" else want_device
        want_compute = auto_ct if want_compute == "auto" else want_compute

    duration, has_audio = _probe_duration(src)
    if not has_audio:
        raise TranscriptionError(
            "transcribe",
            "Este arquivo não tem faixa de áudio, então não há o que transcrever.",
            str(src),
        )

    key_file = cache_file(src, name, lang)
    if reuse_cache and key_file.is_file():
        cached = _read_cache(key_file)
        if cached is not None:
            log.info("transcript cache hit %s (%d trecho(s))", key_file.name,
                     len(cached.segments))
            _emit(on_progress, 100.0, "transcrição reaproveitada do cache")
            return cached

    model_obj, used_device, used_compute = _get_model(
        name, want_device, want_compute, threads)
    prompt, cond_prev = _build_prompt(initial_prompt, condition_on)
    kwargs: dict[str, Any] = {
        "language": lang or None,
        "task": "transcribe",
        "beam_size": max(1, int(beam_size)),
        "word_timestamps": bool(word_timestamps),
        "vad_filter": bool(vad_filter),
        "condition_on_previous_text": cond_prev,
        "initial_prompt": prompt,
        "log_progress": False,
    }
    if vad_filter:
        kwargs["vad_parameters"] = {
            "threshold": 0.5, "min_speech_duration_ms": 200,
            "min_silence_duration_ms": 300, "speech_pad_ms": 120,
        }

    log.info("transcribing %s (%.1f s) with %s/%s on %s, %d thread(s), "
             "condition_on_previous_text=%s", src.name, duration, name, want_compute,
             want_device, threads, cond_prev)
    segments: list[Segment] = []
    detected = lang
    started = time.monotonic()
    try:
        # The audio is decoded with FFmpeg in BLOCK_SECONDS windows and handed to
        # faster-whisper as float32 numpy blocks. Two reasons, both real:
        # (a) given a path, faster-whisper decodes the WHOLE file into one array
        #     (2.3 GB/hour at 16 kHz float32) which breaks rule 1 of the
        #     architecture; (b) some PyAV builds reject the keyword
        #     ``metadata_errors`` that faster-whisper passes to ``av.open()``.
        # One block is about 19 MB, and blocks are decoded sequentially.
        block = float(BLOCK_SECONDS)
        total = duration or block
        pos = 0.0
        while pos < total - 0.05:
            _check_cancel(cancel)
            limit = min(pos + block, total) if duration else pos + block
            samples = extract_pcm(src, pos, limit, cancel=cancel, max_seconds=block + 2.0)
            if samples.size == 0:
                break
            iterator, info = model_obj.transcribe(samples, **kwargs)
            if not kwargs.get("language"):
                kwargs["language"] = getattr(info, "language", None) or "pt"
            detected = (getattr(info, "language", "") or detected) or "pt"
            for seg in iterator:
                _check_cancel(cancel)
                words = _words_from(list(getattr(seg, "words", None) or []))
                for w in words:
                    w.start = round(w.start + pos, 3)
                    w.end = round(w.end + pos, 3)
                text = _norm_text(getattr(seg, "text", "") or "") or join_words(words)
                start = round(float(getattr(seg, "start", 0.0) or 0.0) + pos, 3)
                end = round(float(getattr(seg, "end", 0.0) or 0.0) + pos, 3)
                if text:
                    segments.append(Segment(start, end, text, words, None))
                _emit(on_progress, 100.0 * min(end, total) / max(0.001, total),
                      f"transcrevendo {format_ts(end)} de {format_ts(total)} — "
                      f"{len(segments)} trecho(s)")
            pos = limit
            if not duration:                      # unknown length: stop after 1 block
                break
    except CancelledError:
        log.info("transcription cancelled after %.1f s", time.monotonic() - started)
        raise
    except MemoryError as exc:
        raise TranscriptionError(
            "transcribe",
            f"Faltou memória para transcrever com o modelo '{name}'. Escolha um "
            "modelo menor (base ou small) em Configurações.",
            str(exc),
        ) from exc
    except Exception as exc:                                # noqa: BLE001
        log.exception("whisper transcription failed")
        raise TranscriptionError(
            "transcribe",
            "A transcrição não pôde ser concluída. O arquivo pode estar corrompido "
            "ou sem áudio válido.",
            f"{type(exc).__name__}: {exc}",
        ) from exc

    wall = time.monotonic() - started
    audio_s = duration or (segments[-1].end if segments else 0.0)
    ema = record_speed(name, audio_s, wall, threads=threads,
                       device=used_device, compute=used_compute)
    transcript = Transcript(language=detected, model=name, segments=segments)
    if store_cache and audio_s > 0:
        _write_cache(key_file, transcript, source=src, audio_seconds=audio_s)
    log.info("transcribed %d segment(s) / %d word(s) in %.2f s (audio %.2f s, "
             "RTF %.3f, ema %.3f)", len(segments), len(transcript.words()), wall,
             audio_s, (wall / audio_s if audio_s else 0.0), ema)
    if not segments:
        log.warning("whisper returned no speech for %s", src.name)
    _emit(on_progress, 100.0, f"transcrição concluída — {len(segments)} trecho(s)")
    return transcript


def transcribe_is_cached(path: Path | str, model: str, language: str) -> bool:
    """True when a reusable transcription already exists for this file."""
    return cache_file(Path(path), resolve_model(model), language or "").is_file()


# --------------------------------------------------------------------------- #
# diarisation: real acoustic heuristics, no extra model
# --------------------------------------------------------------------------- #
@dataclass
class _VoiceFeatures:
    """Per-segment acoustic description used by the clustering."""

    index: int
    start: float
    end: float
    f0: float = 0.0            # median pitch in Hz (0.0 = unreliable)
    clarity: float = 0.0       # median autocorrelation peak, 0..1
    voiced: float = 0.0        # fraction of analysed frames that looked voiced
    rms_db: float = -120.0
    zcr: float = 0.0
    reliable: bool = False
    note: str = field(default="", compare=False)


def extract_pcm(path: Path, t0: float, t1: float, *, sr: int = _SAMPLE_RATE,
                cancel: threading.Event | None = None,
                max_seconds: float = 120.0) -> np.ndarray:
    """Decode ``[t0, t1]`` to mono float32 PCM without reading the whole file.

    ``-ss`` sits before ``-i`` (input seeking: the container jumps to the
    nearest keyframe and decodes only what is asked for), so pulling 8 s out of
    a 2 h video costs about 8 s of decoding. The payload leaves through
    ``pipe:1`` in 64 KiB chunks and never touches disk.
    """
    t0 = max(0.0, float(t0))
    t1 = max(t0, float(t1))
    length = min(max(0.05, t1 - t0), max_seconds)
    cmd = [
        "ffmpeg", "-ss", f"{t0:.3f}", "-t", f"{length:.3f}", "-i", str(path),
        "-vn", "-sn", "-dn", "-ac", "1", "-ar", str(int(sr)),
        "-f", "s16le", "pipe:1",
    ]
    try:
        from . import ffmpeg_cli

        proc = ffmpeg_cli.spawn_reader(cmd, cancel=cancel)
    except Exception as exc:                                # noqa: BLE001
        log.debug("ffmpeg_cli unavailable for pcm (%s); plain subprocess", exc)
        from ..hardware import find_ffmpeg

        proc = _RawReader(subprocess.Popen(
            [find_ffmpeg() or "ffmpeg"] + cmd[1:],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        ))

    # The pipe MUST be drained to EOF: stopping early leaves FFmpeg blocked on a
    # full stdout buffer (64 KiB) and the process never exits. `cap` only guards
    # against a runaway decoder.
    cap = (int(length * sr) + 2 * sr) * 2
    chunks: list[bytes] = []
    got = 0
    try:
        while True:
            _check_cancel(cancel)
            piece = proc.read(65536)
            if not piece:
                break
            chunks.append(piece)
            got += len(piece)
            if got > cap:
                proc.close(kill=True)
                log.debug("pcm capped at %d bytes for %.2f s of audio", got, length)
                break
        if proc.alive if hasattr(proc, "alive") else False:
            proc.wait(timeout=30.0)
    except CancelledError:
        proc.close(kill=True)
        raise
    except Exception as exc:                                # noqa: BLE001
        log.debug("pcm extract failed on %s [%.2f-%.2f]: %s", path.name, t0, t1, exc)
        return np.zeros(0, dtype=np.float32)
    if not chunks:
        return np.zeros(0, dtype=np.float32)
    raw = b"".join(chunks)
    if len(raw) % 2:
        raw = raw[:-1]
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0


class _RawReader:
    """Minimal stand-in for ``ffmpeg_cli.PipedProcess`` (fallback path only)."""

    __slots__ = ("p",)

    def __init__(self, proc: subprocess.Popen) -> None:
        self.p = proc

    def read(self, n: int = -1) -> bytes:
        return self.p.stdout.read(n) or b""

    def wait(self, timeout: float | None = None) -> int:
        return self.p.wait(timeout=timeout)

    def close(self, kill: bool = False) -> None:
        try:
            self.p.terminate()
        except OSError:
            pass


def _frame_signal(x: np.ndarray, frame: int = _FRAME, hop: int = _HOP) -> np.ndarray:
    """Split into overlapping frames as a strided view (no sample copy)."""
    if x.size < frame:
        x = np.concatenate([x, np.zeros(frame - x.size, dtype=x.dtype)])
    n = 1 + (x.size - frame) // hop
    return np.lib.stride_tricks.as_strided(
        x, shape=(n, frame), strides=(x.strides[0] * hop, x.strides[0]),
    )


def _f0_of_frame(frame: np.ndarray, sr: int, lag_min: int, lag_max: int) -> tuple[float, float]:
    """Autocorrelation pitch of one frame as ``(f0_hz, clarity)``.

    The peak is searched only inside the plausible voice range (70-350 Hz) and
    then halved while a half lag correlates just as well, which removes the
    classic octave error of locking onto ``2 * T0``.
    """
    x = frame - float(np.mean(frame))
    if float(np.dot(x, x)) <= 1e-9:
        return 0.0, 0.0
    nfft = 1
    while nfft < 2 * x.size:
        nfft <<= 1
    spec = np.fft.rfft(x, n=nfft)
    ac = np.fft.irfft(spec * np.conjugate(spec), n=nfft)[: x.size]
    if ac[0] <= 1e-12:
        return 0.0, 0.0
    ac = ac / ac[0]
    lag_max = min(lag_max, x.size - 2)
    if lag_min >= lag_max:
        return 0.0, 0.0
    window = ac[lag_min:lag_max + 1]
    best = int(np.argmax(window))
    lag = lag_min + best
    clarity = float(window[best])
    for _ in range(3):                                      # octave guard
        half = lag // 2
        if half < lag_min or float(ac[half]) < clarity - 0.02:
            break
        lag = half
        clarity = float(ac[lag])
    return float(sr) / float(lag), clarity


def voice_features(x: np.ndarray, sr: int = _SAMPLE_RATE) -> _VoiceFeatures:
    """Median F0 + RMS + ZCR of a PCM window. Deterministic, numpy only."""
    feats = _VoiceFeatures(index=-1, start=0.0, end=0.0)
    if x.size < int(sr * 0.12):
        feats.note = "trecho curto demais para análise"
        return feats
    frames = np.ascontiguousarray(_frame_signal(x.astype(np.float32, copy=False)))
    energies = np.sqrt(np.mean(frames * frames, axis=1))
    loud = float(np.percentile(energies, 92)) if energies.size else 0.0
    if loud <= 1e-6:
        feats.note = "áudio silencioso"
        return feats
    active = energies > max(1e-4, 0.08 * loud, 0.25 * loud)
    feats.rms_db = float(20.0 * np.log10(max(1e-6, float(np.sqrt(np.mean(x * x))))))
    feats.zcr = float(np.mean(np.abs(np.diff(np.sign(x))) > 0))

    lag_min = max(2, int(round(sr / _F0_MAX)))
    lag_max = min(_FRAME - 2, int(round(sr / _F0_MIN)))
    pitches: list[float] = []
    clarities: list[float] = []
    idx = np.nonzero(active)[0]
    step = 1 if idx.size <= 400 else int(np.ceil(idx.size / 400))
    for i in idx[::step]:
        f0, clarity = _f0_of_frame(frames[int(i)], sr, lag_min, lag_max)
        if _F0_MIN <= f0 <= _F0_MAX and clarity > 0.0:
            pitches.append(f0)
            clarities.append(clarity)
    total = int(idx.size) or 1
    feats.voiced = len(pitches) / float(total)
    if pitches:
        feats.f0 = float(np.median(pitches))
        feats.clarity = float(np.median(clarities))
    feats.reliable = feats.voiced >= 0.30 and feats.clarity >= 0.45 and feats.f0 > 0.0
    if not feats.reliable and not feats.note:
        feats.note = "F0 não confiável" if feats.f0 else "sem trechos sonorizados"
    return feats


def _kmeans_1d(x: np.ndarray, k: int, *, iters: int = 30) -> tuple[np.ndarray, np.ndarray]:
    """Deterministic 1-D k-means. Centroids start at quantiles, no RNG at all.

    Returning a stable solution matters: the same transcript must produce the
    same speaker numbering whether the user runs it once or ten times.
    """
    n = int(x.size)
    if k <= 1 or n < 2:
        return np.zeros(n, dtype=int), np.array([float(np.mean(x))] if n else [0.0])
    ks = np.arange(1, k + 1, dtype=float)
    centres = np.quantile(x, (ks - 0.5) / k)
    labels = np.zeros(n, dtype=int)
    for _ in range(iters):
        dist = np.abs(x[:, None] - centres[None, :])
        new = np.argmin(dist, axis=1)
        moved = not np.array_equal(new, labels)
        labels = new
        for c in range(k):
            sel = x[labels == c]
            if sel.size:
                centres[c] = float(np.mean(sel))
        centres = np.sort(centres)                          # keep ids ordered by pitch
        if not moved:
            break
    return labels.astype(int), centres


def _silhouette_1d(x: np.ndarray, labels: np.ndarray) -> float:
    """Mean silhouette coefficient (exact, ``O(n^2)``; ``n`` is segment count)."""
    k = int(len(set(labels.tolist())))
    if k < 2 or x.size < 3:
        return 0.0
    d = np.abs(x[:, None] - x[None, :])
    same = np.zeros(x.size)
    other = np.zeros(x.size)
    counts = np.bincount(labels)
    for c in range(int(labels.max()) + 1):
        mask = labels == c
        if not mask.any():
            continue
        intra = d[mask][:, mask].sum(axis=1)
        same[mask] = intra / max(1, int(mask.sum()) - 1)
        inter_sizes = counts.copy()
        inter_sizes[c] = 0
        total_other = d[mask].sum(axis=1) - intra
        n_other = int(x.size - mask.sum())
        if n_other > 0:
            other[mask] = total_other / n_other
        # closest-meaningful-cluster variant is not needed here: the mean over
        # all other clusters is monotone with it on 1-D data and stable.
    denom = np.maximum(same, other)
    ok = denom > 1e-9
    if not ok.any():
        return 0.0
    return float(np.mean((other[ok] - same[ok]) / denom[ok]))


def _choose_k(x: np.ndarray, kmax: int = 4, *, min_silhouette: float = 0.22) -> int:
    """Smallest k whose silhouette beats the previous one by a real margin."""
    if x.size < 3:
        return 1
    limit = max(1, min(kmax, int(np.unique(np.round(x, 3)).size)))
    best_k, best_score = 1, 0.0
    for k in range(2, limit + 1):
        labels, _ = _kmeans_1d(x, k)
        if len(set(labels.tolist())) < k:                    # degenerate split
            break
        score = _silhouette_1d(x, labels)
        if score > best_score + 0.05:
            best_k, best_score = k, score
        else:
            break
    return best_k if best_score >= min_silhouette else 1


def _remap_by_appearance(labels: np.ndarray) -> np.ndarray:
    """Renumber clusters so speaker 0 is the voice that speaks first."""
    out = np.full(labels.shape, -1, dtype=int)
    order: dict[int, int] = {}
    for i, lab in enumerate(labels.tolist()):
        if lab not in order:
            order[lab] = len(order)
        out[i] = order[lab]
    return out


def diarize(transcript: Transcript, path: Path, *,
            cancel: threading.Event | None = None,
            kmax: int = 4,
            on_note: Callable[[str], None] | None = None) -> Transcript:
    """Label ``Segment.speaker`` with a cheap, acoustic heuristic.

    Steps (no extra model, nothing to download, fully deterministic):

    1. every segment window is pulled with FFmpeg seeking per segment
       (``-ss`` before ``-i``, 16 kHz mono, streamed in 64 KiB blocks), so the
       file is never decoded entirely;
    2. per frame the normalised autocorrelation gives a median F0 plus clarity,
       and an RMS in dB and a zero-crossing rate are measured as well;
    3. the reliable F0 values (log2 scale) are clustered by deterministic 1-D
       k-means with k in 1..kmax chosen by the silhouette coefficient; clusters
       are renumbered by first appearance, so speaker 0 always speaks first;
    4. a segment whose pitch was unreliable inherits the label of the closest
       reliable segment in time.

    Honest limits: pitch alone cannot separate two voices with a similar F0
    (two men, or a woman and a man in the same register), there is no speaker
    embedding model here, overlapping speech is ignored, and turn boundaries
    come from the ASR segments, not from the audio. Expect 70-90 % agreement on
    a clean two-voice recording; treat the labels as a UI hint, never as ground
    truth. ``Transcript`` has no ``note`` field (models.py must not change), so
    caveats go to ``logging.getLogger("corteia")`` and to ``on_note``.

    If fewer than 40 % of the segments have a trustworthy F0 every speaker stays
    ``None`` and a note is emitted instead of inventing labels.
    """
    notes: list[str] = []

    def _note(text: str) -> None:
        notes.append(text)
        log.warning("diarize: %s", text)
        if on_note is not None:
            try:
                on_note(text)
            except Exception:                               # noqa: BLE001
                log.exception("on_note callback failed")

    _check_cancel(cancel)
    src = Path(path)
    if not transcript.segments:
        _note("nada a diarizar: a transcrição está vazia")
        return transcript
    if not src.is_file():
        raise TranscriptionError(
            "diarize", f"O arquivo de áudio não existe: {src.name}.", str(src),
        )
    duration, has_audio = _probe_duration(src)
    if not has_audio:
        raise TranscriptionError("diarize", "O arquivo não tem faixa de áudio.")

    feats: dict[int, _VoiceFeatures] = {}
    for i, seg in enumerate(transcript.segments):
        _check_cancel(cancel)
        if seg.words:
            t0 = max(0.0, seg.words[0].start - 0.02)
            t1 = seg.words[-1].end + 0.02
        else:
            t0, t1 = max(0.0, seg.start), seg.end
        if duration:
            t1 = min(t1, duration)
        if t1 - t0 < 0.12:
            feats[i] = _VoiceFeatures(index=i, start=t0, end=t1,
                                      note="janela menor que 120 ms")
            continue
        f = voice_features(extract_pcm(src, t0, t1, cancel=cancel))
        f.index, f.start, f.end = i, t0, t1
        feats[i] = f

    good = {i: f for i, f in feats.items() if f.reliable}
    ratio_ok = len(good) / max(1, len(feats))
    if ratio_ok < 0.40:
        for seg in transcript.segments:
            seg.speaker = None
        _note(f"apenas {len(good)}/{len(feats)} trechos com pitch confiável "
              f"({ratio_ok:.0%}): a diarização por F0 não é segura aqui e nenhum "
              "falante foi atribuído")
        return transcript

    keys = sorted(good)
    values = np.array([np.log2(good[i].f0) for i in keys], dtype=float)
    k = _choose_k(values, kmax=kmax)
    labels = np.zeros(values.size, dtype=int)
    if k > 1:
        labels, _centres = _kmeans_1d(values, k)
        labels = _remap_by_appearance(labels)
    else:
        spread = float(values.max() - values.min()) if values.size else 0.0
        _note(f"uma única voz detectada ({values.size} trechos confiáveis, "
              f"variação de pitch de {spread:.2f} oitavas)")

    by_index = {idx: int(labels[pos]) for pos, idx in enumerate(keys)}
    midpoints = {j: (feats[j].start + feats[j].end) / 2.0 for j in feats}
    for i, seg in enumerate(transcript.segments):
        if i in by_index:
            seg.speaker = by_index[i]
        elif keys:
            centre = midpoints.get(i, (seg.start + seg.end) / 2.0)
            nearest = min(keys, key=lambda j: abs(midpoints[j] - centre))
            seg.speaker = by_index[nearest]
        else:
            seg.speaker = None

    used = sorted({int(s.speaker) for s in transcript.segments if s.speaker is not None})
    log.info("diarize: %d/%d trechos confiáveis, k=%d, falantes=%s, F0=%s Hz, "
             "RMS=%s dB", len(good), len(feats), k, used,
             [round(good[i].f0, 1) for i in keys],
             [round(feats[i].rms_db) for i in keys])
    if transcript.model:
        # keep the diarised labels in the transcript cache: reusing a cached
        # transcription must not silently lose the speakers
        cfile = cache_file(src, transcript.model, transcript.language)
        if cfile.is_file():
            _write_cache(cfile, transcript, source=src, audio_seconds=duration)
    if k > 1:
        _note(f"{len(used)} falante(s) separados apenas por pitch; heurística "
              "acústica — confira os rótulos antes de exportar")
    return transcript


def speaker_counts(transcript: Transcript) -> dict[int, float]:
    """Seconds of speech per speaker id (used by the UI chips)."""
    out: dict[int, float] = {}
    for seg in transcript.segments:
        if seg.speaker is None:
            continue
        out[seg.speaker] = out.get(seg.speaker, 0.0) + seg.duration
    return {k: round(v, 2) for k, v in sorted(out.items())}


# --------------------------------------------------------------------------- #
# editing
# --------------------------------------------------------------------------- #
def _split_tokens(text: str) -> list[str]:
    """Split an edited string the way Whisper tokens look (punctuation glued)."""
    return [t for t in _norm_text(text).split(" ") if t]


def _respace_times(weights: list[float], t0: float, t1: float) -> list[tuple[float, float]]:
    """Distribute ``[t0, t1]`` proportionally to ``weights`` (>= 1 item)."""
    total = sum(weights) or 1.0
    span = max(0.01, t1 - t0)
    out: list[tuple[float, float]] = []
    acc = 0.0
    for w in weights:
        nxt = acc + (w / total)
        out.append((t0 + acc * span, t0 + nxt * span))
        acc = nxt
    if out:
        out[-1] = (out[-1][0], t1)
    return out


def apply_text_edits(transcript: Transcript, edits: dict[int, str]) -> Transcript:
    """Apply ``{segment index: new text}`` in place and keep everything in sync.

    Documented behaviour, because the subtitles are driven by word times:

    * same number of words -> only ``Word.text`` changes, timestamps untouched
      (perfect sync, this is the common typo case);
    * different number of words -> the words of **that segment only** are
      re-laid proportionally to their character length between the original
      first-word start and last-word end (or the segment bounds when it had no
      words). Probabilities are taken from the nearest original word;
    * ``Segment.start``/``.end`` never change, so the timeline, the cut points
      and every other segment keep their sync;
    * an empty string clears the text (the segment stays as a silent marker);
    * unknown indexes are skipped with a warning, never raised — the editor may
      hold a stale index while the pipeline re-ran.
    """
    if not edits:
        return transcript
    for raw_idx, raw_text in edits.items():
        try:
            idx = int(raw_idx)
        except (TypeError, ValueError):
            log.warning("apply_text_edits: índice inválido %r ignorado", raw_idx)
            continue
        if not (0 <= idx < len(transcript.segments)):
            log.warning("apply_text_edits: segmento %d não existe (tem %d)",
                        idx, len(transcript.segments))
            continue
        seg = transcript.segments[idx]
        text = _norm_text("" if raw_text is None else str(raw_text))
        tokens = _split_tokens(text)
        old = list(seg.words)
        seg.text = text

        if not tokens:
            seg.words = []
            continue
        if len(tokens) == len(old):
            for w, token in zip(old, tokens):
                w.text = token
            continue
        if not old:
            span = max(0.05, seg.end - seg.start)
            weights = [float(max(1, len(t))) for t in tokens]
            times = _respace_times(weights, seg.start, seg.start + span)
            seg.words = [Word(round(a, 3), round(b, 3), t, 1.0)
                         for (a, b), t in zip(times, tokens)]
            continue
        weights = [float(max(1, len(t))) for t in tokens]
        times = _respace_times(weights, old[0].start,
                                         max(old[-1].end, old[0].start + 0.05))
        n = max(1, len(old))
        words: list[Word] = []
        for pos, ((a, b), token) in enumerate(zip(times, tokens)):
            mirror = old[min(n - 1, int(pos * n / len(tokens)))]
            words.append(Word(round(a, 3), round(b, 3), token, round(mirror.prob, 3)))
        seg.words = words
        log.debug("apply_text_edits: segmento %d reescalonado %d -> %d palavra(s)",
                  idx, len(old), len(words))
    return transcript


def words_for_range(transcript: Transcript, t0: float, t1: float) -> list[Word]:
    """Every word audible inside ``[t0, t1]``, clamped to the window and sorted.

    A word is kept when it overlaps the window at all (its middle does not have
    to be inside), which is what subtitle rendering needs; ``start``/``end`` are
    clamped so a clip that starts in the middle of a word still shows the word
    from time 0. Reversed inputs are swapped; ``Word`` objects are copies, so
    the transcript itself is never modified.
    """
    lo, hi = float(t0), float(t1)
    if lo > hi:
        lo, hi = hi, lo
    out: list[Word] = []
    for seg in transcript.segments:
        if seg.end + 0.25 <= lo or seg.start - 0.25 >= hi:
            continue
        for w in seg.words:
            if w.end <= lo or w.start >= hi:
                continue
            text = _clean_token(w.text)
            if not text:
                continue
            out.append(Word(round(max(lo, w.start), 3), round(min(hi, w.end), 3),
                            text, w.prob))
    out.sort(key=lambda w: (w.start, w.end))
    return out


# --------------------------------------------------------------------------- #
# small surface used by the settings page
# --------------------------------------------------------------------------- #
def model_catalog() -> list[dict[str, Any]]:
    """Rows for the model list: name, Portuguese description, size, state."""
    rows: list[dict[str, Any]] = []
    for name, meta in MODELS.items():
        present = is_downloaded(name) if name != "auto" else True
        rows.append({
            "name": name,
            "desc": str(meta.get("desc", name)),
            "mb": int(meta.get("mb", 0)),
            "url": MODEL_URLS.get(name, ""),
            "downloaded": bool(present),
            "on_disk": model_size_on_disk(name) if name != "auto" else 0,
            "loaded": loaded_model() == name,
        })
    return rows


def recommended_model(audio_seconds: float | None = None) -> str:
    """What ``auto`` would pick on this machine right now (no download)."""
    return resolve_model("auto", audio_seconds)


__all__ = [
    "MODELS", "MODEL_URLS", "CLOUD_MODELS", "DEFAULT_INITIAL_PROMPT",
    "model_dir", "is_downloaded", "download_model", "model_size_on_disk",
    "model_catalog", "recommended_model", "resolve_model", "normalize_name",
    "transcribe", "transcribe_is_cached", "diarize", "speaker_counts",
    "apply_text_edits", "words_for_range", "join_words", "words_text",
    "estimate_duration_hours", "record_speed", "measured_ratio", "perf_file",
    "cache_key", "cache_file", "clear_transcript_cache", "transcripts_dir",
    "unload_model", "loaded_model", "extract_pcm", "voice_features",
    'check_fit', 'largest_fitting', 'memory_note', 'footprint_gb', 'memory_budget',]
