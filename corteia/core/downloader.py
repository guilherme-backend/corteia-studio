"""URL download through yt-dlp, behind an explicit consent switch.

Nothing here runs without the user agreeing in Configurações → Download, and local
addresses are refused: the app is meant to fetch the user's own public links, not to
crawl the network or touch a LAN.
"""

from __future__ import annotations

import ipaddress
import logging
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from .. import paths
from ..config import get_config
from .errors import CancelledError, ProcessingError

log = logging.getLogger("corteia")

ProgressFn = Callable[[float, str], None]

#: best progressive/segmented mp4 up to 1080p (video+audio merged by ffmpeg)
FORMAT_SELECTOR = "bv*[height<=1080][ext=mp4]+ba/b[height<=1080]"
_ALLOWED_SCHEMES = ("http", "https")
_LOCAL_HOSTS = ("localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback")

_URL_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://")


def _consent_given() -> bool:
    return bool(get_config().get("net.download_consent", False))


def _is_local_host(host: str) -> bool:
    """True for loopback/private/link-local names and addresses (LAN is off-limits)."""
    name = (host or "").strip().strip("[]").lower()
    if not name:
        return True
    if name in _LOCAL_HOSTS or name.endswith((".local", ".localhost", ".internal", ".lan")):
        return True
    if name in ("::1",):
        return True
    try:
        ip = ipaddress.ip_address(name)
    except ValueError:
        return False
    return bool(ip.is_loopback or ip.is_private or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified)


def is_url(text: str) -> bool:
    """Cheap check used by the import page to tell a link from a file path."""
    return bool(_URL_RE.match(str(text or "").strip()))


def can_download(url: str) -> tuple[bool, str]:
    """Validate a link before touching the network: ``(ok, message_in_portuguese)``."""
    text = str(url or "").strip()
    if not text:
        return False, "Informe o link do vídeo que você quer baixar."
    if not is_url(text):
        return False, "Isso não parece ser um link (http/https). Para arquivos do computador, " \
                      "use “Escolher arquivo”."
    try:
        parsed = urlparse(text)
    except ValueError:
        return False, "O link informado é inválido."
    if parsed.scheme.lower() not in _ALLOWED_SCHEMES:
        return False, f"O esquema “{parsed.scheme}” não é permitido. Use links http ou https."
    if _is_local_host(parsed.hostname or ""):
        return False, "Links de rede local não são permitidos para download."
    if not parsed.netloc:
        return False, "O link informado está incompleto."
    if not _consent_given():
        return False, ("O download por link está desativado. Ative a permissão em "
                       "Configurações → Download antes de tentar novamente.")
    return True, ""


