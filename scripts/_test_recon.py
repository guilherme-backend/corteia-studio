"""Reconhecimento de comportamento real do núcleo (não é teste)."""
import os, subprocess, sys, time, json
from pathlib import Path
sys.path.insert(0, ".")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from corteia.core import media, silence, scenes, fonts, timeline, render
from corteia.models import Silence, AspectRatio, VideoInfo

T = Path("/tmp/recon"); T.mkdir(exist_ok=True)

# 1) 29.97 fps
p = T / "ntsc.mp4"
subprocess.run(["ffmpeg","-hide_banner","-loglevel","error","-y",
  "-f","lavfi","-i","testsrc=size=320x240:rate=30000/1001:duration=2",
  "-c:v","libx264","-preset","ultrafast","-pix_fmt","yuv420p",str(p)],check=True)
i = media.probe(p); print("1) fps ntsc:", i.fps, i.duration, i.width, i.height, i.fps)

# 2) sem video / txt
a = T/"aud.mp4"
subprocess.run(["ffmpeg","-hide_banner","-loglevel","error","-y","-f","lavfi","-i","sine=frequency=440:duration=1","-c:a","aac",str(a)],check=True)
try:
    media.probe_or_raise(a)
except Exception as e: print("2) só áudio ->", type(e).__name__, repr(e.message), e.stage)
txt=T/"x.txt"; txt.write_text("oi")
try:
    media.probe_or_raise(txt)
except Exception as e: print("3) txt ->", type(e).__name__, repr(e.message))
print("4) is_supported txt:", media.is_supported(txt), "noext:", media.is_supported(T/"x"))
try:
    media.probe_or_raise(T/"nao_existe.mp4")
except Exception as e: print("5) inexistente ->", type(e).__name__, repr(e.message), e.stage)

# 6) thumbnail
v = T/"speech.mp4"
if not v.exists():
    subprocess.run(["ffmpeg","-hide_banner","-loglevel","error","-y",
      "-f","lavfi","-i","testsrc2=size=1280x720:rate=30:duration=12",
      "-f","lavfi","-i","sine=frequency=330:duration=12,amix=inputs=1,volume='if(between(t,4,7),0,1)':eval=frame",
      "-shortest","-c:v","libx264","-preset","ultrafast","-pix_fmt","yuv420p","-c:a","aac",str(v)],check=True)
t1=time.perf_counter(); th=media.thumbnail(v, 2.0); d1=time.perf_counter()-t1
t2=time.perf_counter(); th2=media.thumbnail(v, 2.0); d2=time.perf_counter()-t2
print("6) thumb:", th.name, th.stat().st_size, "1a %.1f ms 2a %.2f ms" % (d1*1000, d2*1000))

# 7) energy curve vs astats
w = T/"tone.wav"
subprocess.run(["ffmpeg","-hide_banner","-loglevel","error","-y","-f","lavfi","-i","sine=frequency=1000:sample_rate=48000:duration=4","-c:a","pcm_s16le",str(w)],check=True)
times, db = media.audio_energy_curve(w, 0.1)
out = subprocess.run(["ffmpeg","-hide_banner","-loglevel","info","-i",str(w),"-af","volumedetect","-f","null","-"],capture_output=True,text=True).stderr
vd = {l.split(":")[0].strip(): l.split(":")[1].strip() for l in out.splitlines() if "mean_volume" in l or "max_volume" in l}
print("7) curve mean=%.3f min=%.3f max=%.3f | volumedetect %s" % (db.mean(), db.min(), db.max(), vd))
import numpy as np
print("   curve n=", len(times), "t[0]=%.3f t[-1]=%.3f" % (times[0], times[-1]))
# trecho silencioso
tm, dv = media.audio_energy_curve(v, 0.1)
sel = (tm>4.2)&(tm<6.8); print("   vídeo: dB medio fala=%.2f silêncio=%.2f" % (dv[~sel].mean(), dv[sel].mean()))

# 8) silence
sl = silence.detect_silences(v, db=-35.0, min_ms=350)
print("8) silences:", [(round(s.start,3), round(s.end,3)) for s in sl])

# 9) scenes cortes duros
c = T/"cuts.mp4"
segs=[]
for col in ("red","green","blue","orange"):
    f=T/f"{col}.mp4"
    subprocess.run(["ffmpeg","-hide_banner","-loglevel","error","-y","-f","lavfi","-i",f"color=c={col}:s=320x240:r=30:d=3","-c:v","libx264","-preset","ultrafast","-pix_fmt","yuv420p",str(f)],check=True)
    segs.append(f)
lst=T/"list.txt"; lst.write_text("".join(f"file '{s}'\n" for s in segs))
subprocess.run(["ffmpeg","-hide_banner","-loglevel","error","-y","-f","concat","-safe","0","-i",str(lst),"-c","copy",str(c)],check=True)
t0=time.perf_counter(); sc = scenes.detect_scenes(c); dt=time.perf_counter()-t0
print("9) scenes:", len(sc), [(round(s.start,2),round(s.end,2)) for s in sc], "%.2fs"%dt)
prog=[]
sc2 = scenes.detect_scenes(c, progress=lambda f,m: prog.append(f))
print("   progresso:", [round(x,2) for x in prog][:6], "...", [round(x,2) for x in prog][-3:], "monotonic=", all(b>=a-1e-9 for a,b in zip(prog,prog[1:])))
import threading
ev=threading.Event(); ev.set()
try:
    scenes.detect_scenes(c, cancel=ev); print("   cancel: NAO levantou")
except Exception as e: print("   cancel ->", type(e).__name__, e.message)

# 10) fonts
print("10) fonts installed sample:", [f for f in fonts.installed() if "Liberation" in f or "DejaVu" in f or "Arial" in f][:8])
print("    pick Arial:", fonts.pick("Arial"), "has Arial:", fonts.has("Arial"))
print("    pick Montserrat:", fonts.pick("Montserrat"))
fa = fonts.file_for("Arial"); print("    file_for('Arial') =", fa, Path(fa).is_file() if fa else None)
print("    file_for('Montserrat') =", fonts.file_for("Montserrat"))
print("    file_for('Arial Black') =", fonts.file_for("Arial Black"))

# 12) plan_blocks max_blocks merge
tm2 = timeline.TimelineMap([(0.0,3.0),(5.0,8.0),(10.0,13.0)])
info = media.probe(v)
b = render.plan_blocks(tm2, None, info, fps=30.0)
print("12) blocks default:", [(x.start,x.end) for x in b])
b2 = render.plan_blocks(tm2, None, info, fps=30.0, max_blocks=2)
print("    blocks max=2:", [(x.start,x.end) for x in b2])
print("    output_size:", render.output_size(__import__("corteia.models",fromlist=["x"]).ExportSettings(), None))
