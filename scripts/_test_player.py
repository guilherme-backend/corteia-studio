"""Real (offscreen) evidence for VideoPlayer + JobRunner. Not a mock: the mp4
is produced with FFmpeg and decoded by Qt's ffmpeg backend."""
import hashlib, os, subprocess, sys, time
from pathlib import Path
os.environ["QT_QPA_PLATFORM"] = "offscreen"
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
MEDIA = ROOT / "scripts" / "_media"
SHOTS = ROOT / "scripts" / "_shots_clips"
MEDIA.mkdir(parents=True, exist_ok=True); SHOTS.mkdir(parents=True, exist_ok=True)
SAMPLE = MEDIA / "player_sample.mp4"
if not SAMPLE.exists():
    subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                    "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30:duration=8",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=8", "-shortest",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(SAMPLE)], check=True)
BROKEN = MEDIA / "broken.mp4"
BROKEN.write_bytes(b"this is not a video file" * 40)

from PySide6.QtWidgets import QApplication
app = QApplication(sys.argv)
from PySide6.QtCore import Qt
from PySide6.QtGui import QImage
from corteia.context import AppContext
from corteia.models import Clip, ClipStatus, Project, Segment, SourceVideo, Transcript, VideoInfo, Word
from corteia.ui.widgets.player import VideoPlayer
from corteia.workers.jobs import JobRunner
from corteia.core.errors import CancelledError

ctx = AppContext()
info = VideoInfo(path=str(SAMPLE), duration=8.0, width=1280, height=720, fps=30.0, codec_video="h264")
transcript = Transcript(language="pt", model="fixture", segments=[
    Segment(2.0, 4.0, "Primeira fala do trecho.", [Word(2.0, 2.5, "Primeira"), Word(2.5, 3.0, "fala"),
                                                    Word(3.0, 3.5, "do"), Word(3.5, 4.0, "trecho")]),
    Segment(4.0, 6.0, "Segunda frase.", [Word(4.0, 4.6, "Segunda"), Word(4.6, 6.0, "frase")])])
p = Project(name="Fixture", sources=[SourceVideo(path=str(SAMPLE), info=info, transcript=transcript,
                                                 stages_done=["probe", "transcribe"])],
             clips=[Clip(source_path=str(SAMPLE), start=2.0, end=8.0, title="Corte A",
                         status=ClipStatus.APPROVED),
                    Clip(source_path=str(SAMPLE), start=0.0, end=4.0, title="Corte B")])
ctx.store.current = p
results = []
def pump(ms, cond=None):
    end = time.time() + ms / 1000.0
    while time.time() < end:
        app.processEvents(); time.sleep(0.01)
        if cond is not None and cond():
            return True
    return cond() if cond is not None else True

player = VideoPlayer(ctx)
player.set_fps(30.0); player.set_frame_size(1280, 720)
player.resize(760, 470); player.show()
loaded = []
player.mediaLoaded.connect(lambda ok: loaded.append(ok))
assert player.set_source(SAMPLE, start=2.0, end=6.0) is True
pump(3500, cond=lambda: player.duration() > 0)
results.append(("duration real da mídia", player.duration(), abs(player.duration() - 8.0) < 0.15))
player.seek(3.0); pump(400)
results.append(("position() após seek(3.0)", player.position(), abs(player.position() - 3.0) <= 0.12))
player.play(); pump(900)
results.append(("avanço do relógio em ~0,9 s", player.position(), player.position() > 3.3))
player.pause()
before = player.position(); player.step_frame(1); pump(120)
results.append(("delta de 1 frame (1/30 s)", player.position() - before, abs((player.position() - before) - 1/30) < 0.05))
player.set_speed(1.5)
results.append(("velocidade 1,5x aplicada", player.speed(), player.speed() == 1.5))

def shot(name):
    """Grab the player, save it and hash the real PNG bytes (no fake evidence)."""
    pix = player.grab()
    path = SHOTS / name
    pix.save(str(path), "PNG")
    return hashlib.sha256(path.read_bytes()).hexdigest(), pix

