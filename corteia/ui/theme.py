"""Tema da interface: paleta, QSS, fontes, ícones vetoriais e utilitários.

A aparência vem 100% dos arquivos ``resources/qss/{dark,light}.qss``. Cada
arquivo começa com um bloco de *tokens* (``@bg``, ``@surface``, ``@accent`` ...);
:func:`apply` lê o QSS, substitui os tokens pelos valores do modo/acento
escolhidos e aplica o resultado no ``QApplication`` — assim um único corpo de
QSS atende aos dois temas. Não usamos ``QPalette``.

Ícones são desenhados na hora com ``QPainter`` sobre um grid normalizado de
24x24 (nenhum pacote de ícones externo) e cacheados por ``(nome, cor, tamanho)``.
"""

from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QFontDatabase,
    QIcon,
    QImage,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
)

from .. import paths

# --------------------------------------------------------------------------- #
# paleta
# --------------------------------------------------------------------------- #
DARK: dict[str, str] = {
    "bg": "#0E0F14",
    "surface": "#15171E",
    "surface2": "#1B1E27",
    "surface3": "#222633",
    "border": "#242836",
    "border_strong": "#343A4E",
    "text": "#E8EAF2",
    "text_dim": "#C2C7D6",
    "muted": "#9AA0B5",
    "faint": "#6B7186",
    "accent": "#7C5CFF",
    "success": "#22E07A",
    "warn": "#FFB020",
    "error": "#FF5C7A",
    "info": "#4EC5FF",
    "on_accent": "#FFFFFF",
    "hover": "rgba(255, 255, 255, 26)",
    "press": "rgba(255, 255, 255, 40)",
    "track": "rgba(255, 255, 255, 15)",
    "overlay": "rgba(8, 9, 13, 190)",
    "shadow": "rgba(0, 0, 0, 120)",
    "scrollbar": "rgba(255, 255, 255, 38)",
    "scrollbar_hover": "rgba(255, 255, 255, 70)",
}

LIGHT: dict[str, str] = {
    "bg": "#F3F5FA",
    "surface": "#FFFFFF",
    "surface2": "#EAEFF7",
    "surface3": "#DDE3F0",
    "border": "#D9DEEA",
    "border_strong": "#BCC3D6",
    "text": "#161821",
    "text_dim": "#3B4152",
    "muted": "#5D6478",
    "faint": "#8A90A2",
    "accent": "#6B4BE8",
    "success": "#0F9D58",
    "warn": "#B26A00",
    "error": "#D93757",
    "info": "#1479B4",
    "on_accent": "#FFFFFF",
    "hover": "rgba(22, 26, 42, 18)",
    "press": "rgba(22, 26, 42, 34)",
    "track": "rgba(22, 26, 42, 22)",
    "overlay": "rgba(243, 245, 250, 205)",
    "shadow": "rgba(30, 35, 60, 40)",
    "scrollbar": "rgba(22, 26, 42, 60)",
    "scrollbar_hover": "rgba(22, 26, 42, 105)",
}

_MODE = "dark"
_ACCENT = DARK["accent"]

#: hierarquia tipográfica (px)
FS_TITLE = 20
FS_SUB = 13
FS_BODY = 14
FS_SMALL = 12
FS_MONO = 12


def set_mode(mode: str, accent: str | None = None) -> None:
    """Registra o modo corrente usado por :func:`color` e :func:`icon`."""
    global _MODE, _ACCENT
    _MODE = "light" if str(mode).lower().startswith("l") else "dark"
    if accent:
        _ACCENT = accent if accent.startswith("#") else f"#{accent}"


def mode() -> str:
    return _MODE


def is_dark() -> bool:
    return _MODE == "dark"


def color(name: str) -> str:
    """Valor do token ``name`` no modo ativo (com fallback no outro modo)."""
    table = LIGHT if _MODE == "light" else DARK
    if name == "accent":
        return _ACCENT
    if name in table:
        return table[name]
    other = DARK if _MODE == "light" else LIGHT
    return other.get(name, "#FF00FF")


def qcolor(name: str) -> QColor:
    return QColor(color(name))


# --------------------------------------------------------------------------- #
# fontes
# --------------------------------------------------------------------------- #
_UI_CANDIDATES = ("Segoe UI", "Inter", "Noto Sans", "Ubuntu", "Cantarell", "DejaVu Sans")
_MONO_CANDIDATES = ("Cascadia Mono", "JetBrains Mono", "Consolas", "DejaVu Sans Mono",
                    "Noto Sans Mono", "Liberation Mono")
_FAMILY_CACHE: dict[str, str] = {}


def _available_families() -> set[str]:
    try:
        return {f.lower() for f in QFontDatabase.families()}
    except Exception:  # noqa: BLE001 - QFontDatabase exige QApplication
        return set()


