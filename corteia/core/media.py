"""Probing, thumbnails, single frames and streaming audio analysis.

Nothing in here decodes a whole video into RAM: audio is read as fixed-size PCM
blocks straight from FFmpeg's stdout pipe and frames come back one at a time.
"""

from __future__ import annotations

import hashlib
import logging
import math
import mimetypes
import re
import threading
from pathlib import Path

import numpy as np

from .. import paths
from ..models import VideoInfo
from . import ffmpeg_cli
from .errors import MediaError

log = logging.getLogger("corteia")

SUPPORTED_EXTS = (
    ".mp4", ".m4v", ".mov", ".mkv", ".webm", ".avi", ".mpg", ".mpeg", ".mts",
    ".m2ts", ".ts", ".wmv", ".flv", ".3gp", ".3g2", ".ogv", ".vob", ".divx",
)
AUDIO_EXTS = (".mp3", ".wav", ".aac", ".m4a", ".flac", ".ogg", ".opus", ".wma", ".aif", ".aiff")

# container -> codecs that can be cut without re-encoding
_COPY_CODECS: dict[str, set[str]] = {
    ".mp4": {"h264", "hevc", "mpeg4"},
    ".m4v": {"h264", "hevc", "mpeg4"},
    ".mov": {"h264", "hevc", "mpeg4", "prores"},
    ".mkv": {"h264", "hevc", "mpeg4", "mpeg2video", "vp8", "vp9", "av1"},
    ".webm": {"vp8", "vp9", "av1"},
    ".ts": {"h264", "hevc", "mpeg2video"},
    ".mts": {"h264", "hevc", "mpeg2video"},
    ".m2ts": {"h264", "hevc", "mpeg2video", "vc1"},
    ".avi": {"mpeg4", "h264", "hevc"},
    ".wmv": {"wmv2", "wmv3", "vc1"},
}

_probe_cache: dict[tuple[str, int, int], VideoInfo] = {}
_probe_lock = threading.Lock()
_CACHE_MAX = 32


# --------------------------------------------------------------------------- #
# scalar helpers
# --------------------------------------------------------------------------- #
def _num(value: object, default: float = 0.0) -> float:
    """Parse an ffprobe scalar (``"12.5"``, ``30000``, ``None``, ``"N/A"``)."""
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text or text.lower() in ("n/a", "none", "unknown"):
        return default
    try:
        return float(text)
    except ValueError:
        return default


def _fraction(value: object) -> float:
    """``"30000/1001"`` -> ``29.97002997``; plain floats/ints pass through."""
    if value is None:
        return 0.0
    text = str(value).strip()
    if not text or text.lower() in ("n/a", "none", "0/0"):
        return 0.0
    if "/" in text:
        num, _, den = text.partition("/")
        n, d = _num(num), _num(den)
        return n / d if d else 0.0
    return _num(text)


def _rotation_of(stream: dict) -> int:
    """Rotation in degrees from ``side_data_list`` (display matrix) or tags."""
    for side in stream.get("side_data_list") or []:
        if not isinstance(side, dict) or "rotation" not in side:
            continue
        rot = int(_num(side.get("rotation")))
        if rot:
            return ((rot % 360) + 360) % 360
    rot = int(_num((stream.get("tags") or {}).get("rotate")))
    return ((rot % 360) + 360) % 360 if rot else 0


def _is_real_video(stream: dict) -> bool:
    """A video stream that is not embedded cover art."""
    if stream.get("codec_type") != "video":
        return False
    return not bool((stream.get("disposition") or {}).get("attached_pic"))


def _decode_duration(path: Path) -> float:
    """Last resort for containers with no duration field: demux to the end."""
    cmd = [
        "ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "info", "-progress", "pipe:2",
        "-i", str(path), "-map", "0:v:0?", "-map", "0:a:0?", "-c", "copy", "-f", "null", "-",
    ]
    try:
        _, tail = ffmpeg_cli.capture(cmd, timeout=600.0)
    except MediaError as exc:
        log.debug("Fallback de duração falhou para %s: %s", path, exc.detail)
        return 0.0
    best = 0.0
    for line in tail.splitlines():
        secs = ffmpeg_cli.extract_seconds(line)
        if secs is not None:
            best = max(best, secs)
    return best


