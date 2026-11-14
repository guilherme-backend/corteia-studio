"""The single place where ffmpeg/ffprobe subprocesses are created.

Every other core module goes through :func:`run`, :func:`capture`, :func:`run_json`
or :func:`spawn_reader`, which gives the app one cancellation path, one progress
parser and one error format (Portuguese message + raw stderr kept in ``detail``).

Design notes
------------
* ``stderr`` is drained in a dedicated thread at all times: FFmpeg is chatty and a
  full pipe would deadlock the process.
* ``stdout`` is *buffered* by :func:`run`/``capture``/``run_json`` — never use them
  to pull binary payload (use :func:`spawn_reader` for ``pipe:1`` output).
* Progress values passed to ``on_progress`` are fractions in ``0.0 .. 1.0``
  (same convention as ``segment.end / duration`` elsewhere in the core).
  Use :func:`as_percent` when a UI needs 0..100.
* No ``shell=True`` anywhere; on Windows ``CREATE_NO_WINDOW`` hides the console.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import threading
import time
from collections import deque
from pathlib import Path
from typing import Callable, Iterable, Iterator

from ..hardware import find_ffmpeg, find_ffprobe
from ..models import format_ts
from .errors import CancelledError, FFmpegMissingError, MediaError

log = logging.getLogger("corteia")

ProgressFn = Callable[[float, str], None]

#: seconds to wait after ``terminate()`` before ``kill()``
KILL_GRACE = 1.5
#: how many stderr lines are kept for error reporting
TAIL_LINES = 200
#: number of stderr lines shown to the user on failure
ERROR_LINES = 15

# --------------------------------------------------------------------------- #
# binaries
# --------------------------------------------------------------------------- #
def ffmpeg_bin() -> str:
    """Absolute path to the ffmpeg executable (``FFmpegMissingError`` if absent)."""
    b = find_ffmpeg()
    if not b:
        raise FFmpegMissingError()
    return b


def ffprobe_bin() -> str:
    """Absolute path to the ffprobe executable (``FFmpegMissingError`` if absent)."""
    b = find_ffprobe()
    if not b:
        # an ffmpeg-only bundle is still usable: ffprobe ships next to it
        ff = find_ffmpeg()
        if ff:
            sib = os.path.join(os.path.dirname(ff), "ffprobe.exe" if os.name == "nt" else "ffprobe")
            if os.path.isfile(sib):
                return sib
        raise FFmpegMissingError(
            "FFprobe não encontrado. Instale o FFmpeg completo ou informe o caminho "
            "em Configurações → FFmpeg."
        )
    return b


def available() -> bool:
    """True when both ffmpeg and ffprobe can be located."""
    return bool(find_ffmpeg()) and bool(find_ffprobe())


def as_percent(p: float) -> int:
    """Clamp a 0..1 fraction to a 0..100 integer for progress bars."""
    try:
        return max(0, min(100, int(round(float(p) * 100.0))))
    except (TypeError, ValueError):
        return 0


def _base_args() -> list[str]:
    """Flags shared by every invocation: no stdin, no banner, no colour."""
    return ["-hide_banner", "-nostdin", "-loglevel", "info", "-progress", "pipe:2"]


def _win_flags() -> dict:
    if os.name == "nt":
        flag = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        return {"creationflags": flag} if flag else {}
    return {}


def _resolve(cmd: Iterable[str]) -> list[str]:
    """Normalise ``cmd``: Path -> str, bare ``ffmpeg``/``ffprobe`` -> real binary."""
    args = [str(a) for a in cmd]
    if not args:
        raise MediaError("ffmpeg", "Nenhum comando informado ao FFmpeg.")
    head = os.path.basename(args[0]).lower()
    if head in ("ffmpeg", "ffmpeg.exe"):
        args[0] = ffmpeg_bin()
    elif head in ("ffprobe", "ffprobe.exe"):
        args[0] = ffprobe_bin()
    return args


def _popen(cmd: list[str], stdout: int) -> subprocess.Popen:
    try:
        return subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=subprocess.PIPE,
            bufsize=0,
            **_win_flags(),
        )
    # ----------------------------------------------------------------- #
    except OSError as exc:                                # missing binary, perms
        raise MediaError(
            "ffmpeg",
            "Não foi possível iniciar o FFmpeg. Verifique o caminho nas Configurações.",
            str(exc),
        ) from exc


# --------------------------------------------------------------------------- #
# progress parsing
# --------------------------------------------------------------------------- #
_RE_OUT_TIME = re.compile(r"out_time_(?:us|ms)\s*=\s*(\d+)")
_RE_HMS = re.compile(r"(?:^|[\s=])time\s*=\s*(\d{1,3}):(\d{1,2}):(\d{1,2})(?:[.,](\d{1,4}))?")
_RE_FRAME = re.compile(r"\bframe\s*=\s*(\d+)")
_RE_FPS = re.compile(r"(\d+(?:\.\d+)?)\s*(?:fps|tb)/")
_RE_INPUT_FPS = re.compile(r"Stream #\d+[:/]\d+.*?(\d+(?:\.\d+)?)\s+fps")
_RE_SPEED = re.compile(r"speed\s*=\s*([0-9.]+)x")


def _hms_to_seconds(line: str) -> float | None:
    m = _RE_HMS.search(line)
    if not m:
        return None
    h, mi, s, frac = m.groups()
    return int(h) * 3600 + int(mi) * 60 + int(s) + (float(f"0.{frac}") if frac else 0.0)


def extract_seconds(line: str) -> float | None:
    """Decode the media position encoded in one FFmpeg status line."""
    m = _RE_OUT_TIME.search(line)
    if m:
        # out_time_ms carries microseconds in every ffmpeg build still supported
        return int(m.group(1)) / 1_000_000.0
    return _hms_to_seconds(line)


def parse_progress_line(line: str, total: float) -> float | None:
    """Return the 0..1 fraction encoded in ``line`` (or ``None`` if it has none).

    Understands ``out_time_ms=``/``out_time_us=``/``time=HH:MM:SS.ms`` and, when a
    frame rate has already been seen, ``frame= N``.
    """
    if total <= 0:
        return None
    secs = extract_seconds(line)
    if secs is None:
        m = _RE_FRAME.search(line)
        fps = _RE_INPUT_FPS.search(line) or _RE_FPS.search(line)
        if m and fps:
            rate = float(fps.group(1))
            if rate > 0:
                secs = int(m.group(1)) / rate
    if secs is None:
        return None
    return max(0.0, min(1.0, secs / total))


class _Tracker:
    """Turns a stream of stderr lines into monotonic progress callbacks."""

    def __init__(self, total_duration: float | None, on_progress: ProgressFn | None) -> None:
        self.total = float(total_duration) if total_duration and total_duration > 0 else 0.0
        self.cb = on_progress
        self.fps: float | None = None
        self.last: float = -1.0
        self.seconds: float = 0.0

    def feed(self, line: str) -> None:
        if self.cb is None or not line:
            return
        if self.fps is None:
            fm = _RE_INPUT_FPS.search(line)
            if fm:
                try:
                    rate = float(fm.group(1))
                except ValueError:
                    rate = 0.0
                if rate > 0:
                    self.fps = rate
        secs = extract_seconds(line)
        if secs is None:
            m = _RE_FRAME.search(line)
            if not m:
                return
            if self.fps:
                secs = int(m.group(1)) / self.fps
            elif self.total <= 0:
                # frame counter without a known rate: only refresh the status text
                self.cb(max(0.0, self.last), self._label(line))
                return
        if secs is None:
            return
        self.seconds = secs
        if self.total <= 0:
            return
        p = max(0.0, min(1.0, secs / self.total))
        if p > self.last + 0.0019:
            self.last = p
            try:
                self.cb(p, self._label(line))
            except Exception:                              # noqa: BLE001 - UI must not kill the render
                log.exception("on_progress callback failed")

    def _label(self, line: str) -> str:
        if self.total:
            return f"{format_ts(self.seconds)} / {format_ts(self.total)}"
        sp = _RE_SPEED.search(line)
        return f"Velocidade {sp.group(1)}x" if sp else line[:80]


class _StderrWatcher(threading.Thread):
    """Reads a binary stderr pipe, decodes FFmpeg's ``\\r`` status lines, keeps a tail."""

    def __init__(self, stream, tracker: _Tracker | None = None,
                 on_line: Callable[[str], None] | None = None) -> None:
        super().__init__(daemon=True, name="ffmpeg-stderr")
        self._stream = stream
        self._tracker = tracker
        self._on_line = on_line
        self.lines: deque[str] = deque(maxlen=TAIL_LINES)
        self._buf = b""

    def run(self) -> None:                                 # pragma: no cover - thread body
        while True:
            try:
                chunk = self._stream.read(8192)
            except (ValueError, OSError):
                break
            if not chunk:
                break
            self._buf += chunk
            while True:
                nl, cr = self._buf.find(b"\n"), self._buf.find(b"\r")
                if nl < 0 and cr < 0:
                    break
                cut = min(x for x in (nl, cr) if x >= 0)
                piece = self._buf[:cut]
                self._buf = self._buf[cut + 1:]
                self._emit(piece)
        if self._buf:
            self._emit(self._buf)
            self._buf = b""

    def _emit(self, raw: bytes) -> None:
        line = raw.decode("utf-8", "replace").strip()
        if not line:
            return
        self.lines.append(line)
        if self._tracker is not None:
            self._tracker.feed(line)
        if self._on_line is not None:
            try:
                self._on_line(line)
            except Exception:                              # noqa: BLE001
                log.exception("stderr line callback failed")

    def tail(self, n: int = ERROR_LINES) -> str:
        """Last ``n`` lines, joined — the technical detail of a failure."""
        items = list(self.lines)[-n:] if n > 0 else list(self.lines)
        return "\n".join(items)


