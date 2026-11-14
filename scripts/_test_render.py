"""End-to-end render proof: real FFmpeg encode of a reframed, subtitled clip."""
import sys
from pathlib import Path
sys.path.insert(0, "/home/ubuntu/corteia-studio")

import subprocess
import cv2
import numpy as np

from corteia.models import (AspectRatio, Clip, ClipEdit, CropPlan, ExportSettings,
                            Resolution, Segment, Silence, SubtitleSettings, SubtitleStyle,
                            Transcript, Word, VideoInfo)
from corteia.core import render as R
from corteia.core import subtitles as SUB
from corteia.core import ffmpeg_cli as FC
from corteia.core.timeline import TimelineMap

SRC = Path("/tmp/render_src.mp4")
OUT = Path("/tmp/render_out.mp4")


def make_source():
    # 14 s, 1280x720, moving pattern, speech-like tone that goes silent 4-7 s
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
           "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30:duration=14",
           "-f", "lavfi", "-i",
           "sine=frequency=330:duration=14,amix=inputs=1,volume='if(between(t,4,7),0,1)':eval=frame",
           "-shortest", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
           "-c:a", "aac", str(SRC)]
    subprocess.run(cmd, check=True)
    print("fonte criada:", SRC, SRC.stat().st_size, "bytes")


def probe(p):
    return VideoInfo(path=str(p), duration=0)


def get_info(p: Path) -> VideoInfo:
    from corteia.core.media import probe
    return probe(p)


def transcript() -> Transcript:
    segs, t = [], 0.4
    lines = [
        "Eu larguei um salário de doze mil reais pra trabalhar de casa e não me arrependo.",
        "A verdade é que estabilidade é uma ilusão, você é sempre o primeiro corte.",
        "Então anota a lição: vende o que você já faz todo dia de graça.",
    ]
    for i, line in enumerate(lines):
        words = line.split()
        dur = 3.6
        ws, acc = [], t
        for w in words:
            d = dur / len(words)
            ws.append(Word(round(acc, 3), round(acc + d, 3), w))
            acc += d
        segs.append(Segment(round(t, 3), round(acc, 3), line, ws, 0))
        t = acc + (0.3 if i == 0 else 0.2)
    return Transcript("pt", "base", segs)


make_source()
info = get_info(SRC)
print(f"info: {info.width}x{info.height} fps={info.fps:.2f} dur={info.duration:.2f} "
      f"audio={info.has_audio} codec={info.codec_video}")

tr = transcript()
clip = Clip(source_path=str(SRC), start=0.0, end=info.duration,
            title="A verdade sobre estabilidade",
            description="Um trecho de teste real.")
clip.edit = ClipEdit(
    aspect=AspectRatio.VERTICAL_9_16,
    subtitles=SubtitleSettings(style=SubtitleStyle.VIRAL, font="DejaVu Sans",
                               highlight_color="#FFE400"),
    title_text="TESTE DE CORTE", title_style="bar",
    watermark_text="© corteia", watermark_pos="br", watermark_opacity=0.65,
    music_path=None, volume=1.0, remove_silences=True)

sil = [Silence(4.0, 7.0)]
tmap = TimelineMap.from_silences(sil, clip.start, clip.end, min_keep=0.5)
print(f"timeline: {tmap} | esperado ~{info.duration - 3.0:.2f}s")

# manual crop plan: keep a 404x720 window (9:16) sliding left/right
plan = CropPlan(points=[(0.0, 80, 0, 404, 720), (3.9, 440, 0, 404, 720),
                        (7.0, 700, 0, 404, 720), (10.0, 800, 0, 404, 720)],
                mode="face", faces_used=1)

blocks = R.plan_blocks(tmap, plan, info, fps=30.0)
print(f"blocos: {len(blocks)}")
for b in blocks:
    print(f"   [{b.start:5.2f}-{b.end:5.2f}] {b.duration:5.2f}s crop={b.crop}")