def font_family(mono: bool = False) -> str:
    """Primeira família instalada da lista de preferência ("" se nenhuma)."""
    key = "mono" if mono else "ui"
    if key not in _FAMILY_CACHE:
        avail = _available_families()
        cands = _MONO_CANDIDATES if mono else _UI_CANDIDATES
        _FAMILY_CACHE[key] = next((c for c in cands if c.lower() in avail), "")
    return _FAMILY_CACHE[key]


def family_list(mono: bool = False) -> str:
    """Lista ``"A", "B", sans-serif`` para uso dentro do QSS."""
    cands = _MONO_CANDIDATES if mono else _UI_CANDIDATES
    parts = [f'"{c}"' for c in cands]
    return ", ".join(parts) + (", monospace" if mono else ", sans-serif")


_WEIGHTS = {
    "light": QFont.Weight.Light,
    "normal": QFont.Weight.Normal,
    "medium": QFont.Weight.Medium,
    "demibold": QFont.Weight.DemiBold,
    "semibold": QFont.Weight.DemiBold,
    "bold": QFont.Weight.Bold,
}


def font(size: int | float = FS_BODY, weight: str = "normal") -> QFont:
    """Fonte da UI com fallback de famílias resolvido pelo Qt."""
    f = QFont()
    fam = font_family()
    if fam:
        f.setFamily(fam)
    f.setPointSize(int(size))
    f.setWeight(_WEIGHTS.get(str(weight).lower(), QFont.Weight.Normal))
    f.setHintingPreference(QFont.HintingPreference.PreferFullHinting)
    return f


def mono_font(size: int | float = FS_MONO, weight: str = "normal") -> QFont:
    """Fonte monoespaçada para tempos, caminhos e números técnicos."""
    f = QFont()
    fam = font_family(mono=True)
    if fam:
        f.setFamily(fam)
    else:
        f.setStyleHint(QFont.StyleHint.TypeWriter)
    f.setPointSize(int(size))
    f.setWeight(_WEIGHTS.get(str(weight).lower(), QFont.Weight.Normal))
    return f


# --------------------------------------------------------------------------- #
# ícones vetoriais (QPainter, grid 24x24)
# --------------------------------------------------------------------------- #
def _round_rect_path(x, y, w, h, r):
    path = QPainterPath()
    path.addRoundedRect(QRectF(x, y, w, h), r, r)
    return path


def _stroke(p: QPainter, *pts: tuple[float, float]) -> None:
    pen = p.pen()
    path = QPainterPath()
    path.moveTo(QPointF(*pts[0]))
    for pt in pts[1:]:
        path.lineTo(QPointF(*pt))
    p.drawPath(path)
    p.setPen(pen)


def _fill(p: QPainter, *pts: tuple[float, float]) -> None:
    path = QPainterPath()
    path.moveTo(QPointF(*pts[0]))
    for pt in pts[1:]:
        path.lineTo(QPointF(*pt))
    path.closeSubpath()
    p.drawPath(path)


def _oval(p: QPainter, x: float, y: float, w: float, h: float, filled: bool = False) -> None:
    brush = p.brush()
    if filled:
        p.setBrush(QBrush(p.pen().color()))
    p.drawEllipse(QRectF(x, y, w, h))
    p.setBrush(brush)


def _ring(p: QPainter, cx: float, cy: float, r: float) -> None:
    p.drawEllipse(QPointF(cx, cy), r, r)


def _dot(p: QPainter, cx: float, cy: float, r: float = 1.1) -> None:
    brush = p.brush()
    p.setBrush(QBrush(p.pen().color()))
    p.drawEllipse(QPointF(cx, cy), r, r)
    p.setBrush(brush)


def _box(p: QPainter, x: float, y: float, w: float, h: float, r: float = 2.0) -> None:
    p.drawPath(_round_rect_path(x, y, w, h, r))


def _home(p: QPainter) -> None:
    _stroke(p, (3.2, 11.2), (12, 3.8), (20.8, 11.2))
    _box(p, 5.6, 10.6, 12.8, 9.4, 1.6)
    _box(p, 10.1, 13.8, 3.8, 6.2, 1.0)


def _import(p: QPainter) -> None:
    _stroke(p, (12, 3.4), (12, 14.2))
    _stroke(p, (7.6, 10.0), (12, 14.6), (16.4, 10.0))
    _stroke(p, (3.6, 16.4), (3.6, 20.4), (20.4, 20.4), (20.4, 16.4))


def _export(p: QPainter) -> None:
    _stroke(p, (12, 15.4), (12, 3.4))
    _stroke(p, (7.8, 7.6), (12, 3.2), (16.2, 7.6))
    _stroke(p, (3.6, 14.0), (3.6, 20.4), (20.4, 20.4), (20.4, 14.0))