# --------------------------------------------------------------------------- #
# probe
# --------------------------------------------------------------------------- #
def probe(path: Path) -> VideoInfo:
    """Read container/stream metadata with ffprobe (cached per file version).

    ``width``/``height`` are the **display** dimensions: when the file carries a
    90/270 rotation the values are already swapped and ``rotation`` keeps the angle.
    """
    p = Path(path)
    key: tuple[str, int, int] | None = None
    try:
        st = p.stat()
        key = (str(p), int(st.st_size), int(st.st_mtime))
    except OSError:
        pass
    if key is not None:
        with _probe_lock:
            hit = _probe_cache.get(key)
        if hit is not None:
            return hit

    data = ffmpeg_cli.run_json([
        "ffprobe", "-v", "error", "-print_format", "json",
        "-show_format", "-show_streams", str(p),
    ])
    fmt = data.get("format") or {}
    streams = [s for s in (data.get("streams") or []) if isinstance(s, dict)]

    info = VideoInfo(path=str(p))
    video = next((s for s in streams if _is_real_video(s)), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)

    if video is not None:
        info.width = int(_num(video.get("width")))
        info.height = int(_num(video.get("height")))
        info.rotation = _rotation_of(video)
        if info.rotation in (90, 270) and info.width and info.height:
            info.width, info.height = info.height, info.width
        info.fps = _fraction(video.get("avg_frame_rate")) or _fraction(video.get("r_frame_rate"))
        stream_dur = _num(video.get("duration"))
        frames = _num(video.get("nb_frames"))
        if not info.fps and frames and stream_dur > 0:
            info.fps = frames / stream_dur
        info.codec_video = str(video.get("codec_name") or "")
        info.duration = stream_dur
        info.bitrate = int(_num(video.get("bit_rate")))
    if audio is not None:
        info.codec_audio = str(audio.get("codec_name") or "")
        if info.duration <= 0:
            info.duration = _num(audio.get("duration"))
    info.has_audio = audio is not None

    info.duration = _num(fmt.get("duration")) or info.duration
    info.bitrate = int(_num(fmt.get("bit_rate"))) or info.bitrate
    if info.duration <= 0:
        info.duration = _decode_duration(p)
    if info.bitrate <= 0 and info.duration > 0:
        try:
            info.bitrate = int(p.stat().st_size * 8 / info.duration)
        except OSError:
            pass
    if not info.fps and info.width:
        # a stream reporting 0/0 (some VFR captures): assume the common 25/30 grid
        info.fps = 30.0 if info.width >= 1280 else 25.0

    if key is not None:
        with _probe_lock:
            if len(_probe_cache) >= _CACHE_MAX:
                _probe_cache.clear()
            _probe_cache[key] = info
    return info


def probe_or_raise(path: Path) -> VideoInfo:
    """Probe and validate the file is usable video; Portuguese messages."""
    p = Path(path)
    if not p.exists():
        raise MediaError("media", f"O arquivo de vídeo não existe: {p.name}")
    if p.is_dir():
        raise MediaError("media", "O caminho informado é uma pasta, não um arquivo de vídeo.")
    try:
        info = probe(p)
    except MediaError as exc:
        raise MediaError(
            "probe",
            f"Não foi possível ler o arquivo de vídeo “{p.name}”. Ele pode estar "
            "corrompido ou em formato incompatível.",
            exc.detail,
        ) from exc
    if not info.codec_video or info.width <= 0 or info.height <= 0:
        raise MediaError("media", "O arquivo não contém vídeo.")
    if info.duration <= 0:
        raise MediaError("media", "A duração do vídeo não pôde ser determinada "
                                  "(arquivo incompleto ou corrompido).")
    return info


def is_supported(path: Path) -> tuple[bool, str]:
    """Check extension/mimetype and explain the reason in Portuguese."""
    p = Path(path)
    ext = p.suffix.lower()
    if ext in AUDIO_EXTS:
        return False, "Arquivos de áudio não são aceitos — importe um vídeo com imagem."
    if ext in SUPPORTED_EXTS:
        return True, ""
    mime, _ = mimetypes.guess_type(str(p))
    if mime and mime.startswith("video/"):
        return True, ""
    if not ext:
        return False, "Selecione um arquivo de vídeo (MP4, MOV, MKV, WEBM, AVI ou MPEG-TS)."
    return False, (f"Formato “{ext}” não é suportado. Use MP4, MOV, MKV, WEBM, AVI "
                   "ou MPEG-TS e tente novamente.")