# --------------------------------------------------------------------------- #
# cancellation helpers
# --------------------------------------------------------------------------- #
def _terminate(proc: subprocess.Popen, grace: float = KILL_GRACE) -> int | None:
    """``terminate()`` then, after ``grace`` seconds, ``kill()``."""
    if proc.poll() is not None:
        return proc.returncode
    try:
        proc.terminate()
    except OSError:
        pass
    try:
        return proc.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        pass
    try:
        proc.kill()
    except OSError:
        pass
    try:
        return proc.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        return proc.returncode


def _check_cancel(cancel: threading.Event | None) -> None:
    if cancel is not None and cancel.is_set():
        raise CancelledError()


# --------------------------------------------------------------------------- #
# the executor
# --------------------------------------------------------------------------- #
class _Completed:
    __slots__ = ("returncode", "stdout", "stderr_tail")

    def __init__(self, returncode: int, stdout: bytes, stderr_tail: str) -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr_tail = stderr_tail


def _execute(
    cmd: list[str],
    *,
    capture_stdout: bool,
    timeout: float | None,
    cancel: threading.Event | None,
    total_duration: float | None,
    on_progress: ProgressFn | None,
    poll: float = 0.05,
) -> tuple[_Completed, _StderrWatcher]:
    args = _resolve(cmd)
    _check_cancel(cancel)
    log.debug("run: %s", " ".join(args[:8]) + (" ..." if len(args) > 8 else ""))
    proc = _popen(args, subprocess.PIPE if capture_stdout else subprocess.DEVNULL)
    tracker = _Tracker(total_duration, on_progress)
    watcher = _StderrWatcher(proc.stderr, tracker)
    watcher.start()
    started = time.monotonic()
    try:
        while True:
            rc = proc.poll()
            if rc is not None:
                break
            if cancel is not None and cancel.is_set():
                _terminate(proc)
                watcher.join(2.0)
                try:
                    proc.stdout.close()
                except (AttributeError, OSError):
                    pass
                raise CancelledError()
            if timeout is not None and (time.monotonic() - started) > timeout:
                _terminate(proc)
                watcher.join(2.0)
                raise MediaError(
                    "ffmpeg",
                    "O FFmpeg demorou demais e foi interrompido.",
                    f"Tempo limite de {timeout:.0f}s excedido.\n{watcher.tail()}",
                )
            time.sleep(poll)
    except BaseException:
        if proc.poll() is None:
            _terminate(proc)
        raise
    out = b""
    if capture_stdout:
        try:
            out = proc.stdout.read() or b""
        except (ValueError, OSError):
            out = b""
        finally:
            try:
                proc.stdout.close()
            except OSError:
                pass
    watcher.join(max(0.2, 3.0 - (time.monotonic() - started)) if timeout else 3.0)
    if proc.returncode != 0:
        tail = watcher.tail()
        raise MediaError(
            "ffmpeg",
            "O FFmpeg não conseguiu concluir a operação.",
            f"Código de saída {proc.returncode}.\n{tail}",
        )
    if total_duration and on_progress is not None:
        try:
            on_progress(1.0, tracker._label("done"))
        except Exception:                                   # noqa: BLE001
            log.exception("on_progress callback failed")
    return _Completed(proc.returncode, out, watcher.tail()), watcher


