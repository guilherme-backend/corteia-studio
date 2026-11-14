"""Rendering: turn a :class:`Clip` into a real MP4 with a single FFmpeg pass.

The graph is built as *blocks*.  A block is the intersection of a kept time
range (after silence removal) with a crop window (from the face tracker), so
each block only needs a static ``crop``+``scale`` — the reliable way to do auto
reframing in FFmpeg, instead of an unreadable ``zoompan`` expression.  Blocks
are joined with ``concat``, the ASS is burned on top, and the audio chain
(volume, optional ducked music bed, fades) is mixed in the same encode.

The graph is always written to ``-filter_complex_script``: Windows caps the
command line near 32 k characters and long graphs blow through it.

Progress convention: ``progress(fraction_0_to_1, label)`` — same as the rest of
:mod:`corteia.core`; the UI multiplies by 100 at its own boundary.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

from .. import paths
from ..models import (AspectRatio, Clip, CropPlan, ExportSettings, SubtitleStyle,
                      Transcript, VideoInfo)
from . import fonts as F
from . import ffmpeg_cli as FC
from . import subtitles as SUB
from .errors import RenderError
from .timeline import TimelineMap

log = logging.getLogger("corteia.render")

MAX_BLOCKS = 80
FADE = 0.08
EPS = 0.02


# --------------------------------------------------------------------------- #
# quoting
# --------------------------------------------------------------------------- #
def filter_path(path: Path | str) -> str:
    """A path usable inside a filtergraph value, on Windows and POSIX.

    Forward slashes (``\\`` is an escape character in the filtergraph parser) and
    ``\\:`` for the drive colon, because ``:`` separates filter options.
    """
    p = Path(path).as_posix()
    if re.match(r"^[A-Za-z]:", p):
        p = p[0] + "\\:" + p[2:]
    return p


def quoted(path: Path | str) -> str:
    return f"'{filter_path(path)}'"


def text_escape(text: str) -> str:
    """Prepare free text for ``drawtext=text='…'``.

    The filtergraph protects everything inside single quotes, so ``:`` and ``,``
    need no escaping — only the quote characters themselves, backslashes and
    newlines would break it, and ``%`` is expandable by drawtext.
    """
    out = re.sub(r"[\\\r\n\t]+", " ", text or "")
    out = out.replace("'", "\u2019").replace('"', "\u201d").replace("%", "\\%")
    return re.sub(r"\s{2,}", " ", out).strip()


# --------------------------------------------------------------------------- #
# blocks
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class Block:
    start: float
    end: float
    crop: tuple[int, int, int, int] | None = None

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


def crop_segments(plan: CropPlan | None, t0: float, t1: float) -> list[tuple[float, float, tuple[int, int, int, int]]]:
    """Normalise a crop plan into ``(start, end, (x, y, w, h))`` windows."""
    if plan is None or not plan.points:
        return []
    segs: list[tuple[float, float, tuple[int, int, int, int]]] = []
    pts = sorted(plan.points, key=lambda p: p[0])
    for i, (t, x, y, w, h) in enumerate(pts):
        nxt = pts[i + 1][0] if i + 1 < len(pts) else max(t1, t)
        a, b = max(t0, float(t)), min(t1, float(max(nxt, t + EPS)))
        if b > a + 0.005:
            segs.append((a, b, (int(x), int(y), int(w), int(h))))
    if segs and segs[0][0] > t0 + 0.005:
        segs.insert(0, (t0, segs[0][0], segs[0][2]))
    if segs and segs[-1][1] < t1 - 0.005:
        segs.append((segs[-1][1], t1, segs[-1][2]))
    return [(a, b, c) for a, b, c in segs if b > a + 0.005]


def plan_blocks(tmap: TimelineMap, plan: CropPlan | None, info: VideoInfo,
                *, fps: float, max_blocks: int = MAX_BLOCKS) -> list[Block]:
    """Intersect kept ranges with crop windows, merging if the graph gets huge."""
    ranges = [(a, b) for a, b in tmap.ranges if b > a + EPS]
    if not ranges:
        raise RenderError("render", "não há trecho aproveitável para renderizar",
                          f"ranges vazios: {tmap}")
    a0, b1 = ranges[0][0], ranges[-1][1]
    crops = crop_segments(plan, a0, b1)
    blocks: list[Block] = []
    for a, b in ranges:
        if not crops:
            blocks.append(Block(a, b, None))
            continue
        for ca, cb, crop in crops:
            lo, hi = max(a, ca), min(b, cb)
            if hi - lo > max(EPS, 1.0 / max(1.0, fps) * 1.5):
                blocks.append(Block(lo, hi, crop))
    if not blocks:
        blocks = [Block(a0, b1, crops[0][2] if crops else None)]
    blocks.sort(key=lambda x: x.start)
    # Close sub-frame seams between *neighbouring* blocks only.  Blindly making
    # every block end where the next begins would re-insert the silence ranges
    # we just removed, which is the opposite of what the user asked for.
    step = 1.0 / max(1.0, fps)
    fixed: list[Block] = []
    for i, blk in enumerate(blocks):
        start, end = round(blk.start, 3), round(blk.end, 3)
        if i + 1 < len(blocks):
            nxt = round(blocks[i + 1].start, 3)
            if 0.0 <= nxt - end < step * 1.5:
                end = nxt
        if end - start < step:
            end = round(start + step, 3)
        fixed.append(Block(start, end, blk.crop))
    blocks = [b for b in fixed if b.duration > 0]
    while len(blocks) > max_blocks:
        blocks.sort(key=lambda b: b.duration)
        j = next((k for k in range(len(blocks) - 1)
                  if blocks[k].crop == blocks[k + 1].crop), 0)
        blocks[j] = Block(blocks[j].start, blocks[j + 1].end, blocks[j].crop)
        del blocks[j + 1]
    return sorted(blocks, key=lambda b: b.start)


def _even(n: float) -> int:
    return max(2, int(round(n)) // 2 * 2)


# --------------------------------------------------------------------------- #
# block filters
# --------------------------------------------------------------------------- #
def fit_filter(crop: tuple[int, int, int, int] | None, src: VideoInfo, out_w: int,
               out_h: int, *, index: int, label_in: str, mode: str = "auto") -> list[str]:
    """Normalise one block to exactly ``out_w``×``out_h``.

    The tracker's ``crop`` is applied first.  With no crop and a mismatched
    ratio, ``auto`` chooses a blurred background fill rather than stretching the
    picture or adding black bars nobody asked for.
    """
    # A pad reference must always be bracketed, or FFmpeg reads it as a filter
    # name ("rawv0setpts=…" → "no such filter").
    if not label_in.startswith("["):
        label_in = f"[{label_in}]"
    chain = f"{label_in}setpts=PTS-STARTPTS"
    w = h = 0
    if crop:
        x, y, w, h = crop
        w, h = _even(w), _even(h)
        x = max(0, min(int(x), max(0, src.width - w)))
        y = max(0, min(int(y), max(0, src.height - h)))
        chain += f",crop=w={w}:h={h}:x={x}:y={y}"
    src_ar = (w / h) if crop else (src.width / max(1, src.height))
    tgt_ar = out_w / out_h
    mismatch = abs(src_ar - tgt_ar) / tgt_ar > 0.02
    effective = mode
    if mode == "auto":
        effective = "blur" if (mismatch and not crop) else "cover"
    tail = ",format=yuv420p,setsar=1"
    if effective == "blur" and mismatch:
        return [
            f"{label_in}setpts=PTS-STARTPTS,split=2[bg{index}],null[fg{index}]",
            (f"[bg{index}]scale={out_w}:{out_h}:force_original_aspect_ratio=increase:"
             f"flags=lanczos,crop={out_w}:{out_h},gblur=sigma=22,"
             f"eq=brightness=-0.05:saturation=0.75[bgb{index}]"),
            (f"[fg{index}]scale={out_w}:{out_h}:force_original_aspect_ratio=decrease:"
             f"flags=lanczos[fgs{index}]"),
            (f"[bgb{index}][fgs{index}]overlay=x=(W-w)/2:y=(H-h)/2:format=auto"
             f"{tail}[vb{index}]"),
        ]
    if effective == "cover":
        chain += (f",scale={out_w}:{out_h}:force_original_aspect_ratio=increase:"
                  f"flags=lanczos,crop={out_w}:{out_h}")
    else:
        chain += (f",scale={out_w}:{out_h}:force_original_aspect_ratio=decrease:"
                  f"flags=lanczos,pad={out_w}:{out_h}:(ow-iw)/2:(oh-ih)/2:color=black")
    return [chain + tail + f"[vb{index}]"]


def video_graph(blocks: list[Block], src: VideoInfo, out_w: int, out_h: int,
                *, fit_mode: str = "auto") -> list[str]:
    """Per-block ``trim`` → fit, joined with ``concat`` into ``[vc]``."""
    lines: list[str] = []
    for i, blk in enumerate(blocks):
        lines.append(f"[0:v]trim=start={blk.start:.3f}:end={blk.end:.3f}[rawv{i}]")
        lines.extend(fit_filter(blk.crop, src, out_w, out_h, index=i,
                                label_in=f"rawv{i}", mode=fit_mode))
    if len(blocks) == 1:
        lines.append("[vb0]null[vc]")
    else:
        chain = "".join(f"[vb{i}]" for i in range(len(blocks)))
        lines.append(f"{chain}concat=n={len(blocks)}:v=1:a=0[vc]")
    return lines


def audio_graph(blocks: list[Block]) -> list[str]:
    """Per-block ``atrim`` joined into ``[ac]`` (silence-cut audio included)."""
    lines: list[str] = []
    for i, blk in enumerate(blocks):
        lines.append(f"[0:a]atrim=start={blk.start:.3f}:end={blk.end:.3f},"
                     f"asetpts=PTS-STARTPTS[ab{i}]")
    if len(blocks) == 1:
        lines.append("[ab0]anull[ac]")
    else:
        chain = "".join(f"[ab{i}]" for i in range(len(blocks)))
        lines.append(f"{chain}concat=n={len(blocks)}:v=0:a=1[ac]")
    return lines


def drawtext(text: str, *, size: int, x: str, y: str, color: str = "white",
             alpha: float = 1.0, outline: int = 0, bold: bool = False) -> str:
    """One ``drawtext`` filter, with a font that actually exists on this machine."""
    ff = F.file_for("Arial Black" if bold else "Arial", bold=bold)
    font_part = f"fontfile={quoted(ff)}:" if ff else f"font='{F.pick('Segoe UI')[0]}':"
    col = f"{color}@{max(0.05, min(1.0, alpha)):.2f}" if alpha < 1.0 else color
    s = (f"drawtext={font_part}text='{text_escape(text)}':fontsize={max(12, int(size))}"
         f":fontcolor={col}:x={x}:y={y}")
    s += f":borderw={outline}:bordercolor=black@0.7" if outline else ":borderw=0"
    return s


def stamp_xy(pos: str, out_w: int, out_h: int, obj_w: int, obj_h: int,
             *, text: bool = False) -> tuple[str, str]:
    """Corner coordinates as filter expressions.

    ``overlay`` and ``drawtext`` use different variables: in ``overlay``, ``w``/``h``
    are the *overlay* size, while in ``drawtext`` ``w``/``h`` are the *video* size
    and the text box is ``text_w``/``text_h``.  Mixing them up silently pushes the
    watermark off screen (``x=W-w-m`` → negative), hence the ``text`` flag.
    """
    m = max(8, int(out_w * 0.045))
    p = (pos or "br").lower()
    ow = "text_w" if text else "w"
    oh = "text_h" if text else "h"
    x = {
        "tl": f"{m}", "tr": f"{out_w}-{ow}-{m}", "bl": f"{m}", "br": f"{out_w}-{ow}-{m}",
        "c": f"({out_w}-{ow})/2", "b": f"({out_w}-{ow})/2", "t": f"({out_w}-{ow})/2",
    }.get(p, f"{out_w}-{ow}-{m}")
    y = {
        "tl": f"{m}", "tr": f"{m}", "bl": f"{out_h}-{oh}-{m}", "br": f"{out_h}-{oh}-{m}",
        "c": f"({out_h}-{oh})/2", "b": f"{out_h}-{oh}-{m}", "t": f"{m}",
    }.get(p, f"{out_h}-{oh}-{m}")
    return x, y


# --------------------------------------------------------------------------- #
# overlays
# --------------------------------------------------------------------------- #
def overlay_graph(label: str, *, ass: Path | None, clip_w: int, clip_h: int,
                  watermark_image: Path | None, watermark_text: str, wm_pos: str,
                  wm_opacity: float, wm_scale: float, title_text: str,
                  title_style: str, wm_input_index: int | None) -> tuple[list[str], str]:
    """Append the burn-in chain (subtitles, watermark, title) to ``lines``."""
    lines: list[str] = []
    if ass is not None and ass.exists():
        lines.append(f"[{label}]ass={quoted(ass)}[vsub]")
        label = "vsub"

    if (watermark_image is not None and Path(watermark_image).is_file()
            and wm_input_index is not None):
        w = _even(max(24, int(clip_w * max(0.05, min(0.6, wm_scale)))))
        a = max(0.05, min(1.0, wm_opacity))
        x, y = stamp_xy(wm_pos, clip_w, clip_h, w, int(w * 0.6))
        lines.append(f"[{wm_input_index}:v]scale={w}:-1,format=rgba,"
                     f"colorchannelmixer=aa={a:.2f}[wmg]")
        lines.append(f"[{label}][wmg]overlay=x={x}:y={y}:format=auto[vwm]")
        label = "vwm"
    elif (watermark_text or "").strip():
        size = max(14, int(clip_h * 0.028))
        x, y = stamp_xy(wm_pos, clip_w, clip_h, 1, size)
        x, y = stamp_xy(wm_pos, clip_w, clip_h, 1, size, text=True)
        dt = drawtext(watermark_text, size=size, x=str(x), y=str(y),
                      alpha=max(0.05, min(1.0, wm_opacity)), outline=2)
        lines.append(f"[{label}]{dt}[vwm]")
        label = "vwm"

    if (title_text or "").strip() and title_style != "none":
        size = max(20, int(clip_h * 0.040))
        y_top = int(clip_h * 0.055)
        if title_style == "bar":
            bar_h = size + int(size * 0.95)
            lines.append(f"[{label}]drawbox=x=0:y={y_top}:w={clip_w}:h={bar_h}:"
                         f"color=0x0E0F14@0.72:t=fill[vtb]")
            label = "vtb"
            dt = drawtext(title_text, size=size, x=str(max(10, int(clip_w * 0.05))),
                          y=str(y_top + int(size * 0.5)), bold=True)
        else:
            dt = drawtext(title_text, size=size, x="(W-text_w)/2",
                          y=str(int(clip_h * 0.07)), outline=3, bold=True)
        lines.append(f"[{label}]{dt}[vtl]")
        label = "vtl"
    return lines, label


def audio_post_graph(label: str, *, duration: float, volume: float,
                     music_index: int | None, music_volume: float,
                     duck: bool, out: str = "aout") -> list[str]:
    """Fade, level and optional ducked music bed for the concat audio."""
    fade_out = max(0.05, min(FADE, duration / 3.0))
    parts = [f"volume={max(0.0, float(volume)):.3f}"]
    if duration > fade_out * 2.5:
        parts.append(f"afade=t=in:st=0:d={FADE:.3f}")
        parts.append(f"afade=t=out:st={max(0.0, duration - fade_out):.3f}:d={fade_out:.3f}")
    lines = [f"[{label}]{','.join(parts)}[vv]"]
    cur = "vv"
    if music_index is not None:
        mus = f"[{music_index}:a]aformat=sample_rates=44100:channel_layouts=stereo," \
              f"volume={max(0.0, float(music_volume)):.3f}[mbed]"
        lines.append(mus)
        if duck:
            # `[vv]` era consumido duas vezes (uma como chave do compressor e
            # outra como entrada do mix) -- o FFmpeg respondia "matches no
            # streams" e a exportação com trilha sonora nunca saía.  A voz é
            # dividida com asplit: uma cópia é a chave, a outra vai ao mix.
            lines.append("[vv]asplit=2[vvoice][vkey]")
            lines.append("[mbed][vkey]sidechaincompress=threshold=0.06:ratio=7:"
                         "attack=25:release=380:makeup=1.15[mduck]")
            lines.append("[vvoice][mduck]amix=inputs=2:duration=first:"
                         "dropout_transition=0:normalize=0,alimiter=limit=0.95[aed]")
            cur = "aed"
        else:
            lines.append("[vv][mbed]amix=inputs=2:duration=first:"
                         "dropout_transition=0:normalize=0,alimiter=limit=0.95[aed]")
            cur = "aed"
    lines.append(f"[{cur}]anull[{out}]")
    return lines


def build_graph(blocks: list[Block], *, src: VideoInfo, out_w: int, out_h: int,
                has_audio: bool, ass: Path | None, clip_w: int, clip_h: int,
                edit, volume: float, music_index: int | None, duration: float,
                fit_mode: str, wm_index: int | None) -> str:
    """Full filter_complex script text."""
    lines = video_graph(blocks, src, out_w, out_h, fit_mode=fit_mode)
    v_lines, v_label = overlay_graph("vc", ass=ass, clip_w=clip_w, clip_h=clip_h,
                                     watermark_image=Path(edit.watermark_path)
                                     if edit.watermark_path else None,
                                     watermark_text=edit.watermark_text,
                                     wm_pos=edit.watermark_pos,
                                     wm_opacity=edit.watermark_opacity,
                                     wm_scale=edit.watermark_scale,
                                     title_text=edit.title_text,
                                     title_style=edit.title_style,
                                     wm_input_index=wm_index)
    lines += v_lines
    lines.append(f"[{v_label}]null[vout]")
    if has_audio:
        lines += audio_graph(blocks)
        lines += audio_post_graph("ac", duration=duration, volume=volume,
                                 music_index=music_index,
                                 music_volume=edit.music_volume, duck=edit.duck_music)
    return ";\n".join(lines) + ";\n"


# --------------------------------------------------------------------------- #
# public API
# --------------------------------------------------------------------------- #
def output_size(settings: ExportSettings, aspect: AspectRatio | None) -> tuple[int, int]:
    """``(w, h)`` of the produced file, honouring the clip's own aspect choice."""
    import dataclasses

    if aspect is None or aspect is settings.aspect:
        return settings.target_size
    # dataclasses.replace keeps every other field as a real object; the old
    # ExportSettings(**settings.to_dict()) clone turned the enums into plain
    # strings, and target_size then died with "'str' object has no attribute
    # 'short_side'".
    return dataclasses.replace(settings, aspect=aspect).target_size