# --------------------------------------------------------------------------- #
# frames / images
# --------------------------------------------------------------------------- #
def _thumb_dir() -> Path:
    d = paths.cache_dir() / "thumbs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _cache_key(path: Path, at: float) -> str:
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = 0.0
    raw = f"{path}|{mtime:.6f}|{at:.3f}".encode("utf-8", "replace")
    return hashlib.sha1(raw).hexdigest()      # noqa: S324 - cache key, not a secret


def thumbnail(path: Path, at: float = -1, size: int = 480, dest: Path | None = None) -> Path:
    """Write (or reuse from cache) a JPEG thumbnail of the frame at ``at`` seconds.

    ``at < 0`` grabs the frame at 10% of the video; ``size`` is the target width.
    The seek sits before ``-i`` so the file is never fully decoded.
    """
    p = Path(path)
    if not p.exists():
        raise MediaError("media", f"O arquivo de vídeo não existe: {p.name}")
    info = probe(p)
    duration = info.duration
    stamp = float(at)
    if stamp < 0:
        stamp = duration * 0.1 if duration > 0 else 0.0
    if duration > 0:
        stamp = min(max(0.0, stamp), max(0.0, duration - 0.04))
    width = max(48, int(size))

    if dest is not None:
        target = Path(dest)
        target.parent.mkdir(parents=True, exist_ok=True)
    else:
        target = _thumb_dir() / f"{_cache_key(p, stamp)}_{width}.jpg"
    if target.exists() and target.stat().st_size > 0:
        return target

    cmd = [
        "ffmpeg", "-hide_banner", "-nostdin", "-v", "error", "-y",
        "-ss", f"{stamp:.3f}", "-i", str(p),
        "-frames:v", "1", "-vf", f"scale='min({width},iw)':-2:flags=bicubic",
        "-an", "-sn", "-dn", "-q:v", "3", "-f", "image2", str(target),
    ]
    try:
        ffmpeg_cli.run(cmd, timeout=120.0)
    except Exception as exc:                                # noqa: BLE001
        target.unlink(missing_ok=True)
        detail = getattr(exc, "detail", "") or str(exc)
        log.warning("Miniatura falhou para %s@%.2fs: %s", p.name, stamp, detail)
        raise MediaError("media", f"Não foi possível gerar a miniatura de “{p.name}”.",
                         detail) from exc
    if not target.exists() or target.stat().st_size == 0:
        target.unlink(missing_ok=True)
        raise MediaError("media",
                         f"Não foi possível gerar a miniatura de “{p.name}” "
                         "(o ponto pedido está fora do vídeo).")
    return target


def frame_at(path: Path, at: float, width: int = 640) -> np.ndarray | None:
    """Decode one frame as a BGR array (OpenCV layout), or ``None`` on failure."""
    p = Path(path)
    try:
        info = probe(p)
    except MediaError:
        return None
    if not info.width or not info.height:
        return None
    if float(at) < 0.0:
        # Tempo negativo não é "começo do vídeo": é um bug de quem calculou a
        # janela. Devolver o quadro 0 escondia o erro e gerava miniatura
        # idêntica para cortes diferentes.
        return None
    w = max(16, int(width))
    h = max(16, int(round(info.height * (w / info.width))))
    dur = info.duration
    stamp = min(max(0.0, float(at)), max(0.0, dur - 0.04)) if dur > 0 else max(0.0, float(at))

    cmd = [
        "ffmpeg", "-hide_banner", "-nostdin", "-v", "error",
        "-ss", f"{stamp:.3f}", "-i", str(p),
        "-frames:v", "1", "-vf", f"scale={w}:{h}",
        "-an", "-sn", "-dn", "-f", "rawvideo", "-pix_fmt", "bgr24", "pipe:1",
    ]
    try:
        proc = ffmpeg_cli.spawn_reader(cmd)
    except MediaError as exc:
        log.debug("frame_at(%s): %s", p.name, exc.detail)
        return None
    try:
        data = proc.read_exact(w * h * 3)
        if len(data) < w * h * 3:
            log.debug("frame_at(%s, %.2fs): quadro incompleto (%d bytes)", p.name, stamp, len(data))
            return None
        return np.frombuffer(data, dtype=np.uint8).reshape(h, w, 3)
    except MediaError as exc:
        log.debug("frame_at(%s, %.2fs) falhou: %s", p.name, stamp, exc.detail)
        return None
    finally:
        proc.close(kill=True)


