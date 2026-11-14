#!/usr/bin/env python3
"""Generate every CorteIA Studio icon from one geometry source of truth.

Outputs (written next to this file, in ``assets/``):

* ``icon.svg``   - 1024x1024 vector master
* ``icon.png``   - 1024x1024 raster master (rendered at 4096, Lanczos downsample)
* ``icon_512.png`` / ``icon_256.png`` / ``icon_64.png`` / ``icon_32.png``
* ``icon.ico``   - multi resolution 16/24/32/48/64/128/256 (exe + installer)

Mark: a violet "play" triangle holding a small neural graph, sliced from the
lower left by a silver pair of scissors with violet finger rings, on a rounded
dark tile using the app palette (``#0E0F14`` / ``#7C5CFF``).

Pillow only -- no rsvg-convert/cairosvg needed -- and deterministic: re-running
produces byte-identical files.

    cd /home/ubuntu/corteia-studio && . .venv/bin/activate
    python assets/make_icons.py
"""
from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

HERE = Path(__file__).resolve().parent
CANVAS = 1024
SS = 4                       # supersample factor -> real render size 4096
PIX = CANVAS * SS

# --------------------------------------------------------------------------- #
# palette (kept in sync with corteia/ui/theme.py)
# --------------------------------------------------------------------------- #
BG_TOP = (26, 27, 40)        # #1A1B28
BG_BOTTOM = (14, 15, 20)     # #0E0F14
ACCENT = (124, 92, 255)      # #7C5CFF
ACCENT_LIGHT = (174, 148, 255)
ACCENT_DEEP = (90, 58, 206)
SILVER = (238, 240, 248)
SILVER_DARK = (150, 158, 180)
INK = (14, 15, 20)

TILE_INSET = 40
TILE_RADIUS = 188

TRIANGLE = [(400, 208), (400, 696), (802, 452)]      # play button
TRI_ROUND = 26
NODES = [(592, 362, 16), (686, 452, 23), (592, 542, 16), (498, 452, 16)]
EDGES = [(0, 1), (1, 2), (3, 0), (3, 2), (3, 1)]

SCISSORS_PIVOT = (388, 706)  # tile-local pivot lands here
SCISSORS_ANGLE = 33          # PIL degrees, counter-clockwise -> blades up-right
SCISSORS_SCALE = 0.86


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #
def _p(x: float, y: float) -> tuple[int, int]:
    return (int(round(x * SS)), int(round(y * SS)))


def new_layer() -> Image.Image:
    return Image.new("RGBA", (PIX, PIX), (0, 0, 0, 0))


def tile_mask() -> Image.Image:
    m = Image.new("L", (PIX, PIX), 0)
    ins = TILE_INSET * SS
    ImageDraw.Draw(m).rounded_rectangle(
        [ins, ins, PIX - ins - 1, PIX - ins - 1], radius=TILE_RADIUS * SS, fill=255
    )
    return m


def vertical_gradient(top, bottom) -> Image.Image:
    col = Image.new("RGBA", (1, PIX))
    px = col.load()
    for y in range(PIX):
        t = y / (PIX - 1)
        px[0, y] = tuple(int(round(top[i] + (bottom[i] - top[i]) * t)) for i in range(3)) + (255,)
    return col.resize((PIX, PIX))


def diagonal_gradient(p0, p1, c0, c1) -> tuple[Image.Image, tuple[int, int]]:
    """RGBA gradient interpolated from ``p0`` to ``p1`` (1024 space), plus origin."""
    a, b = _p(*p0), _p(*p1)
    w, h = b[0] - a[0] + 1, b[1] - a[1] + 1
    img = Image.new("RGBA", (w, h))
    px = img.load()
    denom = float((w - 1) ** 2 + (h - 1) ** 2) or 1.0
    for y in range(h):
        base = y * y
        for x in range(w):
            t = math.sqrt(max(0.0, min(1.0, (x * x + base) / denom)))
            px[x, y] = tuple(int(round(c0[i] + (c1[i] - c0[i]) * t)) for i in range(3)) + (255,)
    return img, a


def rounded_triangle(draw: ImageDraw.ImageDraw, points, radius: int, fill) -> None:
    """Fill a triangle, rounding its convex corners with a curve-joined stroke."""
    sp = [_p(x, y) for x, y in points]
    draw.polygon(sp, fill=fill)
    w = int(round(radius * SS * 2.0))
    draw.line(sp + [sp[0]], fill=fill, width=w, joint="curve")
    rr = w / 2.0
    for (cx, cy) in sp:
        draw.ellipse([cx - rr, cy - rr, cx + rr, cy + rr], fill=fill)


def circle(draw: ImageDraw.ImageDraw, center, radius: int, fill) -> None:
    cx, cy = _p(*center)
    r = radius * SS
    draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=fill)


