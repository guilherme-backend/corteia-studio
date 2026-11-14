"""Hardware and FFmpeg capability detection.

Everything here is probed from the real system — no assumption that a GPU exists
and no assumption that an encoder is compiled in. Results are cached because the
answer cannot change during a run.
"""

from __future__ import annotations

import functools
import os
import re
import shutil
from pathlib import Path
import subprocess
from dataclasses import dataclass, field

from .config import get_config


@dataclass(slots=True)
class HardwareReport:
    cpu_count: int = 1
    ram_gb: float = 0.0
    ram_available_gb: float = 0.0
    gpu_vendor: str = ""
    gpu_name: str = ""
    gpu_vram_gb: float = 0.0
    cuda_available: bool = False
    nvenc_available: bool = False
    ffmpeg_version: str = ""
    video_encoders: list[str] = field(default_factory=list)
    subtitles_filter: bool = False
    font_provider: str = ""
    x265_available: bool = False
    notes: list[str] = field(default_factory=list)

    @property
    def has_gpu(self) -> bool:
        return self.cuda_available or bool(self.nvenc_available)

    @property
    def recommended_worker_threads(self) -> int:
        """Leaves headroom for the UI thread; a 4-core laptop gets 3."""
        n = max(1, self.cpu_count - 1)
        if self.ram_gb and self.ram_gb < 8:
            n = min(n, 2)
        return n

    def summary_lines(self) -> list[str]:
        out = [
            f"CPU: {self.cpu_count} threads lógicos" + (f" · RAM: {self.ram_gb:.1f} GB" if self.ram_gb else ""),
            f"FFmpeg: {self.ffmpeg_version or 'não encontrado'}",
        ]
        if self.gpu_name:
            out.append(f"GPU: {self.gpu_name}" + (f" · {self.gpu_vram_gb:.1f} GB VRAM" if self.gpu_vram_gb else ""))
            out.append("CUDA: " + ("disponível" if self.cuda_available else "indisponível"))
        else:
            out.append("GPU: não detectada — processamento por CPU")
        out.append("Encoders de vídeo: " + (", ".join(self.video_encoders) or "nenhum"))
        out.append("Filtro de legendas (subtitles): " + ("sim" if self.subtitles_filter else "não"))
        out.extend(self.notes)
        return out


def _run(cmd: list[str], timeout: float = 8.0) -> str:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           creationflags=_no_window())
        return (r.stdout or "") + (r.stderr or "")
    except (OSError, subprocess.SubprocessError):
        return ""


def _no_window() -> int:
    if os.name == "nt":
        return getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return 0


def find_ffmpeg() -> str:
    """Path to the ffmpeg binary. Honours an explicit override, then the bundle,
    then PATH. Returns ``""`` when nothing usable is found."""
    cfg = get_config().str_("ffmpeg.path") if get_config().get("ffmpeg.path") else ""
    if cfg and os.path.isfile(cfg):
        return cfg
    from . import paths
    exe = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
    for base in (paths.bundle_root(), paths.bundle_root() / "bin",
                 paths.bundle_root() / "ffmpeg",
                 # install.sh e install.bat baixam o FFmpeg para
                 # third_party/ffmpeg; sem este caminho a promessa do
                 # script nunca virava detecção
                 paths.bundle_root().parent / "third_party" / "ffmpeg",
                 Path(__file__).resolve().parent.parent / "third_party" / "ffmpeg"):
        p = base / exe
        if p.is_file():
            return str(p)
    return shutil.which(exe) or ""


def find_ffprobe() -> str:
    if cfg_path := get_config().get("ffprobe.path"):
        if os.path.isfile(str(cfg_path)):
            return str(cfg_path)
    from . import paths
    exe = "ffprobe.exe" if os.name == "nt" else "ffprobe"
    for base in (paths.bundle_root(), paths.bundle_root() / "bin", paths.bundle_root() / "ffmpeg"):
        p = base / exe
        if p.is_file():
            return str(p)
    ff = find_ffmpeg()
    if ff:
        sib = os.path.join(os.path.dirname(ff), exe)
        if os.path.isfile(sib):
            return sib
    return shutil.which(exe) or ""