def _download(p: QPainter) -> None:
    _stroke(p, (12, 3.6), (12, 15.0))
    _stroke(p, (8.0, 11.2), (12, 15.4), (16.0, 11.2))
    _stroke(p, (4.0, 19.0), (20.0, 19.0))


def _subtitles(p: QPainter) -> None:
    _box(p, 2.6, 5.0, 18.8, 14.0, 2.6)
    pen = p.pen()
    thin = QPen(pen.color(), max(1.4, pen.widthF() * 0.85))
    thin.setCapStyle(Qt.PenCapStyle.RoundCap)
    p.setPen(thin)
    _stroke(p, (6.4, 10.6), (10.6, 10.6))
    _stroke(p, (6.4, 13.8), (13.6, 13.8))
    p.setPen(pen)


def _crop(p: QPainter) -> None:
    _stroke(p, (7.2, 2.4), (7.2, 16.8), (21.6, 16.8))
    _stroke(p, (2.4, 7.2), (16.8, 7.2), (16.8, 21.6))


def _edit(p: QPainter) -> None:
    _stroke(p, (4.2, 19.8), (5.6, 15.4), (16.4, 4.8))
    _stroke(p, (13.6, 2.2), (16.3, 4.9), (19.0, 7.6), (16.3, 10.3), (13.6, 7.6), (16.3, 4.9))
    _stroke(p, (13.0, 8.2), (10.4, 5.6))
    _stroke(p, (4.2, 19.8), (8.6, 18.6))


def _gear(p: QPainter) -> None:
    pen = p.pen()
    p.save()
    p.translate(12, 12)
    for i in range(8):
        p.save()
        p.rotate(i * 45)
        _stroke(p, (0, -7.4), (0, -10.2))
        p.restore()
    p.restore()
    _ring(p, 12, 12, 6.6)
    _ring(p, 12, 12, 2.6)
    p.setPen(pen)


def _play(p: QPainter) -> None:
    brush = p.brush()
    p.setBrush(QBrush(p.pen().color()))
    _fill(p, (7.4, 4.4), (7.4, 19.6), (20.0, 12.0))
    p.setBrush(brush)


def _pause(p: QPainter) -> None:
    brush = p.brush()
    p.setBrush(p.pen().color())
    _box(p, 6.2, 4.4, 4.2, 15.2, 1.4)
    _box(p, 13.6, 4.4, 4.2, 15.2, 1.4)
    p.setBrush(brush)


def _stop(p: QPainter) -> None:
    brush = p.brush()
    p.setBrush(p.pen().color())
    _box(p, 5.6, 5.6, 12.8, 12.8, 2.2)
    p.setBrush(brush)


def _check(p: QPainter) -> None:
    _stroke(p, (4.0, 12.6), (9.4, 18.0), (20.0, 6.2))


def _close(p: QPainter) -> None:
    _stroke(p, (5.4, 5.4), (18.6, 18.6))
    _stroke(p, (18.6, 5.4), (5.4, 18.6))


def _plus(p: QPainter) -> None:
    _stroke(p, (12, 4.6), (12, 19.4))
    _stroke(p, (4.6, 12), (19.4, 12))


def _minus(p: QPainter) -> None:
    _stroke(p, (4.6, 12), (19.4, 12))


def _folder(p: QPainter) -> None:
    _stroke(p, (3.0, 19.4), (3.0, 5.6), (9.4, 5.6), (11.2, 8.0), (21.0, 8.0),
            (21.0, 19.4), (3.0, 19.4))


def _trash(p: QPainter) -> None:
    _stroke(p, (4.0, 6.6), (20.0, 6.6))
    _stroke(p, (9.6, 4.2), (14.4, 4.2))
    _stroke(p, (9.6, 6.6), (9.6, 4.2), (14.4, 4.2), (14.4, 6.6))
    _box(p, 5.8, 6.6, 12.4, 13.6, 1.8)
    _stroke(p, (9.6, 9.8), (9.6, 17.0))
    _stroke(p, (14.4, 9.8), (14.4, 17.0))


def _wand(p: QPainter) -> None:
    _stroke(p, (3.6, 20.4), (14.0, 10.0))
    _stroke(p, (5.4, 8.4), (5.4, 5.0))
    _stroke(p, (3.7, 6.7), (7.1, 6.7))
    _stroke(p, (17.6, 6.4), (17.6, 3.2))
    _stroke(p, (16.0, 4.8), (19.2, 4.8))
    _dot(p, 10.4, 4.4, 0.9)