# --------------------------------------------------------------------------- #
# scissors, drawn pointing along +x then rotated into place
# --------------------------------------------------------------------------- #
def scissors_tile(size: int = 760) -> Image.Image:
    # Square RGBA tile; the pivot sits at the tile centre, blades face +x.
    # Screen coordinates: y grows downward, so an 'up' tip has a negative y.
    # Drawn at supersample resolution (size * SS pixels).
    img = Image.new("RGBA", (size * SS, size * SS), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    c = size / 2.0

    def q(x: float, y: float) -> tuple[int, int]:
        return (int(round((x + c) * SS)), int(round((y + c) * SS)))

    blade_tip, blade_rise = 262, 100
    ring_x, ring_y, ring_r, ring_w = -178, 126, 56, 20
    arm_tip = (ring_x + ring_r * 0.55, ring_y * 0.86)   # end inside the ring
    w_blade, w_tip, w_arm = 32, 6, 26

    def blade(up: bool, colour) -> None:
        sign = -1 if up else 1
        tip = q(blade_tip, sign * blade_rise)
        root = q(0, 0)
        dx, dy = tip[0] - root[0], tip[1] - root[1]
        ln = math.hypot(dx, dy) or 1.0
        nx, ny = -dy / ln, dx / ln
        w0, w1 = w_blade * SS / 2.0, w_tip * SS / 2.0
        d.polygon([
            (root[0] + nx * w0, root[1] + ny * w0),
            (tip[0] + nx * w1, tip[1] + ny * w1),
            (tip[0] - nx * w1, tip[1] - ny * w1),
            (root[0] - nx * w0, root[1] - ny * w0),
        ], fill=colour)
        d.ellipse([root[0] - w0, root[1] - w0, root[0] + w0, root[1] + w0], fill=colour)
        d.ellipse([tip[0] - w1, tip[1] - w1, tip[0] + w1, tip[1] + w1], fill=colour)

    def arm(up: bool, colour) -> None:
        # straight shank from the pivot to the ring, so no gap shows at 16 px
        sign = -1 if up else 1
        p0, p1 = q(-10, 0), q(arm_tip[0], sign * arm_tip[1])
        d.line([p0, p1], fill=colour, width=int(w_arm * SS), joint="curve")
        rr = w_arm * SS / 2.0
        for pt in (p0, p1):
            d.ellipse([pt[0] - rr, pt[1] - rr, pt[0] + rr, pt[1] + rr], fill=colour)

    def ring(up: bool, colour) -> None:
        sign = -1 if up else 1
        cx, cy = q(ring_x, sign * ring_y)
        r, w = ring_r * SS, ring_w * SS
        d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=colour, width=int(w))

    # crossed geometry: the upper blade is driven by the lower handle
    arm(False, ACCENT)
    arm(True, ACCENT_LIGHT)
    ring(False, ACCENT)
    ring(True, ACCENT_LIGHT)
    blade(True, SILVER)
    blade(False, SILVER_DARK)
    circle_at = lambda x, y, r, col: (
        d.ellipse([(x + c - r) * SS, (y + c - r) * SS, (x + c + r) * SS, (y + c + r) * SS], fill=col)
    )
    circle_at(0, 0, 24, SILVER)
    circle_at(0, 0, 10, INK)
    return img


def scissors_shadow(tile: Image.Image) -> Image.Image:
    shadow = Image.new("RGBA", tile.size, (0, 0, 0, 255))
    shadow.putalpha(tile.split()[3].point(lambda v: int(v * 0.38)))
    return shadow


