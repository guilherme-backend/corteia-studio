"""Generate the real synthetic videos used by the vision smoke tests.

Everything is drawn locally with numpy/OpenCV: no network, no third-party
photo. The faces are procedural sprites (soft-mask compositing, skin shading,
eyebrows, nostrils, teeth, hair, sensor noise) built to be *detector-able* —
the bundled YuNet model answers with confidence ~0.78-0.84 on them, which is
what makes the tracking/speech tests real instead of simulated.

Usage
-----
    python scripts/_make_faces_video.py            # both test videos
    python scripts/_make_faces_video.py --test     # only /tmp/faces_test.mp4
    python scripts/_make_faces_video.py --perf     # only /tmp/faces_perf.mp4
"""

from __future__ import annotations

import math
import time
from pathlib import Path

import cv2
import numpy as np

SPEAKER_A = "a"
SPEAKER_B = "b"

# mouth schedule: an interview -- A talks until SPLIT_T, B takes over, so a plan
# has to switch tracks (and must do it with hysteresis, not every sample)
SPLIT_T = 5.0
A_FREQ = 2.7
B_FREQ = 2.3
SPRITE_H_OVER_FH = 2.5      #: sprite height / face-box height
SPRITE_W_OVER_FH = 2.2
FACE_CY_IN_SPRITE = 0.78    #: face-box centre Y, in face heights
MOUTH_CY_IN_SPRITE = 1.08   #: mouth centre Y, in face heights


def _draw(rgb: np.ndarray, alpha: np.ndarray | None, cx: float, cy: float,
          rx: float, ry: float, color, soft: float = 2.0, angle: float = 0.0) -> None:
    """Paint a soft ellipse; ``alpha`` (when given) grows with the shape mask."""
    h, w = rgb.shape[:2]
    x0, x1 = max(0, int(cx - rx * 1.7)), min(w, int(cx + rx * 1.7) + 1)
    y0, y1 = max(0, int(cy - ry * 1.7)), min(h, int(cy + ry * 1.7) + 1)
    if x1 <= x0 or y1 <= y0 or rx <= 0 or ry <= 0:
        return
    yy, xx = np.mgrid[y0:y1, x0:x1]
    dx, dy = xx - cx, yy - cy
    ca, sa = math.cos(math.radians(angle)), math.sin(math.radians(angle))
    d = ((dx * ca + dy * sa) / rx) ** 2 + ((-dx * sa + dy * ca) / ry) ** 2
    m = np.clip((1.0 - d) * soft, 0.0, 1.0).astype(np.float32)
    col = np.asarray(color, np.float32)
    rgb[y0:y1, x0:x1] = rgb[y0:y1, x0:x1] * (1.0 - m[..., None]) + col * m[..., None]
    if alpha is not None:
        alpha[y0:y1, x0:x1] = np.maximum(alpha[y0:y1, x0:x1], m)