settings = ExportSettings(codec="libx264", resolution=Resolution.R_1080, fps=30.0,
                          quality_crf=26, preset="veryfast",
                          aspect=AspectRatio.VERTICAL_9_16, audio_bitrate="160k")
print("target size:", settings.target_size)

events = []
out = R.render_clip(clip, source=SRC, info=info, transcript=tr, settings=settings,
                    plan=plan, tmap=tmap, out=OUT,
                    progress=lambda f, m: events.append((round(f, 3), m)),
                    threads=4)
print(f"\nrender OK: {out} ({out.stat().st_size} bytes), {len(events)} updates de progresso")
print("primeiro/último progresso:", events[0] if events else None, events[-1] if events else None)

# verification with ffprobe
import json
d = json.loads(subprocess.run(["ffprobe", "-v", "error", "-print_format", "json",
                               "-show_format", "-show_streams", str(OUT)],
                              capture_output=True, text=True).stdout)
v = next(s for s in d["streams"] if s["codec_type"] == "video")
a = next((s for s in d["streams"] if s["codec_type"] == "audio"), None)
dur = float(d["format"]["duration"])
print(f"\n== VERIFICAÇÃO DO ARQUIVO ==")
print(f"duração  : {dur:.2f}s  (fonte {info.duration:.2f}s - 3s de silêncio = ~11.0s)")
print(f"resolução: {v['width']}x{v['height']}  AR={v['width']/v['height']:.4f} (alvo {9/16:.4f})")
print(f"fps      : {v['r_frame_rate']}  codec={v['codec_name']}  pix={v['pix_fmt']}")
print(f"áudio    : {a['codec_name'] if a else 'NENHUM'} {a.get('sample_rate') if a else ''} Hz")
assert abs(v["width"] / v["height"] - 9 / 16) < 0.01, "AR errado"
assert abs(dur - tmap.duration) < 0.45, f"duração errada: {dur} vs {tmap.duration}"
assert v["height"] == 1920 and v["width"] == 1080

# burn-in proof: subtitle pixels + title bar
cap = cv2.VideoCapture(str(OUT))
checks = []
for t in (1.2, 5.5, 9.4):
    cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000)
    ok, fr = cap.read()
    if not ok:
        checks.append((t, "no frame")); continue
    gray = cv2.cvtColor(fr, cv2.COLOR_BGR2GRAY)
    ink = int((gray > 200).sum())
    top = gray[:int(1920 * 0.10)]
    bar = int((top < 60).sum())
    hsv = cv2.cvtColor(fr, cv2.COLOR_BGR2HSV)
    yel = int(((hsv[:, :, 1] > 120) & (hsv[:, :, 0] > 20) & (hsv[:, :, 0] < 40)).sum())
    checks.append((t, ink, yel, bar))
    cv2.imwrite(f"/tmp/render_frame_{t}.png", fr)
cap.release()
print("\nframes (t, pixels claros, pixels amarelos, pixels da barra do título):")
for c in checks:
    print("  ", c)
assert any(c[1] > 3000 for c in checks if len(c) == 4), "legenda não aparece no vídeo"
assert any(c[3] > 5000 for c in checks if len(c) == 4), "barra do título não aparece"

# audio: 4-7 s of the source was silence; after the cut the loud parts must be continuous
db = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "info", "-i", str(OUT),
                     "-af", "astats=metadata=1:reset=1,ametadata=print:key=lavfi.astats.Overall.RMS_level",
                     "-f", "null", "-"], capture_output=True, text=True)
vals = [float(x.split("=")[1]) for x in db.stderr.splitlines() if "RMS_level" in x and "=" in x]
print(f"\nRMS medido no clipe: {len(vals)} janelas, min={min(vals):.1f} dB, "
      f"máx={max(vals):.1f} dB, mediana={sorted(vals)[len(vals)//2]:.1f} dB")
assert not any(v < -90 for v in vals), "houve silêncio residual: o corte de silêncio falhou"
print("\nTODAS AS ASSERÇÕES PASSARAM")