def _sparkle(p: QPainter) -> None:
    brush = p.brush()
    p.setBrush(p.pen().color())
    _fill(p, (10.4, 2.6), (12.2, 7.4), (17.0, 9.2), (12.2, 11.0), (10.4, 15.8),
          (8.6, 11.0), (3.8, 9.2), (8.6, 7.4))
    _fill(p, (18.4, 13.6), (19.3, 16.0), (21.7, 16.9), (19.3, 17.8), (18.4, 20.2),
          (17.5, 17.8), (15.1, 16.9), (17.5, 16.0))
    p.setBrush(brush)


def _gpu(p: QPainter) -> None:
    _box(p, 2.6, 5.6, 18.8, 12.8, 2.4)
    _ring(p, 12, 12.0, 3.9)
    _oval(p, 10.6, 10.6, 2.8, 2.8, True)
    _stroke(p, (6.6, 18.4), (6.6, 21.0))
    _stroke(p, (12.0, 18.4), (12.0, 21.0))
    _stroke(p, (17.4, 18.4), (17.4, 21.0))


def _cpu(p: QPainter) -> None:
    _box(p, 6.2, 6.2, 11.6, 11.6, 2.0)
    _box(p, 9.6, 9.6, 4.8, 4.8, 1.0)
    for x in (8.8, 12.0, 15.2):
        _stroke(p, (x, 3.0), (x, 6.2))
        _stroke(p, (x, 17.8), (x, 21.0))
        _stroke(p, (3.0, x), (6.2, x))
        _stroke(p, (17.8, x), (21.0, x))


def _clock(p: QPainter) -> None:
    _ring(p, 12, 12, 8.6)
    _stroke(p, (12, 12), (12, 6.9))
    _stroke(p, (12, 12), (16.0, 14.2))


def _history(p: QPainter) -> None:
    pen = p.pen()
    p.drawArc(QRectF(3.4, 3.4, 17.2, 17.2), 40 * 16, 300 * 16)
    p.setPen(pen)
    _stroke(p, (12, 12), (12, 7.4))
    _stroke(p, (12, 12), (15.6, 13.8))
    _stroke(p, (3.2, 2.6), (3.2, 7.2), (7.8, 7.2))


def _search(p: QPainter) -> None:
    _ring(p, 10.6, 10.6, 6.2)
    _stroke(p, (15.2, 15.2), (20.2, 20.2))


def _refresh(p: QPainter) -> None:
    p.drawArc(QRectF(4.0, 4.0, 16.0, 16.0), 300 * 16, 380 * 16)
    brush = p.brush()
    p.setBrush(QBrush(p.pen().color()))
    _fill(p, (17.8, 1.6), (20.6, 7.0), (14.8, 6.2))
    p.setBrush(brush)


def _star(p: QPainter) -> None:
    brush = p.brush()
    p.setBrush(p.pen().color())
    cx, cy, ro, ri = 12.0, 12.4, 9.0, 4.0
    pts = []
    for i in range(10):
        r = ro if i % 2 == 0 else ri
        a = math.radians(-90 + i * 36)
        pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    _fill(p, *pts)
    p.setBrush(brush)


def _video(p: QPainter) -> None:
    _box(p, 2.4, 6.0, 14.8, 12.0, 2.4)
    _fill(p, (17.6, 10.2), (21.6, 7.4), (21.6, 16.6), (17.6, 13.8))


def _film(p: QPainter) -> None:
    _box(p, 2.6, 4.6, 18.8, 14.8, 2.2)
    _stroke(p, (7.4, 4.8), (7.4, 19.2))
    _stroke(p, (16.6, 4.8), (16.6, 19.2))
    for y in (7.6, 12.0, 16.4):
        _dot(p, 5.0, y, 1.0)
        _dot(p, 19.0, y, 1.0)


def _chart(p: QPainter) -> None:
    _stroke(p, (3.4, 20.2), (20.6, 20.2))
    _box(p, 5.4, 12.0, 3.6, 7.4, 1.0)
    _box(p, 10.6, 7.4, 3.6, 12.0, 1.0)
    _box(p, 15.8, 10.2, 3.6, 9.2, 1.0)


def _back(p: QPainter) -> None:
    _stroke(p, (15.0, 4.6), (7.2, 12.0), (15.0, 19.4))


def _forward(p: QPainter) -> None:
    _stroke(p, (9.0, 4.6), (16.8, 12.0), (9.0, 19.4))


def _chevron_down(p: QPainter) -> None:
    _stroke(p, (5.6, 9.4), (12.0, 15.6), (18.4, 9.4))


def _chevron_up(p: QPainter) -> None:
    _stroke(p, (5.6, 14.6), (12.0, 8.4), (18.4, 14.6))