def _head_sprite(fh: int, skin, hair, shirt, rng: np.random.Generator):
    """Build ``(rgb float32, alpha float32)`` for a face box of ``fh`` pixels."""
    sw, sh = int(fh * SPRITE_W_OVER_FH) | 1, int(fh * SPRITE_H_OVER_FH) | 1
    rgb = np.zeros((sh, sw, 3), np.float32)
    alpha = np.zeros((sh, sw), np.float32)
    cx, cy = sw / 2.0, fh * FACE_CY_IN_SPRITE
    fw = fh * 0.74

    _draw(rgb, alpha, cx, cy + fh * 1.30, fh * 1.05, fh * 0.55, shirt, soft=3.0)   # shoulders
    _draw(rgb, alpha, cx, cy + fh * 0.80, fw * 0.30, fh * 0.32,                     # neck
          (skin[0] * 0.86, skin[1] * 0.86, skin[2] * 0.88), soft=2.4)
    # head: soft ellipse with a key light from the upper left
    yy, xx = np.mgrid[0:sh, 0:sw]
    d = ((xx - cx) / (fw / 2.0)) ** 2 + ((yy - cy) / (fh / 2.0)) ** 2
    m = np.clip((1.0 - d) * 1.6, 0.0, 1.0).astype(np.float32)
    shade = np.clip(1.0 - 0.30 * np.sqrt(np.maximum(d, 0.0))
                    + 0.11 * ((cx - xx) / fw) + 0.07 * ((cy - yy) / fh), 0.55, 1.15)
    rgb[...] = rgb * (1.0 - m[..., None]) + (np.array(skin, np.float32) * shade[..., None]) * m[..., None]
    alpha = np.maximum(alpha, m)

    ex, ey = fw * 0.27, cy - fh * 0.14
    for sx in (-1.0, 1.0):
        x = cx + sx * ex
        _draw(rgb, alpha, x, ey + fh * 0.02, fw * 0.16, fw * 0.075,
              (skin[0] * 0.80, skin[1] * 0.80, skin[2] * 0.84), soft=1.6)          # socket
        _draw(rgb, alpha, x, ey, fw * 0.135, fh * 0.052, (238, 238, 242), soft=2.6)  # sclera
        _draw(rgb, alpha, x + sx * fw * 0.012, ey + fh * 0.004, fw * 0.058, fh * 0.052,
              (95, 66, 42), soft=3.4)                                              # iris
        _draw(rgb, alpha, x + sx * fw * 0.012, ey + fh * 0.004, fw * 0.027, fh * 0.027,
              (16, 12, 10), soft=5.0)                                              # pupil
        _draw(rgb, alpha, x, ey - fh * 0.055, fw * 0.15, fh * 0.030,
              (skin[0] * 0.72, skin[1] * 0.72, skin[2] * 0.75), soft=1.8)          # lid
        _draw(rgb, alpha, x, ey - fh * 0.115, fw * 0.175, fh * 0.030,
              (hair[0] + 14, hair[1] + 11, hair[2] + 9), soft=2.2, angle=-7 * sx)   # brow
    ny = cy + fh * 0.10
    _draw(rgb, alpha, cx, ny - fh * 0.02, fw * 0.085, fh * 0.10,                   # bridge
          (min(255, skin[0] * 1.10), min(255, skin[1] * 1.06), min(255, skin[2] * 1.02)), soft=1.3)
    for sx in (-1.0, 1.0):
        _draw(rgb, alpha, cx + sx * fw * 0.075, ny + fh * 0.035, fw * 0.032, fh * 0.020,
              (skin[0] * 0.52, skin[1] * 0.52, skin[2] * 0.58), soft=3.0)           # nostril
    _draw(rgb, alpha, cx, cy - fh * 0.40, fw * 0.62, fh * 0.30, hair, soft=1.7)     # hair cap
    for sx in (-1.0, 1.0):
        _draw(rgb, alpha, cx + sx * fw * 0.50, cy - fh * 0.08, fw * 0.17, fh * 0.34,
              hair, soft=1.7)                                                      # sides
    for sx in (-1.0, 1.0):
        _draw(rgb, alpha, cx + sx * fw * 0.34, cy + fh * 0.14, fw * 0.14, fh * 0.07,
              (skin[0] * 0.95, skin[1] * 0.90, skin[2] * 0.92), soft=1.2)          # cheeks
    rgb += rng.normal(0.0, 2.4, (sh, sw, 1)).astype(np.float32)
    rgb = cv2.GaussianBlur(rgb, (3, 3), 0)
    alpha = cv2.GaussianBlur(alpha, (3, 3), 0)
    return np.clip(rgb, 0, 255).astype(np.float32), np.clip(alpha, 0, 1).astype(np.float32)


def _mouth(sprite: np.ndarray, fh: float, open_: float) -> None:
    """Draw the mouth of a resized sprite: lips, cavity and upper teeth."""
    sw, sh = sprite.shape[1], sprite.shape[0]
    mcx, mcy = sw / 2.0, fh * MOUTH_CY_IN_SPRITE
    if mcy > sh - 2:
        return
    _draw(sprite, None, mcx, mcy, fh * 0.225, fh * (0.030 + 0.078 * open_),
          (70, 45, 135), soft=2.4)
    if open_ > 0.10:
        _draw(sprite, None, mcx, mcy + fh * 0.006, fh * 0.165, fh * 0.055 * open_,
              (22, 14, 26), soft=3.2)
        _draw(sprite, None, mcx, mcy - fh * 0.022 * open_, fh * 0.130, fh * 0.016,
              (232, 232, 226), soft=4.5)