player.set_crop_rect(None); pump(120)
digest_plain, pix_plain = shot("player_sem_recorte.png")
player.set_crop_rect((400, 0, 480, 720)); pump(120)
digest_crop, pix_crop = shot("player_com_recorte_9-16.png")
img = pix_crop.toImage()
dark = sum(1 for y in range(0, img.height(), 3) for x in range(0, img.width(), 3)
           if img.pixelColor(x, y).lightness() < 45)
img2 = pix_plain.toImage()
dark2 = sum(1 for y in range(0, img2.height(), 3) for x in range(0, img2.width(), 3)
            if img2.pixelColor(x, y).lightness() < 45)
results.append(("overlay: hashes diferentes", digest_plain[:12] != digest_crop[:12], digest_plain[:12] != digest_crop[:12]))
results.append(("overlay: pixéis escurecidos fora do recorte", dark - dark2, dark > dark2 + 200))
results.append(("rect do recorte no widget", player.area.overlay.crop_widget_rect().getRect() if player.area.overlay.crop_widget_rect() else None,
                player.area.overlay.crop_widget_rect() is not None))

player.set_loop_range(3.0, 4.0)
player.seek(3.0); player.play()
pump(2600)
max_seen = player.position()
player.pause()
results.append(("loop 3→4 s: posição final dentro da faixa", max_seen, max_seen < 4.4 and player.loop_range() == (3.0, 4.0)))

errs = []
player.player.errorOccurred.connect(lambda e, tt: errs.append(tt))
player.player.mediaStatusChanged.connect(lambda s: None)
ok = player.set_source(BROKEN, start=1.0)
pump(7000, cond=lambda: player.current_pixmap() is not None)
poster = player.area.poster.pixmap()
shot("player_fallback_poster.png")
results.append(("arquivo inválido: poster real do FFmpeg", None if poster is None else (poster.width(), poster.height()),
                poster is not None and not poster.isNull()))
results.append(("status honesto no erro", player.lbl_status.text()[:60], bool(player.lbl_status.text())))

runner = JobRunner(ctx, max_parallel=2)
fractions = []
j = runner.run(lambda progress: [progress(i / 10.0, f"etapa {i}") or time.sleep(0.02) for i in range(11)],
               name="progresso-fração", on_progress=lambda frac, lbl: fractions.append(frac))
runner.wait_idle(5000)
results.append(("JobRunner: frações 0,0→1,0 entregues na thread GUI", fractions[:3] + [fractions[-1]],
                len(fractions) == 11 and fractions[0] == 0.0 and fractions[-1] == 1.0 and all(0.0 <= f <= 1.0 for f in fractions)))
t0 = time.time(); stop_at = []
def slow(progress):
    for i in range(400):
        if not progress(i / 400.0, "devagar"):
            raise CancelledError()
        time.sleep(0.01)
j2 = runner.run(slow, name="lento")
time.sleep(0.4); runner.cancel("lento"); runner.wait_idle(4000)
stop_at.append(time.time() - t0)
results.append(("JobRunner: cancelamento por Event", round(stop_at[0], 3), stop_at[0] < 1.0 and j2.state == "cancelled"))
seen_err = []
runner.run(lambda progress: (_ for _ in ()).throw(RuntimeError("FFmpeg quebrou")), name="erro",
           on_error=lambda text: seen_err.append(text))
runner.wait_idle(4000)
results.append(("JobRunner: erro legível", seen_err, bool(seen_err) and "FFmpeg quebrou" in seen_err[0]))

print("\n%-46s %-34s %s" % ("verificação", "valor real", "ok"))
print("-" * 96)
all_ok = True
for name, value, ok in results:
    all_ok = bool(ok) and all_ok
    print("%-46s %-34s %s" % (name, str(value)[:32], "OK" if ok else "FALHOU"))
print("-" * 96)
print("screenshots:", sorted(x.name for x in SHOTS.glob("player_*.png")))
print("TODOS OS TESTES PASSARAM" if all_ok else "HOUVE FALHAS")
sys.exit(0 if all_ok else 1)