def _chevron_right(p: QPainter) -> None:
    _stroke(p, (9.4, 5.6), (15.6, 12.0), (9.4, 18.4))


def _warning(p: QPainter) -> None:
    p.drawPolygon([QPointF(12, 3.2), QPointF(21.6, 19.8), QPointF(2.4, 19.8)])
    _stroke(p, (12, 9.0), (12, 14.2))
    _dot(p, 12, 17.0, 1.0)


def _info(p: QPainter) -> None:
    _ring(p, 12, 12, 8.8)
    _stroke(p, (12, 11.0), (12, 16.6))
    _dot(p, 12, 7.6, 1.05)


def _link(p: QPainter) -> None:
    pen = p.pen()
    p.drawArc(QRectF(3.2, 8.8, 9.6, 6.4), 90 * 16, 180 * 16)
    p.drawArc(QRectF(11.2, 8.8, 9.6, 6.4), -90 * 16, 180 * 16)
    p.setPen(pen)
    _stroke(p, (8.4, 12.0), (15.6, 12.0))


def _eye(p: QPainter) -> None:
    p.drawArc(QRectF(1.8, 5.4, 20.4, 13.2), 20 * 16, 140 * 16)
    p.drawArc(QRectF(1.8, 5.4, 20.4, 13.2), 200 * 16, 140 * 16)
    _ring(p, 12, 12, 3.2)


def _eye_off(p: QPainter) -> None:
    _stroke(p, (4.0, 3.6), (20.0, 20.4))
    _eye(p)


def _scissors(p: QPainter) -> None:
    _ring(p, 6.4, 18.0, 2.6)
    _ring(p, 17.6, 18.0, 2.6)
    _stroke(p, (8.2, 15.6), (18.4, 4.0))
    _stroke(p, (15.8, 15.6), (5.6, 4.0))


def _save(p: QPainter) -> None:
    _box(p, 3.4, 3.4, 17.2, 17.2, 2.2)
    _box(p, 8.0, 3.4, 8.0, 5.6, 1.0)
    _box(p, 6.8, 13.0, 10.4, 7.6, 1.4)


def _dot_icon(p: QPainter) -> None:
    _dot(p, 12, 12, 3.2)


def _caret_down(p: QPainter) -> None:
    brush = p.brush()
    p.setBrush(QBrush(p.pen().color()))
    _fill(p, (5.6, 8.6), (18.4, 8.6), (12.0, 16.0))
    p.setBrush(brush)


def _caret_up(p: QPainter) -> None:
    brush = p.brush()
    p.setBrush(QBrush(p.pen().color()))
    _fill(p, (5.6, 15.4), (18.4, 15.4), (12.0, 8.0))
    p.setBrush(brush)


def _caret_right(p: QPainter) -> None:
    brush = p.brush()
    p.setBrush(QBrush(p.pen().color()))
    _fill(p, (8.6, 5.6), (16.0, 12.0), (8.6, 18.4))
    p.setBrush(brush)


def _fallback(p: QPainter, name: str = "") -> None:
    _box(p, 4.4, 4.4, 15.2, 15.2, 3.0)


_ICON_FUNCS: dict[str, Callable[[QPainter], None]] = {
    "home": _home, "import": _import, "export": _export, "download": _download,
    "subtitles": _subtitles, "subtitle": _subtitles, "cc": _subtitles,
    "crop": _crop, "edit": _edit, "settings": _gear, "gear": _gear,
    "play": _play, "pause": _pause, "stop": _stop, "check": _check,
    "close": _close, "plus": _plus, "minus": _minus, "folder": _folder,
    "trash": _trash, "wand": _wand, "magic": _sparkle, "sparkle": _sparkle,
    "ai": _sparkle, "gpu": _gpu, "cpu": _cpu, "clock": _clock,
    "history": _history, "search": _search, "refresh": _refresh, "star": _star,
    "video": _video, "film": _film, "chart": _chart, "back": _back,
    "forward": _forward, "chevron_down": _chevron_down, "chevron_up": _chevron_up,
    "chevron_right": _chevron_right, "warning": _warning, "info": _info,
    "link": _link, "eye": _eye, "eye_off": _eye_off, "scissors": _scissors,
    "save": _save, "dot": _dot_icon,
    "caret_down": _caret_down, "caret_up": _caret_up, "caret_right": _caret_right,
}

#: nomes exigidos pelo contrato de arquitetura
ICON_NAMES = (
    "home", "import", "magic", "subtitles", "crop", "edit", "export", "settings",
    "play", "pause", "stop", "check", "close", "plus", "folder", "trash", "wand",
    "gpu", "cpu", "clock", "search", "refresh", "star", "download", "video",
    "chart", "back", "forward", "warning", "info",
)