# --------------------------------------------------------------------------- #
# audio
# --------------------------------------------------------------------------- #
def extract_wav(path: Path, sr: int = 16000) -> Path:
    """Decode the audio track to mono 16-bit PCM WAV in the temp dir (cached)."""
    p = Path(path)
    rate = max(8000, int(sr))
    out = paths.temp_sub("audio") / f"{p.stem}-{rate}hz.wav"
    try:
        if out.exists() and out.stat().st_size > 44 and out.stat().st_mtime >= p.stat().st_mtime:
            return out
    except OSError:
        pass
    cmd = [
        "ffmpeg", "-hide_banner", "-nostdin", "-v", "error", "-y", "-i", str(p),
        "-vn", "-sn", "-dn", "-map", "0:a:0", "-ac", "1", "-ar", str(rate),
        "-c:a", "pcm_s16le", str(out),
    ]
    try:
        ffmpeg_cli.run(cmd)
    except Exception as exc:                                # noqa: BLE001
        out.unlink(missing_ok=True)
        detail = getattr(exc, "detail", "") or str(exc)
        raise MediaError("media", f"Não foi possível extrair o áudio de “{p.name}”.",
                         detail) from exc
    if not out.exists() or out.stat().st_size <= 44:
        out.unlink(missing_ok=True)
        raise MediaError("media", f"O arquivo “{p.name}” não tem áudio para extrair.")
    return out