def run(
    args: list[str],
    *,
    timeout: float | None = None,
    cancel: threading.Event | None = None,
    total_duration: float | None = None,
    on_progress: ProgressFn | None = None,
) -> str:
    """Run FFmpeg/ffprobe and return **stdout** as text.

    ``total_duration`` enables percentage reporting through ``on_progress``;
    ``cancel`` terminates the child (then kills it 1.5 s later) and raises
    :class:`CancelledError`. A non-zero exit becomes a :class:`MediaError`
    carrying the last stderr lines.
    """
    done, _ = _execute(
        args, capture_stdout=True, timeout=timeout, cancel=cancel,
        total_duration=total_duration, on_progress=on_progress,
    )
    return done.stdout.decode("utf-8", "replace")


def capture(
    args: list[str],
    *,
    timeout: float | None = None,
    cancel: threading.Event | None = None,
    total_duration: float | None = None,
    on_progress: ProgressFn | None = None,
) -> tuple[str, str]:
    """Like :func:`run` but also returns the captured stderr text."""
    done, watcher = _execute(
        args, capture_stdout=True, timeout=timeout, cancel=cancel,
        total_duration=total_duration, on_progress=on_progress,
    )
    return done.stdout.decode("utf-8", "replace"), watcher.tail(TAIL_LINES)