_PIXMAP_CACHE: dict[tuple[str, str, int, float], QPixmap] = {}


def pixmap(name: str, color_name: str | None = None, size: int = 22,
           stroke: float | None = None) -> QPixmap:
    """Desenha (ou recupera do cache) um ícone vetorial em ``QPixmap``."""
    key = (name, str(color_name), int(size), float(stroke or 0))
    cached = _PIXMAP_CACHE.get(key)
    if cached is not None and not cached.isNull():
        return cached

    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
    p.scale(size / 24.0, size / 24.0)
    col = QColor(resolve_color(color_name, "text_dim"))
    if not col.isValid():
        col = qcolor("text_dim")
    pen = QPen(col)
    pen.setWidthF(stroke if stroke else max(1.5, size * 0.078))
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    p.setPen(pen)
    p.setBrush(QBrush(Qt.BrushStyle.NoBrush))
    fn = _ICON_FUNCS.get(name)
    if fn is None:
        _fallback(p)
    else:
        fn(p)
    p.end()
    _PIXMAP_CACHE[key] = pm
    return pm


def icon(name: str, color: str | None = None, size: int = 22) -> QIcon:
    """Ícone vetorial para botões e labels; ``color`` é um token ou hex."""
    return QIcon(pixmap(name, color, size))


def icon_on_accent(name: str, size: int = 22) -> QIcon:
    return QIcon(pixmap(name, color("on_accent"), size))


def token_color(value: str) -> str:
    """Aceita token da paleta (``"accent"``), ``"@accent"`` ou cor literal."""
    if not value:
        return color("text")
    if value.startswith("@"):
        return color(value[1:])
    table = LIGHT if _MODE == "light" else DARK
    if value in table:
        return table[value]
    return value


def resolve_color(value: str | None, default_token: str = "text") -> str:
    if not value:
        return color(default_token)
    return token_color(value)


# --------------------------------------------------------------------------- #
# QSS
# --------------------------------------------------------------------------- #
def resources_root() -> Path:
    return paths.resources_dir()


def qss_path(mode: str | None = None) -> Path:
    name = "light" if (mode or _MODE).lower().startswith("l") else "dark"
    return resources_root() / "qss" / f"{name}.qss"


def _shade(hex_color: str, amount: float) -> str:
    """``amount`` > 0 clareia, < 0 escurece (proporção do espaço RGB)."""
    c = QColor(hex_color)
    if not c.isValid():
        return hex_color
    h, s, v, _a = c.getHsl()
    v = max(0, min(255, int(v * (1 + amount))))
    s = max(0, min(255, int(s * (1 - abs(amount) * 0.25))))
    out = QColor.fromHsl(h, s, v, 255)
    return out.name().upper()


def _rgba(hex_color: str, alpha: int) -> str:
    c = QColor(hex_color)
    return f"rgba({c.red()}, {c.green()}, {c.blue()}, {int(alpha)})"


def parse_defines(text: str) -> dict[str, str]:
    """Lê o bloco ``@define`` do topo de um QSS (as variáveis do arquivo)."""
    import re

    out: dict[str, str] = {}
    for match in re.finditer(r"@define\s+([A-Za-z_][A-Za-z0-9_]*)\s+(.+?)\s*$",
                             text, re.MULTILINE | re.IGNORECASE):
        out[match.group(1)] = match.group(2)
    return out


def palette_tokens(mode: str, accent: str, extra: dict[str, str] | None = None
                   ) -> dict[str, str]:
    """Mapa ``@token -> valor`` usado para expandir o QSS.

    O arquivo QSS manda: os ``@define`` dele sobrescrevem a paleta padrão e o
    acento configurado pelo usuário sobrescreve o ``accent`` do arquivo.
    """
    base = dict(LIGHT if mode == "light" else DARK)
    if extra:
        base.update(extra)
    base["accent"] = accent
    dark = mode != "light"
    base.update({
        "accent_hover": _shade(accent, 0.14 if dark else 0.06),
        "accent_press": _shade(accent, -0.14 if dark else -0.10),
        "accent_soft": _rgba(accent, 44 if dark else 30),
        "accent_softer": _rgba(accent, 24 if dark else 16),
        "accent_ring": _rgba(accent, 120 if dark else 80),
        "success_soft": _rgba(base["success"], 38 if dark else 26),
        "warn_soft": _rgba(base["warn"], 40 if dark else 28),
        "error_soft": _rgba(base["error"], 40 if dark else 26),
        "info_soft": _rgba(base["info"], 38 if dark else 26),
        "ui_font": family_list(mono=False),
        "mono_font": family_list(mono=True),
        "radius": "10px",
    })
    for name, path in ensure_assets(mode, accent).items():
        base[f"img_{name}"] = path.as_posix()
    return base