def _pcm_blocks(path: Path, sr: int, block: int, blocks_per_read: int
                ) -> tuple[list[float], list[float]]:
    """Yield RMS dB per ``block`` samples by reading ffmpeg's f32le stdout in chunks.

    Only ``blocks_per_read`` blocks are ever held in memory, so cost is independent
    of the video length (a 1 h track is streamed, never buffered).
    """
    cmd = [
        "ffmpeg", "-hide_banner", "-nostdin", "-v", "error", "-i", str(path),
        "-vn", "-sn", "-dn", "-map", "0:a:0?", "-ac", "1", "-ar", str(sr),
        "-c:a", "pcm_f32le", "-f", "f32le", "pipe:1",
    ]
    hop = block / sr
    read_size = block * 4 * max(1, blocks_per_read)
    times: list[float] = []
    dbs: list[float] = []
    proc = ffmpeg_cli.spawn_reader(cmd)
    try:
        pending = np.zeros(0, dtype=np.float32)
        index = 0
        while True:
            raw = proc.read_exact(read_size)
            if not raw:
                break
            data = np.frombuffer(raw, dtype=np.float32)
            if data.size == 0:
                break
            if pending.size:
                data = np.concatenate((pending, data))
            usable = (data.size // block) * block
            if usable == 0:
                pending = data
                if len(raw) < read_size:
                    break
                continue
            blocks = data[:usable].reshape(-1, block)
            pending = data[usable:].copy()
            rms = np.sqrt(np.mean(np.square(blocks, dtype=np.float64), axis=1))
            db = 20.0 * np.log10(np.maximum(rms, 1e-10))
            base = index
            times.extend((base + np.arange(db.size, dtype=np.float64) + 0.5) * hop)
            dbs.extend(db.tolist())
            index += int(db.size)
            if len(raw) < read_size:
                break
            if index * hop > 86400.0:          # 24 h sanity stop
                log.warning("Curva de áudio interrompida em 24 h: %s", path.name)
                break
        if pending.size >= max(64, block // 8):
            # incomplete trailing block: emit it as a single frame at its own centre
            tail = float(np.sqrt(np.mean(np.square(pending.astype(np.float64)))))
            times.append(index * hop + (pending.size / sr) / 2.0)
            dbs.append(20.0 * math.log10(max(tail, 1e-10)))
        return times, dbs
    finally:
        proc.close(kill=True)


def audio_energy_curve(
    path: Path,
    hop: float = 0.1,
    db_floor: float = -70.0,
    *,
    max_frames: int = 200_000,
) -> tuple[np.ndarray, np.ndarray]:
    """RMS curve in dBFS for the waveform: returns ``(times, db)``.

    Audio is decoded to 8 kHz mono float and consumed **block by block** from the
    ffmpeg pipe, so a one hour video costs a few hundred KB instead of gigabytes.
    ``hop`` is the frame step in seconds; ``max_frames`` widens it automatically on
    very long files, and videos without audio yield empty arrays.
    """
    p = Path(path)
    empty = (np.zeros(0, dtype=np.float64), np.zeros(0, dtype=np.float64))
    try:
        info = probe(p)
    except MediaError as exc:
        log.warning("Probe falhou em audio_energy_curve(%s): %s", p.name, exc.detail)
        return empty
    if not info.has_audio or not info.codec_audio:
        return empty

    sr = 8000
    hop_used = max(0.02, float(hop))
    if info.duration > 0 and info.duration / hop_used > max_frames:
        hop_used = info.duration / float(max_frames)         # keep the curve affordable
    block = max(16, int(round(sr * hop_used)))

    try:
        times, dbs = _pcm_blocks(p, sr, block, blocks_per_read=16)
    except MediaError as exc:
        log.warning("Áudio de %s não pôde ser decodificado: %s", p.name, exc.detail)
        return empty
    if not dbs:
        return empty
    db = np.clip(np.asarray(dbs, dtype=np.float64), float(db_floor), 0.0)
    t = np.asarray(times, dtype=np.float64)
    log.debug("Curva de áudio: %d quadros @ %.3fs (%s)", db.size, block / sr, p.name)
    return t, db


# --------------------------------------------------------------------------- #
# misc
# --------------------------------------------------------------------------- #
def container_supports_stream_copy(path: Path) -> bool:
    """True when this file can be cut without re-encoding (codec/container match)."""
    p = Path(path)
    allowed = _COPY_CODECS.get(p.suffix.lower())
    if allowed is None:
        return False
    try:
        info = probe(p)
    except MediaError:
        return False
    return bool(info.codec_video) and info.codec_video in allowed


# x264 talking-head footage at CRF 23, bits/s per output height; the classic
# "-6 CRF doubles the bitrate" rule then scales it.
_CRF_BASE: dict[str, float] = {"720p": 2_600_000.0, "1080p": 4_800_000.0, "4K": 18_000_000.0}


def estimate_export_size(info: VideoInfo, seconds: float, crf: int, res: str) -> int:
    """Estimate the exported size in bytes from resolution, CRF and duration."""
    base = _CRF_BASE.get(str(res), _CRF_BASE["1080p"])
    video_bps = base * 2.0 ** ((23.0 - float(crf)) / 6.0)
    if info.bitrate > 0:
        video_bps = min(video_bps, info.bitrate * 1.35)   # source cannot give more detail
    total_bits = (video_bps + 192_000.0) * max(0.0, float(seconds))
    return int(round(total_bits / 8.0, -2))


_URL_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://")


def video_url_or_path(path: str) -> str:
    """Normalise a source for use **inside a filtergraph** (``movie=``, ``subtitles=``).

    URLs are returned untouched; local paths become absolute and get the escaping
    (``\\``, ``:``, quotes, brackets, commas, semicolons) filter arguments require.
    Do not use the result for ``-i`` — pass the plain path there.
    """
    text = str(path or "").strip()
    if not text:
        return text
    if _URL_RE.match(text) or text.startswith(("pipe:", "fd:")):
        return text
    p = Path(text).expanduser()
    try:
        p = p.resolve()
    except OSError:
        pass
    escaped = str(p).replace("\\", "\\\\").replace(":", r"\:").replace("'", r"\'")
    for char in ("[", "]", ",", ";"):
        escaped = escaped.replace(char, "\\" + char)
    return escaped


def invalidate_cache(path: Path | None = None) -> None:
    """Drop cached probe results (all of them, or only those of ``path``)."""
    with _probe_lock:
        if path is None:
            _probe_cache.clear()
            return
        key = str(Path(path))
        for k in [k for k in _probe_cache if k[0] == key]:
            _probe_cache.pop(k, None)