def compose_scissors() -> Image.Image:
    """Rotated scissors + its drop shadow, on a full canvas layer."""
    tile = scissors_tile()
    side = int(tile.width * SCISSORS_SCALE)
    tile = tile.resize((side, side), Image.BICUBIC)
    shadow = scissors_shadow(tile).rotate(SCISSORS_ANGLE, expand=True, resample=Image.BICUBIC)
    tile = tile.rotate(SCISSORS_ANGLE, expand=True, resample=Image.BICUBIC)

    layer = new_layer()
    px, py = _p(*SCISSORS_PIVOT)
    # the pivot is at the centre of the un-rotated tile; after expand=True the
    # centre is preserved, so centring the rotated image on the pivot is exact
    layer.alpha_composite(shadow, (px - shadow.width // 2 + 9 * SS, py - shadow.height // 2 + 13 * SS))
    layer.alpha_composite(tile, (px - tile.width // 2, py - tile.height // 2))
    return layer


# --------------------------------------------------------------------------- #
# master render
# --------------------------------------------------------------------------- #
def render_master() -> Image.Image:
    mask = tile_mask()
    out = vertical_gradient(BG_TOP, BG_BOTTOM)

    # soft accent glow behind the mark, clipped to the tile
    glow = new_layer()
    cx, cy = _p(516, 430)
    r = int(300 * SS)
    ImageDraw.Draw(glow).ellipse([cx - r, cy - r, cx + r, cy + r], fill=(*ACCENT, 105))
    glow = glow.filter(ImageFilter.GaussianBlur(radius=86 * SS))
    out = Image.alpha_composite(out, glow)

    # --- play triangle ----------------------------------------------------
    tri = new_layer()
    rounded_triangle(ImageDraw.Draw(tri), TRIANGLE, TRI_ROUND, (120, 88, 245, 255))
    tgrad, origin = diagonal_gradient((356, 214), (802, 690), ACCENT_LIGHT, ACCENT_DEEP)
    fill = new_layer()
    fill.paste(tgrad, origin)
    out = Image.alpha_composite(out, Image.composite(fill, tri, tri.split()[3]))

    # --- neural graph -----------------------------------------------------
    graph = new_layer()
    gd = ImageDraw.Draw(graph)
    for a, b in EDGES:
        gd.line([_p(*NODES[a][:2]), _p(*NODES[b][:2])], fill=(24, 20, 48, 175), width=int(5 * SS))
    for (nx, ny, rad) in NODES:
        circle(gd, (nx, ny), rad + 8, (24, 20, 48, 130))
    out = Image.alpha_composite(out, graph)
    od = ImageDraw.Draw(out)
    for (nx, ny, rad) in NODES:
        circle(od, (nx, ny), rad, (*SILVER, 255))
        if rad > 20:
            circle(od, (nx - 3, ny - 3), rad - 8, (255, 255, 255, 255))

    # --- scissors ---------------------------------------------------------
    out = Image.alpha_composite(out, compose_scissors())

    # --- tile rim light + crop to the rounded square ----------------------
    rim = Image.new("RGBA", (PIX, PIX), (*BG_TOP, 255))
    edge = Image.new("L", (PIX, PIX), 0)
    ins = TILE_INSET * SS
    ImageDraw.Draw(edge).rounded_rectangle(
        [ins, ins, PIX - ins - 1, PIX - ins - 1], radius=TILE_RADIUS * SS,
        outline=255, width=int(2.6 * SS),
    )
    highlight = Image.new("RGBA", (PIX, PIX), (210, 214, 232, 255))
    out = Image.composite(highlight, out, edge.point(lambda v: int(v * 0.22)))

    transparent = Image.new("RGBA", (PIX, PIX), (0, 0, 0, 0))
    out = Image.composite(out, transparent, mask)

    return out.resize((CANVAS, CANVAS), Image.LANCZOS)


# --------------------------------------------------------------------------- #
# outputs
# --------------------------------------------------------------------------- #
def emit_pngs(img: Image.Image) -> None:
    img.save(HERE / "icon.png", optimize=True)
    for size in (512, 256, 64, 32):
        img.resize((size, size), Image.LANCZOS).save(HERE / f"icon_{size}.png", optimize=True)


def emit_contact_sheet(img: Image.Image) -> None:
    """Preview on light and dark backgrounds at real icon sizes."""
    sheet = Image.new("RGBA", (900, 300), (255, 255, 255, 255))
    d = ImageDraw.Draw(sheet)
    d.rectangle([450, 0, 900, 300], fill=(18, 19, 26, 255))
    x = 30
    for size in (256, 128, 64, 48, 32, 24, 16):
        tile = img.resize((size, size), Image.LANCZOS)
        sheet.alpha_composite(tile, (x, 20))
        sheet.alpha_composite(tile, (450 + x, 20))
        x += size + 22
    d.text((30, 290 - 14), "light background", fill=(0, 0, 0, 255))
    sheet.save(HERE / "icon_preview.png")


def emit_ico(img: Image.Image) -> None:
    sizes = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
    img.save(HERE / "icon.ico", format="ICO", sizes=sizes)


def emit_svg() -> None:
    tri = " ".join(f"{x},{y}" for x, y in TRIANGLE)
    edge_xml = "\n".join(
        f'      <line x1="{NODES[a][0]}" y1="{NODES[a][1]}" x2="{NODES[b][0]}" y2="{NODES[b][1]}"/>'
        for a, b in EDGES
    )
    node_xml = "\n".join(
        f'      <circle cx="{x}" cy="{y}" r="{r + 8}" fill="#181430" fill-opacity="0.51"/>\n'
        f'      <circle cx="{x}" cy="{y}" r="{r}" fill="#EEF0F8"/>'
        + (f'\n      <circle cx="{x - 3}" cy="{y - 3}" r="{r - 8}" fill="#FFFFFF"' if r > 20 else "")
        for x, y, r in NODES
    )
    svg = f'''<?xml version="1.0" encoding="UTF-8"?>
<!-- CorteIA Studio - application icon (vector master, 1024x1024).
     Geometry is maintained in assets/make_icons.py; regenerate the rasters with
     `python assets/make_icons.py` after editing either file.
     Mark: play triangle (video) + neural graph (AI) + scissors (cut). -->
<svg xmlns="http://www.w3.org/2000/svg" width="1024" height="1024" viewBox="0 0 1024 1024">
  <defs>
    <linearGradient id="tile" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="#1A1B28"/>
      <stop offset="1" stop-color="#0E0F14"/>
    </linearGradient>
    <linearGradient id="violet" x1="0.22" y1="0.16" x2="0.86" y2="0.92">
      <stop offset="0" stop-color="#AE94FF"/>
      <stop offset="1" stop-color="#5A3ACE"/>
    </linearGradient>
    <linearGradient id="steel" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0" stop-color="#EEF0F8"/>
      <stop offset="1" stop-color="#969EB4"/>
    </linearGradient>
    <radialGradient id="glow" cx="0.504" cy="0.42" r="0.293">
      <stop offset="0" stop-color="#7C5CFF" stop-opacity="0.41"/>
      <stop offset="1" stop-color="#7C5CFF" stop-opacity="0"/>
    </radialGradient>
    <filter id="soft" x="-40%" y="-40%" width="180%" height="180%">
      <feGaussianBlur stdDeviation="9"/>
    </filter>
    <clipPath id="tileClip">
      <rect x="40" y="40" width="944" height="944" rx="188"/>
    </clipPath>
  </defs>

  <rect x="40" y="40" width="944" height="944" rx="188" fill="url(#tile)"/>

  <g clip-path="url(#tileClip)">
    <circle cx="516" cy="430" r="300" fill="url(#glow)"/>

    <!-- play triangle: the round join rounds its corners -->
    <polygon points="{tri}" fill="url(#violet)" stroke="url(#violet)"
             stroke-width="52" stroke-linejoin="round"/>

    <!-- neural graph -->
    <g stroke="#181430" stroke-opacity="0.69" stroke-width="5" stroke-linecap="round">
{edge_xml}
    </g>
    <g>
{node_xml}
    </g>

    <!-- scissors: crossed blades with violet finger rings, cutting in from the
         lower left (SVG rotate is clockwise, the Pillow version mirrors it) -->
    <g transform="translate(388,706) scale(0.86) rotate(-33)">
      <g filter="url(#soft)" stroke="#000000" stroke-opacity="0.38" stroke-linecap="round">
        <line x1="6" y1="6" x2="268" y2="-94" stroke-width="32"/>
        <line x1="6" y1="6" x2="268" y2="106" stroke-width="32"/>
        <line x1="-10" y1="6" x2="-124" y2="88" stroke-width="26"/>
        <line x1="-10" y1="6" x2="-124" y2="-76" stroke-width="26"/>
        <circle cx="-178" cy="126" r="56" fill="none" stroke-width="20"/>
        <circle cx="-178" cy="-126" r="56" fill="none" stroke-width="20"/>
      </g>
      <line x1="-10" y1="0" x2="-147" y2="108" stroke="#7C5CFF" stroke-width="26" stroke-linecap="round"/>
      <line x1="-10" y1="0" x2="-147" y2="-108" stroke="#AE94FF" stroke-width="26" stroke-linecap="round"/>
      <circle cx="-178" cy="126" r="56" fill="none" stroke="#7C5CFF" stroke-width="20"/>
      <circle cx="-178" cy="-126" r="56" fill="none" stroke="#AE94FF" stroke-width="20"/>
      <line x1="0" y1="0" x2="262" y2="-100" stroke="url(#steel)" stroke-width="32" stroke-linecap="round"/>
      <line x1="0" y1="0" x2="262" y2="100" stroke="#969EB4" stroke-width="32" stroke-linecap="round"/>
      <circle cx="0" cy="0" r="24" fill="#EEF0F8"/>
      <circle cx="0" cy="0" r="10" fill="#0E0F14"/>
    </g>
  </g>

  <rect x="41.3" y="41.3" width="941.4" height="941.4" rx="186.7"
        fill="none" stroke="#D2D6E8" stroke-opacity="0.22" stroke-width="2.6"/>
</svg>
'''
    (HERE / "icon.svg").write_text(svg, encoding="utf-8")


def main() -> int:
    HERE.mkdir(parents=True, exist_ok=True)
    img = render_master().convert("RGBA")
    emit_pngs(img)
    emit_ico(img)
    emit_svg()
    emit_contact_sheet(img)
    for p in sorted(HERE.iterdir()):
        if p.suffix in {".png", ".ico", ".svg"}:
            print(f"{p.name:18s} {p.stat().st_size:>9,d} bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