def load_qss(mode: str = "dark", accent: str | None = None) -> str:
    """Lê o QSS, expande os tokens e avisa se sobrar algum sem valor."""
    accent = accent or (LIGHT if mode == "light" else DARK)["accent"]
    text = qss_path(mode).read_text(encoding="utf-8")
    tokens = palette_tokens(mode, accent, extra=parse_defines(text))
    # chave mais longa primeiro: evita que "@accent" estrague "@accent_soft"
    for key, value in sorted(tokens.items(), key=lambda kv: -len(kv[0])):
        text = text.replace(f"@{key}", value)
    body = re.sub(r"/\*.*?\*/", "", text, flags=re.S)   # comentários fora da checagem
    leftover = [m.group(0) for m in re.finditer(r"@[A-Za-z][A-Za-z0-9_]*", body)]
    if leftover:                                   # nunca deve acontecer
        import logging

        logging.getLogger("corteia").warning("tokens QSS sem valor: %s", sorted(set(leftover)))
    return text


def apply(app, mode: str | None = None, accent: str | None = None) -> str:
    """Aplica tema + fonte base no ``QApplication`` e devolve o QSS usado."""
    resolved_mode = (mode or _MODE)
    resolved_mode = "light" if str(resolved_mode).lower().startswith("l") else "dark"
    if not accent:
        accent = (LIGHT if resolved_mode == "light" else DARK)["accent"]
    set_mode(resolved_mode, accent)
    try:
        app.setStyle("Fusion")
    except Exception:  # noqa: BLE001 - estilo pode não existir na plataforma
        pass
    qss = load_qss(resolved_mode, accent)
    app.setStyleSheet(qss)
    app.setFont(font(FS_BODY))
    return qss