def run_json(args: list[str]) -> dict:
    """Run a command and parse its stdout as JSON (used by ``ffprobe``)."""
    out = run(args, timeout=120.0)
    text = out.strip()
    if not text:
        raise MediaError("ffprobe", "O FFmpeg não retornou nenhuma informação do vídeo.")
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise MediaError(
            "ffprobe", "Não foi possível ler as informações do arquivo de vídeo.", str(exc)
        ) from exc


def stderr_lines(
    args: list[str],
    *,
    timeout: float | None = None,
    cancel: threading.Event | None = None,
    total_duration: float | None = None,
    on_progress: ProgressFn | None = None,
) -> list[str]:
    """Run a filter-only command and return every stderr line (e.g. silencedetect)."""
    _, tail = capture(args, timeout=timeout, cancel=cancel,
                      total_duration=total_duration, on_progress=on_progress)
    return [ln for ln in tail.splitlines() if ln.strip()]


# --------------------------------------------------------------------------- #
# streaming stdout (binary payload out of ffmpeg)
# --------------------------------------------------------------------------- #
class PipedProcess:
    """A live ffmpeg process whose binary stdout can be read in chunks."""

    def __init__(self, proc: subprocess.Popen, watcher: _StderrWatcher,
                 cmd: list[str]) -> None:
        self._proc = proc
        self._watcher = watcher
        self.cmd = cmd

    @property
    def stdout(self):
        """Binary file object attached to the child's stdout."""
        return self._proc.stdout

    @property
    def returncode(self) -> int | None:
        return self._proc.poll()

    @property
    def alive(self) -> bool:
        return self._proc.poll() is None

    def read(self, n: int = -1) -> bytes:
        return self._proc.stdout.read(n) or b""

    def iter_bytes(self, size: int) -> Iterator[bytes]:
        while True:
            chunk = self._proc.stdout.read(size)
            if not chunk:
                return
            yield chunk

    def read_exact(self, n: int) -> bytes:
        """Read exactly ``n`` bytes, or whatever is left at EOF."""
        buf = bytearray()
        while len(buf) < n:
            piece = self._proc.stdout.read(n - len(buf))
            if not piece:
                break
            buf += piece
        return bytes(buf)

    def tail(self, n: int = ERROR_LINES) -> str:
        return self._watcher.tail(n)

    def close(self, kill: bool = False) -> None:
        if self._proc.poll() is None:
            if kill:
                _terminate(self._proc, grace=0.3)
            else:
                _terminate(self._proc)
        for stream in (self._proc.stdout, self._proc.stderr):
            try:
                stream.close()
            except (AttributeError, OSError):
                pass

    def wait(self, timeout: float | None = None) -> int:
        """Join the stderr pump and validate the exit code."""
        try:
            rc = self._proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            _terminate(self._proc)
            rc = self._proc.poll()
            raise MediaError(
                "ffmpeg", "O FFmpeg demorou demais e foi interrompido.",
                f"Tempo limite de {timeout:.0f}s excedido.\n{self.tail()}",
            ) from None
        finally:
            self._watcher.join(2.0)
        if rc != 0:
            raise MediaError("ffmpeg", "O FFmpeg não conseguiu concluir a operação.",
                             f"Código de saída {rc}.\n{self.tail()}")
        return rc or 0