@functools.lru_cache(maxsize=1)
def probe() -> HardwareReport:
    import psutil

    hw = HardwareReport(cpu_count=os.cpu_count() or 1)
    try:
        vm = psutil.virtual_memory()
        hw.ram_gb = vm.total / 1024**3
        hw.ram_available_gb = vm.available / 1024**3
    except Exception:                                     # noqa: BLE001
        pass

    ff = find_ffmpeg()
    if ff:
        ver = _run([ff, "-hide_banner", "-version"])
        m = re.search(r"ffmpeg version (\S+)", ver)
        hw.ffmpeg_version = m.group(1) if m else ver.splitlines()[0][:60] if ver else ""
        long_out = _run([ff, "-hide_banner", "-L"], timeout=12)      # full licence/features
        short = _run([ff, "-hide_banner", "-encoders"], timeout=12)
        blob = long_out + "\n" + short
        encoders = []
        for name in ("libx264", "libx265", "h264_nvenc", "hevc_nvenc", "h264_amf", "hevc_amf",
                     "h264_qsv", "hevc_qsv", "h264_videotoolbox", "mpeg4"):
            if re.search(rf"\b{name}\b", blob):
                encoders.append(name)
        hw.video_encoders = encoders
        hw.x265_available = "libx265" in encoders
        hw.nvenc_available = "h264_nvenc" in encoders
        filters = _run([ff, "-hide_banner", "-filters"], timeout=12)
        hw.subtitles_filter = bool(re.search(r"\bsubtitles\b", filters))
        hw.font_provider = ("fontconfig" if "fontconfig" in long_out else
                            "directwrite" if "directwrite" in long_out else
                            "freebuffer" if "freetype" in long_out else "")
        if not hw.subtitles_filter:
            hw.notes.append("AVISO: este FFmpeg foi compilado sem o filtro 'subtitles' "
                            "(libass). As legendas não serão gravadas no vídeo.")
    else:
        hw.notes.append("AVISO: FFmpeg não encontrado. Instale ou informe o caminho em Configurações.")

    # NVIDIA ----------------------------------------------------------------
    nv = _run(["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"])
    m = re.match(r"([^,]+),\s*([0-9.]+)", nv.strip())
    if m:
        hw.gpu_vendor = "NVIDIA"
        hw.gpu_name = m.group(1).strip()
        try:
            hw.gpu_vram_gb = float(m.group(2)) / 1024.0
        except ValueError:
            hw.gpu_vram_gb = 0.0

    # NVENC in FFmpeg only proves the encoder is compiled in; it says nothing
    # about a working CUDA runtime.  Handing ctranslate2 device="cuda" on a fake
    # signal crashes with "CUDA driver version is insufficient", so CUDA is
    # claimed only when a GPU answered nvidia-smi *and* the runtime agrees.
    hw.cuda_available = bool(hw.gpu_name) and (_cuda_runtime_ok() or _torch_has_cuda())
    if hw.nvenc_available and not hw.cuda_available:
        hw.notes.append("Este FFmpeg tem NVENC, mas não há runtime CUDA ativo: a "
                        "codificação será por CPU. NVENC exige driver NVIDIA instalado.")

    if not hw.gpu_name:
        amd = ""
        if os.name == "nt":
            # `wmic` foi REMOVIDO no Windows 11 24H2 e no Server 2025, então o
            # caminho principal é o CIM via PowerShell; wmic fica como fallback
            # para Windows 10 e 11 antigos.
            amd = _run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                        "Get-CimInstance Win32_VideoController | "
                        "Select-Object -ExpandProperty AdapterRAM,Name"], timeout=8)
            if not amd.strip() or "Exception" in amd:
                amd = _run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                            "Get-CimInstance Win32_VideoController | ForEach-Object "
                            "{ $_.Name }"], timeout=8)
            if not amd.strip():
                amd = _run(["wmic", "path", "win32_videocontroller", "get", "name"])
        if not amd:
            amd = _run(["lspci"], timeout=6)
        for token in ("AMD", "Radeon", "Intel", "Arc"):
            if token.lower() in amd.lower():
                hw.gpu_vendor = token
                mm = re.search(rf"[^\n]*{token}[^\n]*", amd, re.I)
                hw.gpu_name = mm.group(0).strip()[:80] if mm else token
                break
        if hw.gpu_vendor and not hw.cuda_available:
            hw.notes.append(f"GPU {hw.gpu_vendor} detectada, mas sem CUDA: usando CPU "
                            "(ou NVENC/AMF quando disponível no FFmpeg).")

    if hw.ram_gb and hw.ram_gb < 8:
        hw.notes.append("RAM limitada (< 8 GB): prefira modelos Whisper 'base' ou 'small'.")
    return hw


def _cuda_runtime_ok() -> bool:
    """Ask ctranslate2 itself — the library the ASR stage actually uses."""
    try:
        import ctranslate2
        return int(ctranslate2.get_supported_device_count("cuda") or 0) > 0
    except Exception:                                     # noqa: BLE001
        return False


def _torch_has_cuda() -> bool:
    try:
        import torch
        return bool(torch.cuda.is_available())
    except Exception:                                     # noqa: BLE001
        return False


def ctranslate2_device() -> tuple[str, str]:
    """Returns ``(device, compute_type)`` for faster-whisper honouring settings.

    ``auto`` means: CUDA + float16 when usable, otherwise CPU + int8 (smaller and
    faster on laptops) falling back to float32 if int8 is unsupported.
    """
    cfg = get_config()
    want_device = cfg.str_("proc.device") or "auto"
    want_compute = cfg.str_("proc.compute_type") or "auto"
    hw = probe()

    if want_device == "auto":
        device = "cuda" if hw.cuda_available else "cpu"
    else:
        device = want_device
        if device == "cuda" and not hw.cuda_available:
            device = "cpu"

    if want_compute != "auto":
        return device, want_compute
    if device == "cuda":
        return device, "float16"
    # int8 halves memory and is ~2x faster on CPU; large models keep int8_float16 out
    return device, "int8"


def parallel_limit(default: int = 2) -> int:
    cfg = get_config()
    n = cfg.int_("proc.max_parallel_jobs") or default
    hw = probe()
    if hw.ram_gb:
        # an x264 1080p encode peaks near 1.5 GB; never let the queue thrash RAM
        cap = max(1, int(hw.ram_gb // 2.0))
        n = min(n, cap, max(1, hw.cpu_count // 2))
    return max(1, int(n))


def invalidate_cache() -> None:
    probe.cache_clear()