def render_clip(clip: Clip, *, source: Path, info: VideoInfo, transcript: Transcript | None,
                settings: ExportSettings, plan: CropPlan | None, tmap: TimelineMap,
                out: Path, progress=None, cancel=None, threads: int = 0,
                proxy: bool = False, ass_path: Path | None = None,
                fit_mode: str = "auto") -> Path:
    """Encode one clip.  Raises :class:`RenderError` with FFmpeg's own words.

    ``proxy`` produces a small preview file (480p, faster preset, short) used by
    the editor's "render preview" button; the graph is otherwise identical, so
    what the user sees is what the export will look like.
    """
    source = Path(source)
    out = Path(out)
    if not source.exists():
        raise RenderError("render", f"o vídeo de origem não existe mais: {source.name}",
                          str(source))
    out_w, out_h = output_size(settings, clip.edit.aspect or settings.aspect)
    if proxy:
        out_w, out_h = _proxy_size(out_w, out_h)

    duration = tmap.duration

    if ass_path is None and transcript is not None and clip.edit.subtitles is not None \
            and clip.edit.subtitles.style is not SubtitleStyle.NONE:
        ass_path = paths.temp_file(".ass", "subs")
        SUB.build_ass(transcript, clip.start, clip.end, clip.edit.subtitles, ass_path,
                      clip_w=out_w, clip_h=out_h, edits=clip.edit.manual_text_edits,
                      tmap=tmap)

    # FFmpeg numbers inputs by order of appearance, not by argv position.
    inputs = ["-i", str(source)]
    music_index = wm_index = None
    next_index = 1
    if clip.edit.music_path and Path(clip.edit.music_path).is_file():
        inputs += ["-stream_loop", "-1", "-i", str(clip.edit.music_path)]
        music_index = next_index
        next_index += 1
    if clip.edit.watermark_path and Path(clip.edit.watermark_path).is_file():
        inputs += ["-i", str(clip.edit.watermark_path)]
        wm_index = next_index
        next_index += 1

    graph = build_graph(plan_blocks(tmap, plan, info, fps=settings.fps), src=info,
                        out_w=out_w, out_h=out_h, has_audio=info.has_audio,
                        ass=ass_path, clip_w=out_w, clip_h=out_h, edit=clip.edit,
                        volume=clip.edit.volume, music_index=music_index,
                        duration=duration, fit_mode=fit_mode, wm_index=wm_index)
    script = paths.temp_file(".txt", "graphs")
    script.write_text(graph, encoding="utf-8")

    codec = getattr(settings.codec, "value", None) or str(settings.codec)
    if codec in ("Codec.H264", "Codec.H265"):
        codec = codec.split(".")[-1].lower()
    if codec == "libx265" and "libx265" not in _encoders():
        codec = "libx264"
    if codec == "libx264" and "libx264" not in _encoders():
        codec = "mpeg4"
    extra = ["-filter_complex_script", str(script)]
    maps = ["-map", "[vout]"]
    if info.has_audio:
        maps += ["-map", "[aout]"]
    else:
        extra.append("-an")
    preset = "veryfast" if proxy else settings.preset
    crf = 30 if proxy else settings.quality_crf
    cmd = [FC.ffmpeg_bin(), "-hide_banner", "-nostdin", "-y", *inputs,
           *extra, *maps,
           "-c:v", codec, "-preset", preset, "-crf", str(crf),
           "-pix_fmt", "yuv420p", "-r", str(settings.fps),
           "-colorspace", "bt709", "-color_primaries", "bt709",
           "-color_trc", "bt709", "-movflags", "+faststart",
           "-metadata", f"title={clip.title or 'CorteIA'}",
           "-metadata", f"comment=corte {int(clip.start)}-{int(clip.end)} do arquivo {source.name}"]
    if info.has_audio:
        cmd += ["-c:a", "aac", "-b:a", settings.audio_bitrate, "-ar", "48000", "-ac", "2"]
    if threads:
        cmd += ["-threads", str(int(threads))]
    cmd.append(str(out))

    def cb(frac: float, msg: str) -> None:
        if progress:
            progress(min(0.999, max(0.0, frac)), msg)

    try:
        FC.run(cmd, total_duration=duration, on_progress=cb, cancel=cancel)
    except Exception as exc:
        hint = _explain_failure(str(exc))
        raise RenderError("render", hint, str(exc)[:1200]) from exc
    finally:
        if ass_path and ass_path.exists() and proxy:
            ass_path.unlink(missing_ok=True)
        script.unlink(missing_ok=True)
    if not out.exists() or out.stat().st_size < 1024:
        raise RenderError("render", "o arquivo final não foi gerado (saída vazia)",
                          f"out={out} size={out.stat().st_size if out.exists() else 'missing'}")
    return out