def spawn_reader(
    args: list[str],
    *,
    cancel: threading.Event | None = None,
    total_duration: float | None = None,
    on_progress: ProgressFn | None = None,
) -> PipedProcess:
    """Start a process for streamed stdout reading; stderr is drained in a thread."""
    cmd = _resolve(args)
    _check_cancel(cancel)
    proc = _popen(cmd, subprocess.PIPE)
    watcher = _StderrWatcher(proc.stderr, _Tracker(total_duration, on_progress))
    watcher.start()
    return PipedProcess(proc, watcher, cmd)


# --------------------------------------------------------------------------- #
# monitored job (the UI likes to hold one of these)
# --------------------------------------------------------------------------- #
class FFmpegJob:
    """A live FFmpeg process monitored in its own thread.

    ``start`` launches, ``percent`` polls, ``cancel`` stops. Errors reached from
    ``wait`` are raised as :class:`MediaError`; when ``on_error`` is given it is
    called instead of raising so a worker thread can forward it to a Qt signal.
    """

    def __init__(self) -> None:
        self._proc: subprocess.Popen | None = None
        self._watcher: _StderrWatcher | None = None
        self._tracker: _Tracker | None = None
        self._lock = threading.Lock()
        self._done = threading.Event()
        self._percent = 0.0
        self._error: str = ""
        self._pending: BaseException | None = None
        self._killed = threading.Event()
        self._on_progress: ProgressFn | None = None
        self._on_error: Callable[[BaseException], None] | None = None
        self._cancel: threading.Event | None = None
        self.cmd: list[str] = []

    # ------------------------------------------------------------------ api
    def start(
        self,
        cmd: list[str],
        *,
        total_duration: float | None = None,
        on_progress: ProgressFn | None = None,
        on_error: Callable[[BaseException], None] | None = None,
        cancel: threading.Event | None = None,
    ) -> "FFmpegJob":
        """Launch ``cmd`` in the background and return ``self``."""
        with self._lock:
            if self._proc is not None:
                raise MediaError("ffmpeg", "Este trabalho de FFmpeg já foi iniciado.")
            self._on_progress = on_progress
            self._on_error = on_error
            self._cancel = cancel
            self.cmd = _resolve(cmd)
            self._tracker = _Tracker(total_duration, self._on_tick)
            self._proc = _popen(self.cmd, subprocess.DEVNULL)
            self._watcher = _StderrWatcher(self._proc.stderr, self._tracker)
            self._watcher.start()
        threading.Thread(target=self._monitor, daemon=True, name="ffmpeg-job").start()
        return self

    def _on_tick(self, p: float, msg: str) -> None:
        self._percent = p
        if self._on_progress is not None:
            try:
                self._on_progress(p, msg)
            except Exception:                               # noqa: BLE001
                log.exception("on_progress callback failed")

    def _monitor(self) -> None:
        assert self._proc is not None and self._watcher is not None
        rc: int | None = None
        try:
            while True:
                rc = self._proc.poll()
                if rc is not None:
                    break
                if self._stop_requested():
                    _terminate(self._proc)
                    self._finish(CancelledError())
                    return
                time.sleep(0.1)
            self._watcher.join(2.0)
            if rc not in (0, None):
                if self._stop_requested():
                    self._finish(CancelledError())
                    return
                err = MediaError("ffmpeg", "O FFmpeg não conseguiu concluir a operação.",
                                 f"Código de saída {rc}.\n{self._watcher.tail()}")
                self._finish(err)
                return
            if self._tracker is not None and self._tracker.total > 0:
                self._on_tick(1.0, self._tracker._label("done"))
            self._finish(None)
        except BaseException as exc:                         # noqa: BLE001
            self._finish(exc)

    def _finish(self, err: BaseException | None) -> None:
        self._pending = err
        if err is not None and not isinstance(err, CancelledError):
            self._error = str(err)
            log.error("FFmpegJob falhou: %s", err)
            if self._on_error is not None:
                try:
                    self._on_error(err)
                except Exception:                             # noqa: BLE001
                    log.exception("on_error callback failed")
                self._pending = None      # the callback owns the failure now
        self._done.set()

    # ----------------------------------------------------------------- state
    @property
    def alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    @property
    def percent(self) -> float:
        """Progress fraction 0..1 (``as_percent(job.percent)`` for 0..100)."""
        return self._percent

    @property
    def error(self) -> str:
        return self._error

    @property
    def cancelled(self) -> bool:
        return self._stop_requested()

    def _stop_requested(self) -> bool:
        """True when ``cancel()`` was called or the caller's event was set."""
        if self._killed.is_set():
            return True
        return self._cancel is not None and self._cancel.is_set()

    def cancel(self) -> None:
        """Terminate the child; ``wait`` then raises :class:`CancelledError`."""
        self._killed.set()
        if self._cancel is not None:
            self._cancel.set()
        if self._proc is not None and self._proc.poll() is None:
            _terminate(self._proc)

    def wait(self, timeout: float | None = None) -> int:
        """Block until the process ends; returns the exit code."""
        if self._proc is None:
            raise MediaError("ffmpeg", "Nenhum processo de FFmpeg foi iniciado.")
        if not self._done.wait(timeout=timeout):
            raise MediaError("ffmpeg", "O FFmpeg demorou demais e foi interrompido.",
                             f"Tempo limite de {timeout:.0f}s excedido.")
        if self._stop_requested():
            raise CancelledError()
        err = self._pending
        if err is not None:
            raise err
        return self._proc.returncode or 0