def _paste(bg: np.ndarray, rgb: np.ndarray, alpha: np.ndarray, cx: float, cy: float,
           scale: float, mouth: float) -> None:
    """Composite a sprite so that its face-box centre lands on ``(cx, cy)``."""
    h, w = bg.shape[:2]
    sh, sw = alpha.shape
    tw, th = max(8, int(round(sw * scale))), max(16, int(round(sh * scale)))
    sprite = cv2.resize(rgb, (tw, th), interpolation=cv2.INTER_AREA).astype(np.float32)
    a = cv2.resize(alpha, (tw, th), interpolation=cv2.INTER_AREA)
    _mouth(sprite, th / SPRITE_H_OVER_FH, mouth)
    x0 = int(round(cx - tw / 2.0))
    y0 = int(round(cy - th * (FACE_CY_IN_SPRITE / SPRITE_H_OVER_FH)))
    xs0, ys0 = max(0, x0), max(0, y0)
    xs1, ys1 = min(w, x0 + tw), min(h, y0 + th)
    if xs1 <= xs0 or ys1 <= ys0:
        return
    sub = bg[ys0:ys1, xs0:xs1].astype(np.float32)
    crop = sprite[ys0 - y0:ys1 - y0, xs0 - x0:xs1 - x0]
    asp = a[ys0 - y0:ys1 - y0, xs0 - x0:xs1 - x0][..., None]
    bg[ys0:ys1, xs0:xs1] = np.clip(sub * (1.0 - asp) + crop * asp, 0, 255).astype(np.uint8)



def _background(w: int, h: int, rng: np.random.Generator) -> np.ndarray:
    """Static studio backdrop with two soft props (so saliency has structure)."""
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    top = np.array([26.0, 30.0, 44.0], np.float32)
    bottom = np.array([12.0, 14.0, 20.0], np.float32)
    k = (yy / h)[..., None]
    img = top * (1.0 - k) + bottom * k
    glow = np.exp(-(((xx - w * 0.20) / (w * 0.35)) ** 2 + ((yy - h * 0.15) / (h * 0.40)) ** 2))
    img += 9.0 * glow[..., None]
    img = img.astype(np.float32)
    _draw(img, None, w * 0.84, h * 0.78, w * 0.14, h * 0.26, (58, 44, 34), soft=3.0)
    _draw(img, None, w * 0.10, h * 0.26, w * 0.06, h * 0.20, (44, 56, 66), soft=2.0)
    img = cv2.GaussianBlur(img, (31, 31), 0)
    return np.clip(img, 0, 255).astype(np.uint8)


def scene_state(t: float, w: int, h: int) -> dict[str, dict[str, float]]:
    """Where each person is at time ``t`` and how open their mouth is."""
    swing = math.sin(2 * math.pi * 0.11 * t)
    return {
        SPEAKER_A: dict(cx=(0.31 + 0.11 * swing) * w,
                        cy=(0.42 + 0.03 * math.sin(2 * math.pi * 0.33 * t)) * h,
                        fh=0.30 * h,
                        mouth=(0.5 + 0.5 * math.sin(2 * math.pi * A_FREQ * t))
                        if t < SPLIT_T else 0.0),
        SPEAKER_B: dict(cx=(0.69 - 0.11 * swing) * w,
                        cy=(0.46 - 0.03 * math.sin(2 * math.pi * 0.29 * t)) * h,
                        fh=0.245 * h,
                        mouth=(0.5 + 0.5 * math.sin(2 * math.pi * B_FREQ * t))
                        if t >= SPLIT_T else 0.0),
    }