# --------------------------------------------------------------------------- #
# yt-dlp options
# --------------------------------------------------------------------------- #
def build_opts(dest_dir: Path, on_progress: ProgressFn | None = None,
               cancel: threading.Event | None = None) -> dict[str, Any]:
    """Return the yt-dlp option dict used by :func:`download` (no network touched)."""
    out_dir = Path(dest_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    def _hook(d: dict) -> None:
        if cancel is not None and cancel.is_set():
            raise _cancel_exception()
        if on_progress is None:
            return
        status = d.get("status")
        if status == "downloading":
            total = float(d.get("total_bytes") or d.get("total_bytes_estimate") or 0.0)
            done = float(d.get("downloaded_bytes") or 0.0)
            frac = min(0.94, max(0.0, done / total)) if total > 0 else 0.0
            speed = d.get("speed")
            eta = d.get("eta")
            bits = f"{(speed or 0) / 1_000_000:.1f} MB/s" if speed else "velocidade desconhecida"
            msg = f"Baixando… {bits}"
            if eta:
                msg += f" · faltam {int(eta)} s"
            on_progress(frac, msg)
        elif status == "finished":
            on_progress(0.95, "Juntando vídeo e áudio (merge)…")
        elif status == "error":
            on_progress(0.0, "Falha no download")

    return {
        "format": FORMAT_SELECTOR,
        "outtmpl": str(out_dir / "%(title).80B [%(id)s].%(ext)s"),
        "merge_output_format": "mp4",
        "noplaylist": True,
        "playlist_items": "1",
        "quiet": True,
        "no_warnings": True,
        "no_color": True,
        "noprogress": True,
        "restrictfilenames": True,
        "writesubtitles": False,
        "writeautomaticsub": False,
        "writeinfojson": False,
        "writeannotations": False,
        "writethumbnail": False,
        "overwrites": True,
        "retries": 3,
        "fragment_retries": 3,
        "socket_timeout": 30,
        "cachedir": str(paths.temp_sub("ydl-cache")),
        "progress_hooks": [_hook],
        "postprocessor_hooks": [],
        "logger": _Logger(),
        "concurrent_fragment_downloads": 3,
    }


class _Logger:
    """Route yt-dlp messages into the app logger instead of stdout."""

    def debug(self, msg: str) -> None:
        log.debug("yt-dlp: %s", msg)

    def info(self, msg: str) -> None:
        log.debug("yt-dlp: %s", msg)

    def warning(self, msg: str) -> None:
        log.warning("yt-dlp: %s", msg)

    def error(self, msg: str) -> None:
        log.error("yt-dlp: %s", msg)


def _cancel_exception():
    """The exception class yt-dlp understands as "stop this download"."""
    from yt_dlp.utils import DownloadCancelled

    return DownloadCancelled("cancelado pelo usuário")


def _yt_dlp():
    try:
        import yt_dlp
    except ImportError as exc:                              # pragma: no cover - dependency
        raise ProcessingError(
            "download",
            "A biblioteca de download (yt-dlp) não está instalada neste ambiente.",
            str(exc),
        ) from exc
    return yt_dlp


# --------------------------------------------------------------------------- #
# public API
# --------------------------------------------------------------------------- #
def download(
    url: str,
    dest_dir: Path | None = None,
    *,
    on_progress: ProgressFn | None = None,
    cancel: threading.Event | None = None,
) -> Path:
    """Download the best mp4 <= 1080p of ``url`` and return the saved file.

    Honours ``net.download_consent`` (see :func:`can_download`), merges separate
    video/audio streams into MP4 and reports 0..1 progress. Cancellation raises
    :class:`CancelledError`; every other failure is a :class:`ProcessingError`
    with a Portuguese message.
    """
    ok, why = can_download(url)
    if not ok:
        raise ProcessingError("download", why)
    target_dir = Path(dest_dir) if dest_dir is not None else paths.temp_sub("downloads")
    yt = _yt_dlp()
    opts = build_opts(target_dir, on_progress, cancel)
    try:
        with yt.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
    except CancelledError:
        raise
    except Exception as exc:                                # noqa: BLE001
        if cancel is not None and cancel.is_set():
            raise CancelledError() from exc
        if type(exc).__name__ == "DownloadCancelled":
            raise CancelledError() from exc
        log.error("Download falhou para %s: %s", url, exc)
        raise ProcessingError(
            "download",
            "Não foi possível baixar este vídeo. Verifique o link, sua conexão e se o "
            "vídeo é público e permite download.",
            str(exc),
        ) from exc
    if cancel is not None and cancel.is_set():
        raise CancelledError()

    path = _result_path(info, target_dir, yt)
    if path is None:
        raise ProcessingError("download", "O download terminou, mas o arquivo final não foi "
                                          "encontrado no disco.")
    log.info("Download concluído: %s", path)
    if on_progress is not None:
        try:
            on_progress(1.0, "Download concluído")
        except Exception:                                    # noqa: BLE001
            log.exception("callback de progresso falhou")
    return path


def _result_path(info: dict | None, target_dir: Path, yt) -> Path | None:
    """Resolve the final file from yt-dlp's info dict, falling back to a scan."""
    if info:
        requested = info.get("requested_downloads") or []
        for item in requested:
            fp = item.get("filepath") or item.get("_filename")
            if fp and Path(fp).exists():
                return Path(fp)
        if info.get("url") and not info.get("formats"):      # direct file, no formats
            fp = info.get("filepath")
            if fp and Path(fp).exists():
                return Path(fp)
        try:
            with yt.YoutubeDL({"skip_download": True, "quiet": True}) as probe:
                guess = probe.prepare_filename(info)
        except Exception:                                     # noqa: BLE001
            guess = ""
        if guess:
            for cand in (guess, str(Path(guess).with_suffix(".mp4"))):
                if Path(cand).exists():
                    return Path(cand)
    # last resort: the freshest mp4 written in the last few minutes by this very job
    fresh = time.time() - 300.0
    candidates = [p for p in target_dir.glob("*.mp4") if p.stat().st_mtime >= fresh]
    return max(candidates, key=lambda p: p.stat().st_mtime) if candidates else None


def best_format_url(url: str) -> dict:
    """Inspect a link without downloading: title, duration, chosen format and size."""
    ok, why = can_download(url)
    if not ok:
        raise ProcessingError("download", why)
    yt = _yt_dlp()
    opts = {
        "format": FORMAT_SELECTOR,
        "quiet": True, "no_warnings": True, "noprogress": True, "noplaylist": True,
        "skip_download": True, "logger": _Logger(), "socket_timeout": 30,
    }
    try:
        with yt.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as exc:                                 # noqa: BLE001
        log.error("Falha ao inspecionar %s: %s", url, exc)
        raise ProcessingError(
            "download",
            "Não foi possível obter as informações desse link.",
            str(exc),
        ) from exc
    if not info:
        raise ProcessingError("download", "Esse link não retornou nenhuma informação de vídeo.")
    if info.get("entries"):                                  # playlist: take the first item
        entries = [e for e in info["entries"] if e]
        if not entries:
            raise ProcessingError("download", "A playlist está vazia ou indisponível.")
        info = entries[0]

    best = {}
    if info.get("formats"):
        try:
            with yt.YoutubeDL({"format": FORMAT_SELECTOR, "quiet": True,
                               "skip_download": True}) as ydl:
                fmts, _ = ydl.sort_by_format(FORMAT_SELECTOR, info["formats"], None)
                if fmts:
                    top = fmts[0]
                    best = {
                        "format_id": top.get("format_id", ""),
                        "ext": top.get("ext", ""),
                        "height": top.get("height") or 0,
                        "width": top.get("width") or 0,
                        "fps": top.get("fps") or 0.0,
                        "vcodec": top.get("vcodec", ""),
                        "acodec": top.get("acodec", ""),
                        "url": top.get("url", ""),
                        "bitrate": top.get("tbr") or 0,
                        "filesize": top.get("filesize") or top.get("filesize_approx") or 0,
                    }
        except Exception as exc:                              # noqa: BLE001
            log.debug("Não foi possível escolher o melhor formato: %s", exc)
    size = info.get("filesize") or info.get("filesize_approx") or best.get("filesize") or 0
    return {
        "title": info.get("title", ""),
        "id": str(info.get("id", "")),
        "url": info.get("webpage_url") or url,
        "duration": float(info.get("duration") or 0.0),
        "height": int(best.get("height") or info.get("height") or 0),
        "ext": best.get("ext") or info.get("ext") or "mp4",
        "uploader": info.get("uploader") or info.get("channel") or "",
        "thumbnail": info.get("thumbnail") or "",
        "filesize": int(size or 0),
        "format": best,
        "formats_count": len(info.get("formats") or []),
    }