# --------------------------------------------------------------------------- #
# command builders
# --------------------------------------------------------------------------- #
def encode_cmd(
    inputs: list[str],
    global_opts: list[str],
    vfilter: str | None,
    afilter: str | None,
    out: Path,
    *,
    codec: str,
    crf: int,
    preset: str,
    fps: float | int,
    audio_bitrate: str,
    extra: list[str] | None = None,
    threads: int = 0,
) -> list[str]:
    """Build an encoding command line (no ``shell`` — a plain argv list).

    Items in ``inputs`` are file paths, except items starting with ``-`` which are
    inserted verbatim, so pre-input options such as ``-ss 3`` are expressible.
    """
    cmd: list[str] = [ffmpeg_bin(), *_base_args()]
    cmd += [str(g) for g in (global_opts or []) if str(g)]
    for item in inputs or []:
        text = str(item)
        if text.startswith("-"):
            cmd.append(text)
        else:
            cmd += ["-i", text]
    cmd += ["-y"]

    if vfilter:
        cmd += ["-vf", vfilter]
    if afilter:
        cmd += ["-af", afilter]

    codec = str(codec or "libx264")
    cmd += ["-c:v", codec]
    if "nvenc" in codec:
        cmd += ["-rc", "vbr", "-cq", str(int(crf)), "-preset", "p4" if preset in ("fast", "medium") else "p5"]
    elif "qsv" in codec:
        cmd += ["-global_quality", str(int(crf)), "-preset", "fast"]
    elif "videotoolbox" in codec:
        cmd += ["-q:v", str(max(1, min(62, int(crf))))]
    elif "amf" in codec:
        cmd += ["-qp_i", str(int(crf)), "-qp_p", str(int(crf)), "-quality", "balanced"]
    else:                                                   # libx264 / libx265 / mpeg4
        cmd += ["-crf", str(int(crf)), "-preset", str(preset or "fast")]
    if fps:
        cmd += ["-r", f"{float(fps):g}"]
    cmd += ["-c:a", "aac", "-b:a", str(audio_bitrate or "192k")]
    if threads and threads > 0:
        cmd += ["-threads", str(int(threads))]
    cmd += [str(x) for x in (extra or [])]
    cmd.append(str(out))
    return cmd