def make_frame(t: float, sprites: dict, base_bg: np.ndarray, w: int, h: int,
               rng: np.random.Generator) -> np.ndarray:
    """One frame of the two-person interview scene at time ``t``."""
    img = base_bg.copy()
    for name, s in scene_state(t, w, h).items():
        rgb, alpha = sprites[name]
        fh_canon = rgb.shape[0] / SPRITE_H_OVER_FH
        _paste(img, rgb, alpha, s["cx"], s["cy"], s["fh"] / fh_canon,
               max(0.0, min(1.0, s["mouth"])))
    for s in scene_state(t, w, h).values():          # grain only over the people
        rw, rh = int(s["fh"] * 1.5), int(s["fh"] * 1.9)
        x0, y0 = max(0, int(s["cx"] - rw / 2)), max(0, int(s["cy"] - rh / 2))
        x1, y1 = min(w, x0 + rw), min(h, y0 + rh)
        if x1 > x0 and y1 > y0:
            roi = img[y0:y1, x0:x1].astype(np.float32)
            roi += rng.normal(0.0, 2.0, roi.shape)
            img[y0:y1, x0:x1] = np.clip(roi, 0, 255).astype(np.uint8)
    return img


def build(out: Path, *, size=(1280, 720), fps: float = 30.0, duration: float = 12.0,
          seed: int = 7) -> dict:
    """Write ``out``: mp4v pass with OpenCV, then a real h264 remux with FFmpeg."""
    import shutil
    import subprocess

    w, h = size
    rng = np.random.default_rng(seed)
    sprites = {
        SPEAKER_A: _head_sprite(int(0.30 * h), (150, 170, 205), (38, 34, 30), (58, 64, 92), rng),
        SPEAKER_B: _head_sprite(int(0.245 * h), (120, 142, 188), (22, 22, 26), (92, 58, 56), rng),
    }
    bg = _background(w, h, rng)
    out.parent.mkdir(parents=True, exist_ok=True)
    raw = out.with_suffix(".raw.mp4")
    vw = cv2.VideoWriter(str(raw), cv2.VideoWriter_fourcc(*"mp4v"), float(fps), (w, h))
    if not vw.isOpened():
        raise RuntimeError("cv2.VideoWriter não abriu (mp4v indisponível)")
    n = int(round(duration * fps))
    t0 = time.perf_counter()
    for i in range(n):
        vw.write(make_frame(i / fps, sprites, bg, w, h, rng))
    vw.release()
    draw_s = time.perf_counter() - t0

    ff = shutil.which("ffmpeg") or "/usr/bin/ffmpeg"
    subprocess.run([ff, "-y", "-loglevel", "error", "-i", str(raw), "-an",
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                    "-pix_fmt", "yuv420p", "-g", str(int(fps)),
                    "-movflags", "+faststart", str(out)], check=True, capture_output=True)
    raw.unlink(missing_ok=True)
    fp = shutil.which("ffprobe") or "/usr/bin/ffprobe"
    probe = subprocess.run([fp, "-v", "error", "-show_entries",
                            "stream=codec_name,width,height,r_frame_rate,nb_frames",
                            "-show_entries", "format=duration", "-of", "default=nw=1", str(out)],
                           capture_output=True, text=True).stdout.strip()
    return {"draw_seconds": round(draw_s, 2), "total_seconds": round(time.perf_counter() - t0, 2),
            "frames": n, "size": size, "bytes": out.stat().st_size, "ffprobe": probe}


def main(argv=None) -> int:
    import argparse

    ap = argparse.ArgumentParser(description="gerador de vídeo sintético com rostos")
    ap.add_argument("--test", action="store_true", help="apenas /tmp/faces_test.mp4")
    ap.add_argument("--perf", action="store_true", help="apenas /tmp/faces_perf.mp4")
    args = ap.parse_args(argv)

    jobs = []
    if not args.perf:
        jobs.append((Path("/tmp/faces_test.mp4"), (1280, 720), 12.0))
    if not args.test:
        jobs.append((Path("/tmp/faces_perf.mp4"), (1920, 1080), 60.0))
    for path, size, dur in jobs:
        stats = build(path, size=size, fps=30.0, duration=dur)
        print(f"{path}: {size[0]}x{size[1]} {dur}s frames={stats['frames']} "
              f"draw={stats['draw_seconds']}s total={stats['total_seconds']}s "
              f"{stats['bytes'] / 1e6:.1f} MB")
        print(stats["ffprobe"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