def _proxy_size(w: int, h: int) -> tuple[int, int]:
    if h >= w:                       # portrait
        return _even(480 * w / h), 480
    return 854, _even(480 * h / w)


_ENCODERS: list[str] | None = None


def _encoders() -> list[str]:
    global _ENCODERS
    if _ENCODERS is None:
        try:
            from ..hardware import probe
            _ENCODERS = probe().video_encoders
        except Exception:                                 # noqa: BLE001
            _ENCODERS = ["libx264"]
    return _ENCODERS


def _explain_failure(detail: str) -> str:
    d = (detail or "").lower()
    if "subtitles" in d and ("not found" in d or "no such filter" in d):
        return ("este FFmpeg não tem o filtro de legendas (libass). Instale um build "
                "com --enable-libass ou desative 'queimar legendas'.")
    if "ass=" in d and ("invalid" in d or "no such" in d):
        return "o arquivo de legendas não pôde ser lido pelo FFmpeg."
    if "crop area" in d or "invalid too large" in d:
        return "o recorte automático saiu dos limites do vídeo; reanalise o enquadramento."
    if "no space left" in d:
        return "faltou espaço em disco durante a renderização."
    if "out of memory" in d or "cannot allocate" in d:
        return "memória insuficiente: tente 720p, menos tarefas simultâneas ou o modelo Whisper menor."
    if "encoder not found" in d:
        return "o codificador escolhido não existe neste FFmpeg; troque para H.264."
    return "o FFmpeg não conseguiu renderizar este corte."


def preview_render(clip: Clip, dest: Path, *, seconds: float = 6.0, width: int = 480,
                   progress=None, cancel=None) -> Path:
    """Short low-res proxy of a clip — really encoded, so it previews the export."""
    from .media import probe as _probe
    source = Path(clip.source_path)
    info = _probe(source)
    end = min(clip.end, clip.start + max(1.0, seconds))
    sub = Clip(**{k: v for k, v in clip.to_dict().items() if k != "edit"})
    sub.edit = clip.edit
    sub.start, sub.end = clip.start, end
    tmap = TimelineMap.full(clip.start, end)
    settings = ExportSettings(resolution=Resolution.R_720, fps=min(30.0, info.fps or 30.0),
                              aspect=clip.edit.aspect or AspectRatio.VERTICAL_9_16,
                              quality_crf=32, preset="ultrafast")
    return render_clip(sub, source=source, info=info, transcript=None, settings=settings,
                       plan=None, tmap=tmap, out=Path(dest), progress=progress,
                       cancel=cancel, proxy=True)