def copy_cmd(inputs: list[str], out: Path, *, start: float | None = None,
             end: float | None = None, extra: list[str] | None = None,
             fast: bool = False) -> list[str]:
    """Stream-copy (no re-encode) cut command.

    ``fast=True`` seeks before ``-i``: instant, but the cut snaps to the previous
    keyframe (verified: 5→11 s came back as 11.0 s on a 10 s GOP file). The default
    puts ``-ss``/``-to`` after the input, which keeps the container duration exact
    (same file: 5.99 s) at the cost of decoding up to the start point.
    """
    cmd: list[str] = [ffmpeg_bin(), *_base_args(), "-y"]
    if start is not None and fast:
        cmd += ["-ss", f"{max(0.0, float(start)):.3f}"]
    for item in inputs or []:
        text = str(item)
        cmd += [text] if text.startswith("-") else ["-i", text]
    if start is not None and not fast:
        cmd += ["-ss", f"{max(0.0, float(start)):.3f}"]
    if end is not None and start is not None:
        cmd += (["-t", f"{max(0.0, float(end) - float(start)):.3f}"] if fast
                else ["-to", f"{max(0.0, float(end)):.3f}"])
    elif end is not None:
        cmd += ["-to", f"{max(0.0, float(end)):.3f}"]
    cmd += ["-map", "0", "-c", "copy", "-avoid_negative_ts", "make_zero"]
    cmd += [str(x) for x in (extra or [])]
    cmd.append(str(out))
    return cmd


def silence_detect_cmd(path: Path, db: float, min_duration_s: float) -> list[str]:
    """Command that prints ``silencedetect`` events on stderr."""
    return [
        ffmpeg_bin(), *_base_args(), "-i", str(path),
        "-af", f"silencedetect=n={db:.1f}dB:d={max(0.05, float(min_duration_s)):.3f}",
        "-f", "null", "-",
    ]