# --------------------------------------------------------------------------- #
# utilitários de imagem
# --------------------------------------------------------------------------- #
def rounded_pixmap(src: QPixmap, radius: int = 10, size: tuple[int, int] | None = None
                   ) -> QPixmap:
    """Recorta e arredonda uma miniatura (usado em cards e previews)."""
    if src.isNull():
        out = QPixmap(size[0] if size else 160, size[1] if size else 90)
        out.fill(qcolor("surface3"))
        return out
    target = size or (src.width(), src.height())
    scaled = src.scaled(target[0], target[1],
                        Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                        Qt.TransformationMode.SmoothTransformation)
    out = QPixmap(target[0], target[1])
    out.fill(Qt.GlobalColor.transparent)
    p = QPainter(out)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    clip = QPainterPath()
    clip.addRoundedRect(QRectF(0, 0, target[0], target[1]), radius, radius)
    p.setClipPath(clip)
    p.drawPixmap((target[0] - scaled.width()) // 2, (target[1] - scaled.height()) // 2, scaled)
    p.end()
    return out


def pixmap_from_file(path: str | Path, size: tuple[int, int], radius: int = 10) -> QPixmap | None:
    """Carrega uma miniatura do disco já arredondada; ``None`` se não existir."""
    p = Path(path)
    if not p.is_file():
        return None
    src = QPixmap(str(p))
    if src.isNull():
        return None
    return rounded_pixmap(src, radius, size)


def placeholder_pixmap(size: tuple[int, int], icon_name: str = "video",
                       label: str = "") -> QPixmap:
    """Bloco honesto usado quando ainda não existe miniatura real."""
    pm = QPixmap(max(24, size[0]), max(24, size[1]))
    pm.fill(qcolor("surface3"))
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    icon_size = max(18, min(pm.width(), pm.height()) // 3)
    p.drawPixmap((pm.width() - icon_size) // 2,
                 (pm.height() - icon_size) // 2 - (10 if label else 0),
                 pixmap(icon_name, "faint", icon_size))
    if label:
        f = font(FS_SMALL)
        p.setFont(f)
        p.setPen(qcolor("faint"))
        p.drawText(QRectF(4, pm.height() * 0.55, pm.width() - 8, pm.height() * 0.4),
                   Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop, label)
    p.end()
    return rounded_pixmap(pm, 10, size)


def scale_to_width(path: str | Path, width: int) -> QImage | None:
    """Abre uma imagem apenas na largura pedida (economia de RAM)."""
    p = Path(path)
    if not p.is_file():
        return None
    img = QImage(str(p))
    if img.isNull():
        return None
    return img.scaledToWidth(width, Qt.TransformationMode.SmoothTransformation)


# --------------------------------------------------------------------------- #
# formatação para o usuário
# --------------------------------------------------------------------------- #
def format_duration_long(seconds: float) -> str:
    """``3725`` -> ``1 h 02 min``; ``83`` -> ``1 min 23 s``."""
    total = int(round(max(0.0, float(seconds or 0.0))))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h} h {m:02d} min"
    if m:
        return f"{m} min {s:02d} s"
    return f"{s} s"


def relative_time(ts: float) -> str:
    """Data relativa em português ("há 3 dias"); ``ts`` é epoch."""
    import time as _time

    if not ts:
        return "nunca"
    delta = max(0.0, _time.time() - float(ts))
    if delta < 60:
        return "agora mesmo"
    if delta < 3600:
        return f"há {int(delta // 60)} min"
    if delta < 86400:
        return f"há {int(delta // 3600)} h"
    if delta < 86400 * 7:
        return f"há {int(delta // 86400)} dias"
    return _time.strftime("%d/%m/%Y", _time.localtime(ts))


def date_time(ts: float) -> str:
    """Data+hora absoluta do sistema."""
    import time as _time

    if not ts:
        return "-"
    return _time.strftime("%d/%m/%Y %H:%M", _time.localtime(ts))


# --------------------------------------------------------------------------- #
# abrir caminhos no sistema (real: xdg-open / explorer / open)
# --------------------------------------------------------------------------- #
def open_location(path: str | Path, *, reveal: bool = False) -> tuple[bool, str]:
    """Abre pasta/arquivo no gerenciador do sistema.

    Retorna ``(ok, explicacao)`` — a UI mostra a explicação quando falha, nunca
    fica em silêncio. ``reveal=True`` tenta selecionar o arquivo (Windows/macOS).
    """
    import os
    import subprocess
    import sys

    from PySide6.QtCore import QUrl
    from PySide6.QtGui import QDesktopServices

    p = Path(path)
    if not str(path):
        return False, "Caminho não informado."
    if not p.exists():
        return False, f"O caminho não existe mais: {p}"

    if reveal and not p.is_dir():
        if sys.platform == "win32":
            try:
                subprocess.Popen(["explorer", "/select,", str(p)])
                return True, ""
            except OSError as exc:
                return False, f"Falha ao abrir o Explorador: {exc}"
        if sys.platform == "darwin":
            try:
                subprocess.Popen(["open", "-R", str(p)])
                return True, ""
            except OSError as exc:
                return False, f"Falha ao abrir o Finder: {exc}"
        # Linux: sem padrão de "selecionar"; abrimos o diretório pai
        return open_location(p.parent)

    if sys.platform == "win32":
        try:
            os.startfile(str(p))  # type: ignore[attr-defined]
            return True, ""
        except OSError as exc:
            return False, f"Falha ao abrir: {exc}"
    ok = QDesktopServices.openUrl(QUrl.fromLocalFile(str(p)))
    if ok:
        return True, ""
    try:
        subprocess.Popen(["xdg-open", str(p)], stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
        return True, ""
    except OSError as exc:
        return False, f"Nenhum programa do sistema abriu {p} ({exc})."


# --------------------------------------------------------------------------- #
# assets PNG exigidos pelo QSS (indicadores de checkbox/radio/setas)
# --------------------------------------------------------------------------- #
#: (nome do token, ícone, tamanho em px, espessura do traço)
_ASSET_SPECS = (
    ("check", "check", 20, 2.9),
    ("radio", "dot", 20, 2.0),
    ("arrow_down", "caret_down", 16, 1.6),
    ("arrow_up", "caret_up", 16, 1.6),
    ("arrow_combo", "caret_down", 20, 1.5),
    ("branch_open", "caret_down", 16, 1.8),
    ("branch_close", "caret_right", 16, 1.8),
)


def assets_dir(mode: str) -> Path:
    d = paths.cache_dir() / "qss" / ("dark" if mode != "light" else "light")
    try:
        d.mkdir(parents=True, exist_ok=True)
    except OSError:  # pragma: no cover - sistema de arquivos somente leitura
        pass
    return d


def ensure_assets(mode: str, accent: str) -> dict[str, Path]:
    """Gera (e cacheia em disco) os PNGs referenciados pelo QSS."""
    out: dict[str, Path] = {}
    base = LIGHT if mode == "light" else DARK
    tint = {"check": accent, "radio": accent, "arrow_down": base["muted"],
            "arrow_up": base["muted"], "arrow_combo": base["muted"],
            "branch_open": base["muted"], "branch_close": base["muted"]}
    d = assets_dir(mode)
    for name, icon_name, size, stroke in _ASSET_SPECS:
        target = d / f"{name}_{QColor(tint[name]).rgb():x}.png"
        out[name] = target
        if target.exists():
            continue
        try:
            pm = pixmap(icon_name, tint[name], size, stroke=stroke)
            pm.save(str(target), "PNG")
        except OSError:  # pragma: no cover - sem permissão de escrita
                continue
    return out
