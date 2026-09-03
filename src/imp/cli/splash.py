# SPDX-License-Identifier: MIT
# Copyright 2026 Josh Poimboeuf
"""The startup splash: a once-printed, randomly-chosen compact pixel-art scene.

The engine math, the ~89-role palette, and the scene builders are ported 1:1 from the
v5 gallery's JavaScript (gallery-50-v5.html); the pool itself is a curated subset of
the gallery's 50, and the phrases have since been rewritten. The dark table is the
gallery's `P` hexes, bar the two creature greys re-tuned for legibility on a dark
terminal; light is re-derived for a near-white surface; ansi maps every role
to one of the 16 named SGR colours.
"""
from __future__ import annotations

import math
import random
import re
from collections.abc import Callable

Color = tuple[int, int, int] | str
Grid = list[list["Color | None"]]
SceneBuilder = Callable[[Grid], None]

W = 30
H = 15

_RESET = "\x1b[0m"
_ITALIC = "\x1b[3m"

_SGR_RE = re.compile(r"\x1b\[[0-9;]*m")


def _visible_width(s: str) -> int:
    return len(_SGR_RE.sub("", s))

# name -> (fg SGR code, bg SGR code) for the 16 named ANSI colors.
_NAMED: dict[str, tuple[int, int]] = {
    "black": (30, 40), "red": (31, 41), "green": (32, 42), "yellow": (33, 43),
    "blue": (34, 44), "magenta": (35, 45), "cyan": (36, 46), "white": (37, 47),
    "bright_black": (90, 100), "bright_red": (91, 101), "bright_green": (92, 102),
    "bright_yellow": (93, 103), "bright_blue": (94, 104),
    "bright_magenta": (95, 105), "bright_cyan": (96, 106), "bright_white": (97, 107),
}

# Locked mascot sprite (15 rows x 16 cols). Legend: o=body, O=horn/dark-edge,
# W=eye glint, b=eye, space=transparent. Verbatim from the gallery IMP array.
IMP: tuple[str, ...] = (
    "   O        O   ", "  O          O  ", "  O          O  ", "   O        O   ",
    "    OO    OO    ",
    "    oooooooo    ", "   oooooooooo   ", "  oooWbooWbooo  ", "  ooobboobbooo  ",
    "  oooooooooooo  ", "  oooooooooooo  ", "   oooooooooo   ", "    oooooooo    ",
    "    oo    oo    ", "   oo      oo   ",
)

# dark = the gallery `P` hexes copied verbatim (plus `tag`, the CSS tagline colour, and
# `voidCore`, the gallery's inline '#06040a' void centre), except `ravenC`/`catC`, which
# the gallery set almost black — lightened here so the creatures read against a dark
# terminal. light/ansi are derived from it.
_DARK: dict[str, Color | None] = {
    "o": (0xFF, 0x9A, 0x3C), "O": (0xC2, 0x61, 0x1F), "W": (0xFF, 0xE9, 0xC8),
    "b": (0x2A, 0x16, 0x06),
    "star": (0x7F, 0xA2, 0xCA), "starD": (0x4A, 0x55, 0x68), "bright": (0xCF, 0xE0, 0xFF),
    "moon": (0xB8, 0xC2, 0xD6), "moonGlow": (0x39, 0x43, 0x5C),
    "port": (0xA8, 0x84, 0xB5), "portG": (0x6A, 0x54, 0x75), "portI": (0xD8, 0xB8, 0xE0),
    "portSwirl": (0x6A, 0x4A, 0x8A), "rune": (0x5E, 0xA0, 0x9E),
    "mtnFar": (0x22, 0x28, 0x38), "mtnMid": (0x2C, 0x33, 0x46), "mtnNear": (0x16, 0x1B, 0x27),
    "snow": (0x9F, 0xB0, 0xC8),
    "fire1": (0xB8, 0x6A, 0x1F), "fire2": (0xD2, 0x95, 0x4A), "fire3": (0xF0, 0xCF, 0x80),
    "fireTip": (0xFF, 0xF0, 0xC0), "ember": (0xFF, 0x9A, 0x3C),
    "flame": (0xFF, 0xB2, 0x4A), "flame2": (0xFF, 0xD9, 0xA0), "wax": (0xCB, 0xB8, 0x9A),
    "waxD": (0x8F, 0x8A, 0x78),
    "pot": (0x4A, 0x45, 0x66), "potD": (0x33, 0x2E, 0x46), "rim": (0x6A, 0x64, 0x88),
    "brew": (0x7B, 0xBF, 0x86), "brewB": (0xB6, 0xE6, 0xBD), "steam": (0x5A, 0x60, 0x72),
    "crysLit": (0x6F, 0xA0, 0xB8), "crysShade": (0x3A, 0x5A, 0x70), "crysTip": (0xCF, 0xEA, 0xF2),
    "crysBase": (0x2A, 0x42, 0x50),
    "mushStem": (0xCD, 0xC6, 0xD2), "cap1": (0xA8, 0x84, 0xB5), "cap1D": (0x7A, 0x5A, 0x90),
    "cap2": (0x5E, 0xA0, 0x9E), "cap2D": (0x3A, 0x66, 0x66),
    "ground": (0x1A, 0x16, 0x22), "groundGlow": (0x24, 0x1C, 0x30), "mushGlow": (0x3A, 0x2F, 0x45),
    "planetA": (0xB0, 0x7A, 0x4A), "planetB": (0xD2, 0xA0, 0x70), "planetC": (0x7A, 0x52, 0x30),
    "ring": (0xC2, 0xCA, 0xD8),
    "torchPole": (0x5A, 0x56, 0x68), "torchBr": (0x46, 0x42, 0x5A), "cloud": (0x2C, 0x33, 0x46),
    "glass": (0x3E, 0x38, 0x60), "orbHi": (0xFF, 0xF4, 0xE8), "orbRim": (0x6A, 0x64, 0x88),
    "orbStand": (0x4A, 0x46, 0x58),
    "lanMetal": (0x6A, 0x66, 0x78), "lanCore": (0xFF, 0xCF, 0x86), "lanGlow": (0xFF, 0x9A, 0x3C),
    "bookPage": (0xAE, 0xB6, 0xC8), "bookPageD": (0x71, 0x7A, 0x90),
    "bookSpine": (0x3A, 0x35, 0x50),
    "bookInk": (0x5B, 0x60, 0x75),
    "boltCore": (0xEA, 0xF2, 0xFF), "boltGlow": (0x7F, 0xA2, 0xCA),
    "spark": (0xFF, 0xCF, 0x86), "wisp": (0x8F, 0xE0, 0xC8), "dim": (0x3F, 0x4A, 0x5E),
    "metal": (0x8A, 0x84, 0x96), "metalD": (0x5A, 0x55, 0x68),
    "ravenC": (0x55, 0x55, 0x6A), "beak": (0xFF, 0xB2, 0x4A),
    "pumpkin": (0xFF, 0x8C, 0x2A), "pumpkinD": (0xC2, 0x61, 0x1F),
    "pumpkinGlow": (0xFF, 0xD9, 0xA0),
    "stem": (0x5A, 0x7A, 0x3A),
    "catC": (0x4E, 0x48, 0x60), "catEye": (0x9B, 0xE8, 0x6A),
    "paper": (0xD9, 0xCB, 0xA8), "paperD": (0xA8, 0x9A, 0x78), "ink": (0x5B, 0x50, 0x40),
    "green": (0x7B, 0xBF, 0x86), "teal": (0x5E, 0xA0, 0x9E), "violet": (0xA8, 0x84, 0xB5),
    "sun": (0xFF, 0xCF, 0x6A), "sunD": (0xFF, 0x9A, 0x3C), "sky": (0x3A, 0x4A, 0x6A),
    "wood": (0x5A, 0x3A, 0x24), "white": (0xE8, 0xE4, 0xF0),
    "tag": (0x9A, 0xA0, 0xA8), "voidCore": (0x06, 0x04, 0x0A),
}
# light: every role re-derived for a near-white surface (best-effort; per-theme tuning is
# a spec non-goal). Same key set as _DARK.
_LIGHT: dict[str, Color | None] = {
    "o": (0xC2, 0x61, 0x1F), "O": (0x7A, 0x3A, 0x0E), "W": (0x8A, 0x5A, 0x20),
    "b": (0x2A, 0x16, 0x06),
    "star": (0x2A, 0x5A, 0x90), "starD": (0x8A, 0x93, 0xA8), "bright": (0x1E, 0x44, 0x7E),
    "moon": (0x5A, 0x64, 0x80), "moonGlow": (0xAA, 0xB0, 0xC0),
    "port": (0x6A, 0x3A, 0x90), "portG": (0x8A, 0x6A, 0xA0), "portI": (0x4A, 0x2A, 0x70),
    "portSwirl": (0x5A, 0x3A, 0x80), "rune": (0x2A, 0x7A, 0x78),
    "mtnFar": (0x9A, 0xA0, 0xB0), "mtnMid": (0x6A, 0x72, 0x86), "mtnNear": (0x4A, 0x52, 0x66),
    "snow": (0xC8, 0xCE, 0xD8),
    "fire1": (0xB8, 0x54, 0x1A), "fire2": (0xA8, 0x70, 0x2A), "fire3": (0x8A, 0x6D, 0x12),
    "fireTip": (0x6A, 0x5A, 0x1A), "ember": (0xC2, 0x61, 0x1F),
    "flame": (0xC2, 0x61, 0x1F), "flame2": (0x8A, 0x6D, 0x2A), "wax": (0x8A, 0x7A, 0x5A),
    "waxD": (0x6A, 0x64, 0x50),
    "pot": (0x4A, 0x46, 0x60), "potD": (0x3A, 0x36, 0x50), "rim": (0x5A, 0x54, 0x78),
    "brew": (0x2A, 0x8A, 0x4A), "brewB": (0x4A, 0xA0, 0x6A), "steam": (0x8A, 0x90, 0xA0),
    "crysLit": (0x3A, 0x7A, 0x98), "crysShade": (0x2A, 0x5A, 0x70), "crysTip": (0x4A, 0x8A, 0xA8),
    "crysBase": (0x2A, 0x4A, 0x5A),
    "mushStem": (0x8A, 0x84, 0x9A), "cap1": (0x6A, 0x3A, 0x90), "cap1D": (0x5A, 0x2A, 0x80),
    "cap2": (0x2A, 0x7A, 0x78), "cap2D": (0x1E, 0x54, 0x52),
    "ground": (0xB6, 0xB6, 0xB6), "groundGlow": (0xA8, 0xA8, 0xA8), "mushGlow": (0xB0, 0xA0, 0xC0),
    "planetA": (0x9A, 0x5A, 0x2A), "planetB": (0x8A, 0x6A, 0x3A), "planetC": (0x6A, 0x3A, 0x1A),
    "ring": (0x6A, 0x72, 0x86),
    "torchPole": (0x5A, 0x56, 0x68), "torchBr": (0x46, 0x42, 0x5A), "cloud": (0x8A, 0x93, 0xA8),
    "glass": (0x4A, 0x46, 0x68), "orbHi": (0x5A, 0x4A, 0x2A), "orbRim": (0x5A, 0x54, 0x78),
    "orbStand": (0x4A, 0x46, 0x58),
    "lanMetal": (0x5A, 0x56, 0x68), "lanCore": (0x8A, 0x6A, 0x1A), "lanGlow": (0xC2, 0x61, 0x1F),
    "bookPage": (0x8A, 0x82, 0x96), "bookPageD": (0x6A, 0x64, 0x78),
    "bookSpine": (0x3A, 0x35, 0x50),
    "bookInk": (0x5B, 0x60, 0x75),
    "boltCore": (0x2A, 0x4A, 0x7A), "boltGlow": (0x2A, 0x5A, 0x90),
    "spark": (0x8A, 0x6A, 0x1A), "wisp": (0x2A, 0x8A, 0x7A), "dim": (0x6A, 0x72, 0x86),
    "metal": (0x5A, 0x56, 0x68), "metalD": (0x3A, 0x36, 0x48),
    "ravenC": (0x2A, 0x2A, 0x36), "beak": (0xC2, 0x61, 0x1F),
    "pumpkin": (0xC2, 0x61, 0x1F), "pumpkinD": (0x8A, 0x3A, 0x0E),
    "pumpkinGlow": (0x8A, 0x6A, 0x2A),
    "stem": (0x3A, 0x5A, 0x1A),
    "catC": (0x26, 0x22, 0x2E), "catEye": (0x3A, 0x8A, 0x2A),
    "paper": (0x8A, 0x7A, 0x5A), "paperD": (0x6A, 0x5A, 0x3A), "ink": (0x3A, 0x30, 0x20),
    "green": (0x2A, 0x8A, 0x4A), "teal": (0x2A, 0x7A, 0x78), "violet": (0x6A, 0x3A, 0x90),
    "sun": (0xC2, 0x61, 0x1F), "sunD": (0xA8, 0x5A, 0x1A), "sky": (0x8A, 0x93, 0xA8),
    "wood": (0x5A, 0x3A, 0x24), "white": (0x4A, 0x46, 0x58),
    "tag": (0x5A, 0x66, 0x78), "voidCore": (0xD8, 0xD8, 0xD8),
}
# ansi: every role mapped to one of the 16 named SGR colours (best-effort). Same key set.
_ANSI_P: dict[str, Color | None] = {
    "o": "bright_yellow", "O": "yellow", "W": "bright_white", "b": "black",
    "star": "blue", "starD": "bright_black", "bright": "bright_white",
    "moon": "white", "moonGlow": "bright_black",
    "port": "magenta", "portG": "bright_black", "portI": "bright_magenta",
    "portSwirl": "magenta", "rune": "cyan",
    "mtnFar": "bright_black", "mtnMid": "bright_black", "mtnNear": "black", "snow": "bright_white",
    "fire1": "red", "fire2": "bright_red", "fire3": "bright_yellow",
    "fireTip": "bright_yellow", "ember": "bright_yellow",
    "flame": "bright_red", "flame2": "bright_yellow", "wax": "white", "waxD": "bright_black",
    "pot": "bright_black", "potD": "bright_black", "rim": "white",
    "brew": "green", "brewB": "bright_green", "steam": "bright_black",
    "crysLit": "cyan", "crysShade": "blue", "crysTip": "bright_cyan", "crysBase": "blue",
    "mushStem": "white", "cap1": "magenta", "cap1D": "bright_black",
    "cap2": "bright_cyan", "cap2D": "cyan",
    "ground": "black", "groundGlow": "bright_black", "mushGlow": "bright_black",
    "planetA": "yellow", "planetB": "bright_yellow", "planetC": "red", "ring": "white",
    "torchPole": "bright_black", "torchBr": "bright_black", "cloud": "bright_black",
    "glass": "blue", "orbHi": "bright_white", "orbRim": "bright_black", "orbStand": "bright_black",
    "lanMetal": "bright_black", "lanCore": "bright_yellow", "lanGlow": "bright_yellow",
    "bookPage": "white", "bookPageD": "bright_black", "bookSpine": "bright_black",
    "bookInk": "bright_black",
    "boltCore": "bright_white", "boltGlow": "blue",
    "spark": "bright_yellow", "wisp": "bright_cyan", "dim": "bright_black",
    "metal": "white", "metalD": "bright_black",
    "ravenC": "bright_black", "beak": "bright_red",
    "pumpkin": "yellow", "pumpkinD": "red", "pumpkinGlow": "bright_yellow", "stem": "green",
    "catC": "bright_black", "catEye": "bright_green",
    "paper": "white", "paperD": "bright_black", "ink": "bright_black",
    "green": "green", "teal": "cyan", "violet": "magenta",
    "sun": "bright_yellow", "sunD": "yellow", "sky": "blue", "wood": "yellow",
    "white": "bright_white",
    "tag": "bright_black", "voidCore": "black",
}


def _build_palette(theme: str) -> dict[str, Color | None]:
    return {"dark": _DARK, "light": _LIGHT, "ansi": _ANSI_P}.get(theme, _ANSI_P)


# The active palette, set by _use_theme before any scene/primitive runs. Scene builders
# are (grid) -> None, so the palette is module state.
_P: dict[str, Color | None] = {}


def _use_theme(theme: str) -> None:
    global _P
    _P = _build_palette(theme)


def _fg_open(c: Color | None) -> str:
    if c is None:
        return ""
    if isinstance(c, tuple):
        return f"\x1b[38;2;{c[0]};{c[1]};{c[2]}m"
    return f"\x1b[{_NAMED[c][0]}m"


def _bg_open(c: Color | None) -> str:
    if c is None:
        return ""
    if isinstance(c, tuple):
        return f"\x1b[48;2;{c[0]};{c[1]};{c[2]}m"
    return f"\x1b[{_NAMED[c][1]}m"


def _make_grid() -> Grid:
    return [[None] * W for _ in range(H)]


def _round(v: float) -> int:
    """Round half toward +infinity, matching JS Math.round for the values used here."""
    return math.floor(v + 0.5)


def _imul(a: int, b: int) -> int:
    return ((a & 0xFFFFFFFF) * (b & 0xFFFFFFFF)) & 0xFFFFFFFF


def _hash(x: int, y: int) -> float:
    """Multiply-xor-shift hash in [0, 1); no RNG, so a chosen scene renders identically."""
    h = (_imul(int(x), 374761393) + _imul(int(y), 668265263)) & 0xFFFFFFFF
    h = _imul(h ^ (h >> 13), 1274126177)
    h ^= h >> 16
    return (h & 0xFFFFFFFF) / 4294967296.0


def _px(g: Grid, x: float, y: float, c: Color | None) -> None:
    xi = _round(x)
    yi = _round(y)
    if c is not None and 0 <= yi < H and 0 <= xi < W:
        g[yi][xi] = c


def _rect(g: Grid, x: int, y: int, w: int, h: int, c: Color | None) -> None:
    for j in range(h):
        for i in range(w):
            _px(g, x + i, y + j, c)


def _disc(g: Grid, cx: float, cy: float, rx: float, ry: float, c: Color | None) -> None:
    for y in range(H):
        for x in range(W):
            dx = (x - cx) / rx
            dy = (y - cy) / ry
            if dx * dx + dy * dy <= 1:
                g[y][x] = c


def _carve(g: Grid, cx: float, cy: float, rx: float, ry: float) -> None:
    for y in range(H):
        for x in range(W):
            dx = (x - cx) / rx
            dy = (y - cy) / ry
            if dx * dx + dy * dy <= 1:
                g[y][x] = None


def _ring(g: Grid, cx: float, cy: float, rx: float, ry: float,
          c: Color | None, inn: float, out: float) -> None:
    for y in range(H):
        for x in range(W):
            dx = (x - cx) / rx
            dy = (y - cy) / ry
            d = dx * dx + dy * dy
            if inn <= d <= out:
                _px(g, x, y, c)


def _line(g: Grid, x0: float, y0: float, x1: float, y1: float, c: Color | None) -> None:
    xi0, yi0, xi1, yi1 = int(x0), int(y0), int(x1), int(y1)
    dx = abs(xi1 - xi0)
    dy = abs(yi1 - yi0)
    sx = 1 if xi0 < xi1 else -1
    sy = 1 if yi0 < yi1 else -1
    e = dx - dy
    while True:
        _px(g, xi0, yi0, c)
        if xi0 == xi1 and yi0 == yi1:
            break
        e2 = 2 * e
        if e2 > -dy:
            e -= dy
            xi0 += sx
        if e2 < dx:
            e += dx
            yi0 += sy


def _spark(g: Grid, x: float, y: float, c: Color | None) -> None:
    _px(g, x, y, c)
    _px(g, x - 1, y, c)
    _px(g, x + 1, y, c)
    _px(g, x, y - 1, c)
    _px(g, x, y + 1, c)


def _stars(g: Grid, max_y: int, dens: float, pad: int = 0, seed: int = 0) -> None:
    """Deterministic scatter of up to round(dens*34) stars, confined to x >= 19.

    Candidates are rejected when they touch an already-placed star or would be the
    third on a row or column, which is what made the raw hash sequence read as
    lines once `max_y` squashed it. `seed` shifts the sequence so scenes do not all
    share one constellation; with `pad`, a star is dropped if anything is already
    painted within that many cells, so a scene that draws its subject first is not
    crowded by stars sitting against it.
    """
    n = _round(dens * 34)
    placed: list[tuple[int, int]] = []
    rows: dict[int, int] = {}
    cols: dict[int, int] = {}
    i = 0
    while len(placed) < n and i < n * 8:
        i += 1
        k = i + seed * 101
        jx = 19 + math.floor(_hash(k, 3) * (W - 19))
        jy = math.floor(_hash(k, 7) * max_y)
        if rows.get(jy, 0) >= 2 or cols.get(jx, 0) >= 2:
            continue
        if any(abs(jx - px) <= 1 and abs(jy - py) <= 1 for px, py in placed):
            continue
        if pad and any(g[yy][xx] is not None
                       for yy in range(max(0, jy - pad), min(H, jy + pad + 1))
                       for xx in range(max(0, jx - pad), min(W, jx + pad + 1))):
            continue
        placed.append((jx, jy))
        rows[jy] = rows.get(jy, 0) + 1
        cols[jx] = cols.get(jx, 0) + 1
        br = _hash(k + 1, 11)
        c = _P["bright"] if br < 0.16 else (_P["star"] if br < 0.58 else _P["starD"])
        _px(g, jx, jy, c)


def _glow_ring(g: Grid, cx: float, cy: float, r: float, c: Color | None) -> None:
    for y in range(H):
        for x in range(W):
            dx = (x - cx) / (r + 2.2)
            dy = (y - cy) / (r + 2.2)
            d = dx * dx + dy * dy
            if 0.6 <= d <= 1.0 and (x + y) & 1 and g[y][x] is None:
                _px(g, x, y, c)


def _despeckle(g: Grid, c: Color | None) -> None:
    """Clear cells of colour `c` with no orthogonal neighbour painted. The dithered
    glow rings can strand a pixel off the shape it belongs to, and a cell touching
    only at a corner still reads as detached at this size."""
    lone = [(x, y) for y in range(H) for x in range(W)
            if g[y][x] == c and not any(
                0 <= y + dy < H and 0 <= x + dx < W and g[y + dy][x + dx] is not None
                for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1)))]
    for x, y in lone:
        g[y][x] = None


def _ground(g: Grid, y: int, c: Color | None) -> None:
    for x in range(W):
        _px(g, x, y, c)


def _imp(g: Grid, ox: int, oy: int) -> None:
    m: dict[str, Color | None] = {"o": _P["o"], "O": _P["O"], "W": _P["W"], "b": _P["b"]}
    for r, row in enumerate(IMP):
        for c, ch in enumerate(row):
            col = m.get(ch)
            if col is not None:
                _px(g, ox + c, oy + r, col)


def _L(g: Grid) -> Grid:
    _imp(g, 2, 0)
    return g


def _cell(top: Color | None, bot: Color | None, *, color: bool) -> str:
    if top is None and bot is None:
        return " "
    if not color:
        return "▀" if top is not None else "▄"
    if top is not None and bot is None:
        return _fg_open(top) + "▀" + _RESET
    if top is None and bot is not None:
        return _fg_open(bot) + "▄" + _RESET
    return _fg_open(top) + _bg_open(bot) + "▀" + _RESET


def _encode_row(g: Grid, top_idx: int, bot_idx: int, *, color: bool) -> str:
    w = len(g[0])
    top_row = g[top_idx] if top_idx < len(g) else [None] * w
    bot_row = g[bot_idx] if bot_idx < len(g) else [None] * w
    return "".join(
        _cell(top_row[x], bot_row[x], color=color) for x in range(w)
    ).rstrip()


def _encode_grid(g: Grid, *, color: bool) -> list[str]:
    return [_encode_row(g, 2 * k, 2 * k + 1, color=color) for k in range((H + 1) // 2)]


# --- prop builders ---


def _crescent(g: Grid, cx: int, cy: int) -> None:
    for dx, dy in [(0, -2), (1, -2), (-1, -1), (0, -1), (-1, 0), (0, 0),
                   (-1, 1), (0, 1), (0, 2), (1, 2)]:
        _px(g, cx + dx, cy + dy, _P["moon"])


def _moon_glow(g: Grid, cx: int, cy: int, r: int, cres: bool) -> None:
    _glow_ring(g, cx, cy, r, _P["moonGlow"])
    _disc(g, cx, cy, r, r, _P["moon"])
    if cres:
        _carve(g, cx + _round(r * 0.7), cy - 1, r, r)


def _cloud(g: Grid, cx: int, cy: int, c: Color | None) -> None:
    _disc(g, cx, cy, 5, 1.5, c)
    _disc(g, cx - 3, cy + 1, 3, 1.4, c)
    _disc(g, cx + 3, cy + 1, 3, 1.4, c)
    _disc(g, cx + 1, cy - 1, 2.4, 1.2, c)


def _planet(g: Grid, cx: int, cy: int, r: int) -> None:
    _disc(g, cx, cy, r, r, _P["planetA"])
    for y in range(cy - r, cy + r + 1):
        if ((y - cy + 99) & 3) == 0 or y == cy - 1:
            for x in range(cx - r, cx + r + 1):
                if 0 <= y < H and 0 <= x < W and g[y][x] is not None:
                    _px(g, x, y, _P["planetB"])
    for y in range(cy - r, cy + r + 1):
        for x in range(cx, cx + r + 1):
            dx = (x - cx) / r
            dy = (y - cy) / r
            if dx * dx + dy * dy <= 1 and dx > 0.38:
                _px(g, x, y, _P["planetC"])
    # a ring seen edge-on: one bar either side, level with the planet's centre.
    # The gallery's dithered band landed as two bars at different heights.
    _ring(g, cx, cy, r + 2.5, r * 0.45, _P["ring"], 0.84, 1.12)


def _void_hole(g: Grid, cx: int, cy: int) -> None:
    _glow_ring(g, cx, cy, 4, _P["ember"])
    _ring(g, cx, cy, 4.6, 2.8, _P["O"], 0.7, 1.1)
    _ring(g, cx, cy, 4.6, 2.8, _P["port"], 0.85, 1.0)
    _disc(g, cx, cy, 2.4, 1.6, _P["voidCore"])


def _portal(g: Grid, cx: int, cy: int) -> None:
    _ring(g, cx, cy, 6, 9, _P["port"], 0.8, 1.08)
    _ring(g, cx, cy, 4.6, 7, _P["portI"], 0.74, 1.08)
    for y in range(cy - 9, cy + 9):
        if not 0 <= y < H:
            continue
        for x in range(cx - 6, cx + 6):
            if not 0 <= x < W:
                continue
            dx = (x - cx) / 6
            dy = (y - cy) / 9
            if dx * dx + dy * dy < 0.62 and g[y][x] is None:
                _px(g, x, y, _P["portSwirl"] if (x + y) & 1 else _P["portG"])
    _spark(g, cx, cy - 5, _P["portI"])


def _orb(g: Grid, cx: int, cy: int, r: int) -> None:
    """A solid rimmed sphere with a swirl and one specular highlight. The gallery's
    dithered rim ring read as a rough halo rather than glass."""
    _disc(g, cx, cy, r, r, _P["orbRim"])
    _disc(g, cx, cy, r - 1, r - 1, _P["glass"])
    _disc(g, cx + 1, cy + 1, r * 0.5, r * 0.5, _P["port"])
    _px(g, cx - 2, cy - 2, _P["orbHi"])
    _px(g, cx - 1, cy - 2, _P["orbHi"])
    _rect(g, cx - 3, cy + r + 1, 7, 1, _P["orbStand"])
    _rect(g, cx - 2, cy + r + 2, 5, 1, _P["orbRim"])


def _cauldron(g: Grid, cx: int, top_y: int) -> None:
    """A rim, a brew surface, a tapering pot on two legs, and a fire under it —
    the gallery's half-ellipse read as a shallow dish rather than a cauldron."""
    for x in range(cx - 5, cx + 6):
        _px(g, x, top_y, _P["potD"] if x in (cx - 5, cx + 5) else _P["rim"])
    for x in range(cx - 4, cx + 5):
        _px(g, x, top_y + 1, _P["brew"] if (x + top_y) & 1 else _P["brewB"])
    for half, dy in ((4, 2), (4, 3), (3, 4)):
        for x in range(cx - half, cx + half + 1):
            _px(g, x, top_y + dy, _P["potD"] if x in (cx - half, cx + half) else _P["pot"])
    _px(g, cx - 3, top_y + 5, _P["potD"])
    _px(g, cx + 3, top_y + 5, _P["potD"])
    for x, c in ((cx - 1, _P["fire2"]), (cx, _P["fire3"]), (cx + 1, _P["fire2"])):
        _px(g, x, top_y + 5, c)
    _px(g, cx, top_y + 6, _P["fire1"])
    _px(g, cx - 3, top_y - 1, _P["brewB"])
    _px(g, cx + 2, top_y - 2, _P["brewB"])
    _px(g, cx - 2, top_y - 4, _P["steam"])
    _px(g, cx + 1, top_y - 6, _P["steam"])


def _lantern(g: Grid, x: int, by: int) -> None:
    _px(g, x + 1, by - 9, _P["lanMetal"])
    _rect(g, x, by - 8, 4, 1, _P["lanMetal"])
    _rect(g, x, by - 7, 1, 6, _P["lanMetal"])
    _rect(g, x + 3, by - 7, 1, 6, _P["lanMetal"])
    for y in range(by - 6, by - 1):
        for xx in range(x + 1, x + 3):
            _px(g, xx, y, _P["lanCore"] if (xx + y) & 1 else _P["lanGlow"])
    _rect(g, x, by - 1, 4, 1, _P["lanMetal"])
    # six glow marks hugging the frame, symmetric about it: the dithered ring the
    # gallery used scatters into loose confetti at this size.
    for dy, dx in ((-2, 3), (0, 4), (2, 3)):
        _px(g, x + 1 - dx, by - 4 + dy, _P["lanGlow"])
        _px(g, x + 2 + dx, by - 4 + dy, _P["lanGlow"])


def _candle(g: Grid, x: int, by: int) -> None:
    for j in range(4):
        _px(g, x, by - j, _P["wax"])
        _px(g, x + 1, by - j, _P["waxD"])
    _px(g, x, by - 4, _P["flame"])
    _px(g, x, by - 5, _P["flame2"])
    _px(g, x, by - 6, _P["W"])


def _torch(g: Grid, x: int, by: int) -> None:
    for j in range(6):
        _px(g, x, by - j, _P["torchPole"])
    _disc(g, x, by - 7, 2, 3, _P["flame"])
    _disc(g, x, by - 7, 1, 2, _P["flame2"])
    _px(g, x, by - 9, _P["W"])


def _rune_sigil(g: Grid, cx: int, cy: int) -> None:
    """Runes carved into a standing stone: an arched slab with two glyphs cut into
    it. Drawn free-floating, the marks read as scattered dots rather than carving."""
    for x in range(cx - 2, cx + 3):
        _px(g, x, cy - 4, _P["metalD"])
    for x in range(cx - 3, cx + 4):
        _px(g, x, cy - 3, _P["metalD"])
    for y in range(cy - 2, cy + 6):
        for x in range(cx - 4, cx + 5):
            _px(g, x, y, _P["metalD"] if x in (cx - 4, cx + 4) or y == cy + 5
                else _P["metal"])
    for y in range(cy - 1, cy + 4):
        _px(g, cx - 2, y, _P["rune"])
        _px(g, cx + 2, y, _P["rune"])
    _px(g, cx - 1, cy - 1, _P["rune"])
    _px(g, cx - 1, cy + 1, _P["rune"])
    _px(g, cx + 1, cy, _P["rune"])
    _px(g, cx + 1, cy + 2, _P["rune"])
    _px(g, cx, cy - 1, _P["rune"])
    _px(g, cx, cy + 3, _P["rune"])


def _wisps(g: Grid) -> None:
    for x, y in [(20, 3), (26, 5), (23, 9), (28, 11)]:
        _px(g, x, y, _P["wisp"])
        _px(g, x - 1, y, _P["teal"])
        _px(g, x + 1, y, _P["teal"])
        _px(g, x, y - 1, _P["teal"])
        _px(g, x, y + 1, _P["teal"])


def _wand(g: Grid, x: int, by: int) -> None:
    for k in range(8):
        _px(g, x + k, by - k, _P["rim"])
    _spark(g, x + 8, by - 8, _P["flame2"])
    _px(g, x + 10, by - 9, _P["spark"])
    _px(g, x + 6, by - 10, _P["spark"])
    _px(g, x + 11, by - 6, _P["spark"])


def _hourglass(g: Grid, cx: int, top_y: int) -> None:
    _rect(g, cx - 3, top_y, 7, 1, _P["wood"])
    _rect(g, cx - 3, top_y + 8, 7, 1, _P["wood"])
    for j in range(4):
        _px(g, cx - 3 + j, top_y + 1 + j, _P["metal"])
        _px(g, cx + 3 - j, top_y + 1 + j, _P["metal"])
    for j in range(4):
        _px(g, cx - 3 + j, top_y + 8 - j, _P["metal"])
        _px(g, cx + 3 - j, top_y + 8 - j, _P["metal"])
    _px(g, cx, top_y + 4, _P["spark"])
    _rect(g, cx - 1, top_y + 2, 3, 1, _P["flame2"])
    _px(g, cx, top_y + 6, _P["flame2"])


def _crystals(g: Grid, by: int, pts: list[tuple[int, int]]) -> None:
    for x, h in pts:
        for j in range(h):
            w = (h - j) // 2
            for dx in range(-w, w + 1):
                _px(g, x + dx, by - j, _P["crysShade"] if dx < 0 else _P["crysLit"])
        _px(g, x, by - h, _P["crysTip"])
        _px(g, x, by - h + 1, _P["crysTip"])


def _quill(g: Grid, x: int, by: int) -> None:
    _rect(g, x, by - 2, 4, 3, _P["rim"])
    _rect(g, x + 1, by - 3, 2, 1, _P["pot"])
    for k in range(7):
        _px(g, x + 2 + k, by - 3 - k, _P["white"])
    for k in range(1, 6):
        _px(g, x + 2 + k - 1, by - 3 - k, _P["bookPageD"])


def _mush(g: Grid, x: int, by: int, h: int, cap: Color | None, cap_d: Color | None) -> None:
    """Stem plus a domed, speckled cap; the gallery's flat disc read as a bar."""
    for j in range(h):
        _px(g, x, by - j, _P["mushStem"])
    top = by - h
    for dx in (-2, -1, 0, 1, 2):
        _px(g, x + dx, top, cap_d if abs(dx) == 2 else cap)
    for dx in (-1, 0, 1):
        _px(g, x + dx, top - 1, cap)
    _px(g, x, top - 2, cap)
    _px(g, x - 1, top - 1, _P["white"])
    _px(g, x + 1, top, _P["white"])


def _campfire(g: Grid, cx: int, by: int) -> None:
    """Crossed logs, a tapering flame, and the imp's stick out over it."""
    _line(g, cx - 4, by, cx + 4, by - 2, _P["wood"])
    _line(g, cx - 4, by - 2, cx + 4, by, _P["wood"])
    for i, (w, c) in enumerate(((3, _P["fire1"]), (3, _P["fire1"]), (2, _P["fire2"]),
                                (2, _P["fire2"]), (1, _P["fire3"]), (0, _P["fireTip"]))):
        for x in range(cx - w, cx + w + 1):
            _px(g, x, by - 1 - i, c)
    _line(g, 16, by - 4, cx - 3, by - 8, _P["paperD"])
    for dx, dy in ((-2, -8), (-1, -8), (-2, -9), (-1, -9)):
        _px(g, cx + dx, by + dy, _P["wax"])


def _firefly(g: Grid, x: int, y: int) -> None:
    _px(g, x, y, _P["spark"])
    _px(g, x - 1, y, _P["O"])
    _px(g, x + 1, y, _P["O"])
    _px(g, x, y - 1, _P["O"])
    _px(g, x, y + 1, _P["O"])


def _spider(g: Grid) -> None:
    # spun symmetrically about column 24: the gallery ran the guys out to x=30,
    # one past the grid, so the right half was clipped
    _line(g, 19, 0, 29, 12, _P["dim"])
    _line(g, 29, 0, 19, 12, _P["dim"])
    _line(g, 24, 0, 24, 12, _P["dim"])
    _line(g, 19, 6, 29, 6, _P["dim"])
    _ring(g, 24, 6, 4, 4, _P["dim"], 0.7, 1.1)
    _disc(g, 24, 6, 1.4, 1.4, _P["ravenC"])
    for d in (-2, -1, 1, 2):
        _px(g, 24 + d, 6 - 2, _P["ravenC"])
        _px(g, 24 + d, 6 + 2, _P["ravenC"])
    _px(g, 23, 6, _P["catEye"])
    _px(g, 25, 6, _P["catEye"])


def _bat(g: Grid, cx: int, cy: int) -> None:
    """Body, ears, and two swept wings; the gallery's line-drawn bat read as a zigzag."""
    c = _P["ravenC"]
    for dx in (-1, 0, 1):
        _px(g, cx + dx, cy, c)
    _px(g, cx, cy + 1, c)
    for dx in (-3, -2, -1, 1, 2, 3):
        _px(g, cx + dx, cy - 1, c)
    _px(g, cx - 4, cy, c)
    _px(g, cx + 4, cy, c)


def _raven(g: Grid, x: int, by: int) -> None:
    _ground(g, by + 1, _P["bookSpine"])
    _disc(g, x, by - 2, 2.4, 2, _P["ravenC"])
    _disc(g, x + 2, by - 4, 1.4, 1.4, _P["ravenC"])
    _px(g, x + 4, by - 4, _P["beak"])
    _px(g, x + 1, by - 4, _P["bright"])
    _line(g, x - 2, by - 1, x - 5, by + 0, _P["ravenC"])
    _px(g, x, by + 1, _P["ravenC"])
    _px(g, x + 1, by + 1, _P["ravenC"])


def _mirror(g: Grid, cx: int, cy: int) -> None:
    """A hanging oval glass with a small imp reflected in it, and a shine across
    the surface. A rectangular frame on a stand reads as a monitor at this size;
    the oval and the shine are what make it a mirror."""
    _disc(g, cx, cy, 4.5, 6.5, _P["metal"])
    _disc(g, cx, cy, 3.4, 5.4, _P["glass"])
    _px(g, cx - 2, cy - 3, _P["O"])
    _px(g, cx + 2, cy - 3, _P["O"])
    for dy in (-2, -1, 0):
        for xx in range(cx - 2, cx + 3):
            _px(g, xx, cy + dy, _P["o"])
    _px(g, cx - 1, cy - 1, _P["b"])
    _px(g, cx + 1, cy - 1, _P["b"])
    for k in range(3):
        _px(g, cx + 2 - k, cy + 2 + k, _P["orbHi"])


def _cat(g: Grid, x: int, by: int) -> None:
    _ground(g, by + 1, _P["bookSpine"])
    _disc(g, x, by - 2, 2.6, 3, _P["catC"])
    _px(g, x - 2, by - 5, _P["catC"])
    _px(g, x - 3, by - 6, _P["catC"])
    _px(g, x + 2, by - 5, _P["catC"])
    _px(g, x + 3, by - 6, _P["catC"])
    _px(g, x - 1, by - 3, _P["catEye"])
    _px(g, x + 1, by - 3, _P["catEye"])
    for k in range(5):
        _px(g, x + 3 + k, by - (2 if k > 2 else 0), _P["catC"])


def _coffee(g: Grid, x: int, by: int) -> None:
    _rect(g, x, by - 4, 5, 4, _P["metal"])
    _rect(g, x + 1, by - 3, 3, 1, _P["wood"])
    for hx, hy in ((5, -4), (6, -3), (6, -2), (5, -1)):   # a closed C handle
        _px(g, x + hx, by + hy, _P["metal"])
    _px(g, x + 1, by - 6, _P["steam"])
    _px(g, x + 3, by - 7, _P["steam"])
    _px(g, x + 2, by - 8, _P["steam"])


def _terminal_win(g: Grid, x: int, y: int) -> None:
    _rect(g, x, y, 12, 8, _P["bookSpine"])
    _rect(g, x + 1, y + 1, 10, 6, _P["ground"])
    _rect(g, x + 1, y + 1, 10, 1, _P["metalD"])
    _px(g, x + 2, y + 3, _P["green"])
    _px(g, x + 3, y + 3, _P["green"])
    _px(g, x + 5, y + 3, _P["bookPageD"])
    _px(g, x + 2, y + 5, _P["o"])
    _px(g, x + 4, y + 5, _P["bright"])


def _diff_panel(g: Grid, x: int, y: int) -> None:
    """A compact diff: one removed line above one added line. Both gutter marks sit
    on the same three columns and both code bars on the same four, so the pair reads
    as two rows of one hunk rather than as loose bars."""
    _rect(g, x, y, 12, 10, _P["bookSpine"])
    _rect(g, x + 1, y + 1, 10, 8, _P["ground"])
    _rect(g, x + 1, y + 1, 10, 1, _P["metalD"])
    _px(g, x + 2, y + 1, _P["bookPageD"])

    # Removed line: a minus in the gutter and warm code to its right.
    _rect(g, x + 2, y + 3, 3, 1, _P["fire1"])
    _rect(g, x + 6, y + 3, 4, 1, _P["fire1"])
    # Added line: a three-stroke plus on the minus's columns, green code beside it.
    _px(g, x + 3, y + 5, _P["green"])
    _rect(g, x + 2, y + 6, 3, 1, _P["green"])
    _px(g, x + 3, y + 7, _P["green"])
    _rect(g, x + 6, y + 6, 4, 1, _P["green"])


def _desk(g: Grid, x: int, by: int) -> None:
    """A desk: a top on two legs, the book on the imp's side and the candle beyond
    it. The full-height `_candle` overwhelms a desk this size, so it is a stub."""
    for xx in range(x, x + 11):
        _px(g, xx, by, _P["wood"])
    for dy in (1, 2):
        _px(g, x + 1, by + dy, _P["wood"])
        _px(g, x + 9, by + dy, _P["wood"])
    for xx in range(x + 1, x + 4):
        _px(g, xx, by - 1, _P["O"])
    _px(g, x + 1, by - 2, _P["bookPage"])
    _px(g, x + 2, by - 2, _P["bookPage"])
    _px(g, x + 3, by - 2, _P["bookPageD"])
    _px(g, x + 8, by - 1, _P["wax"])
    _px(g, x + 8, by - 2, _P["wax"])
    _px(g, x + 8, by - 3, _P["flame"])
    _px(g, x + 8, by - 4, _P["flame2"])
    _px(g, x + 7, by - 3, _P["W"])


def _beetle(g: Grid, cx: int, cy: int) -> None:
    _disc(g, cx, cy, 3, 2, _P["ravenC"])
    _px(g, cx, cy - 3, _P["ravenC"])
    _line(g, cx, cy - 2, cx, cy + 2, _P["O"])
    for dy in (-1, 0, 1):
        _px(g, cx - 4, cy + dy * 2, _P["ravenC"])
        _px(g, cx + 4, cy + dy * 2, _P["ravenC"])
        _line(g, cx - 3, cy + dy, cx - 4, cy + dy * 2, _P["ravenC"])
        _line(g, cx + 3, cy + dy, cx + 4, cy + dy * 2, _P["ravenC"])
    _px(g, cx, cy - 4, _P["O"])


def _dice(g: Grid, x: int, by: int) -> None:
    def die(dx: int, pips: list[tuple[int, int]]) -> None:
        _rect(g, dx, by - 5, 5, 5, _P["white"])
        for i in range(5):
            _px(g, dx + i, by - 5, _P["metalD"])
            _px(g, dx + i, by - 1, _P["metalD"])
            _px(g, dx, by - 5 + i, _P["metalD"])
            _px(g, dx + 4, by - 5 + i, _P["metalD"])
        for a, b in pips:
            _px(g, dx + a, by - 5 + b, _P["b"])
    die(x, [(2, 2)])
    die(x + 6, [(1, 1), (3, 3)])
    _ground(g, by + 1, _P["bookSpine"])


def _sunrise(g: Grid) -> None:
    """Sun half-risen behind a ridge. The ground is laid last so the disc cannot
    leak below the horizon — carving it left holes and a stray pixel under the ridge."""
    hor = 11
    _disc(g, 24, hor - 1, 3.6, 3.6, _P["sun"])
    for a in range(-60, 61, 30):
        t = a * math.pi / 180
        _px(g, 24 + math.sin(t) * 6, hor - 1 - math.cos(t) * 6, _P["sunD"])
    for x in range(18, W):
        h = _round(2 + 1.5 * math.sin(x * 0.4))
        for y in range(hor, H):
            _px(g, x, y, _P["mtnNear"] if y <= hor + h else _P["mtnMid"])


def _s_crescent_stars(g: Grid) -> None:
    _crescent(g, 24, 4)
    _stars(g, 10, 0.5, pad=1, seed=1)
    _L(g)


def _s_full_moon_cloud(g: Grid) -> None:
    _moon_glow(g, 24, 5, 4, False)
    _cloud(g, 24, 8, _P["steam"])
    _despeckle(g, _P["moonGlow"])
    _stars(g, 8, 0.4, pad=1, seed=2)
    _L(g)


def _s_constellation(g: Grid) -> None:
    """A dipper: a handle rising from the left into a four-star bowl. The gallery's
    open zigzag read as a broken chain rather than a figure."""
    pts = [(18, 9), (20, 8), (22, 6), (24, 5), (27, 4), (28, 7), (25, 8)]
    for a, b in ((0, 1), (1, 2), (2, 3), (3, 4), (4, 5), (5, 6), (6, 3)):
        _line(g, pts[a][0], pts[a][1], pts[b][0], pts[b][1], _P["starD"])
    for i, (x, y) in enumerate(pts):
        _px(g, x, y, _P["bright"] if i in (0, 4, 5) else _P["star"])
    _stars(g, H, 0.16, pad=1, seed=3)
    _L(g)


def _s_ringed_planet(g: Grid) -> None:
    _planet(g, 24, 7, 3)
    _stars(g, H, 0.18, pad=1, seed=4)
    _L(g)


def _s_comet(g: Grid) -> None:
    # a warm tail thickening toward the head and cooling to starlight behind it;
    # the head sits clear of the corner so the whole thing reads as a comet.
    for k in range(1, 11):
        _px(g, 27 - k, 3 + k,
            _P["flame2"] if k < 3 else (_P["ember"] if k < 6 else _P["starD"]))
        if 1 < k < 6:
            _px(g, 27 - k, 2 + k, _P["ember"] if k < 4 else _P["starD"])
    _disc(g, 27, 3, 1.4, 1.4, _P["flame2"])
    _px(g, 26, 4, _P["ember"])
    _px(g, 27, 3, _P["bright"])
    _stars(g, H, 0.26, pad=1, seed=5)
    _L(g)


def _s_starfield(g: Grid) -> None:
    _spark(g, 24, 4, _P["bright"])
    _spark(g, 27, 9, _P["star"])
    _stars(g, H, 0.5, pad=2, seed=6)
    _L(g)


def _s_void(g: Grid) -> None:
    _void_hole(g, 23, 7)
    _stars(g, H, 0.18, pad=1, seed=7)
    _L(g)


def _s_summoning_circle(g: Grid) -> None:
    _ring(g, 14, 12, 13, 2.3, _P["port"], 0.74, 1.08)
    _ring(g, 14, 12, 9, 1.6, _P["portG"], 0.66, 1.16)
    for a in range(0, 360, 45):
        t = a * math.pi / 180
        _px(g, 14 + math.cos(t) * 13, 12 + math.sin(t) * 2.3, _P["rune"])
    _stars(g, 8, 0.35, pad=1, seed=8)
    _L(g)


def _s_portal(g: Grid) -> None:
    _portal(g, 24, 7)
    _stars(g, 8, 0.3, pad=1, seed=9)
    _L(g)


def _s_crystal_ball(g: Grid) -> None:
    _orb(g, 24, 7, 4)
    _stars(g, 7, 0.28, pad=1, seed=10)
    _L(g)


def _s_cauldron(g: Grid) -> None:
    _cauldron(g, 23, 7)
    _stars(g, 7, 0.28, pad=1, seed=11)
    _L(g)


def _s_lantern(g: Grid) -> None:
    _lantern(g, 21, 13)
    _stars(g, 7, 0.3, pad=1, seed=12)
    _L(g)


def _s_twin_candles(g: Grid) -> None:
    _candle(g, 21, 13)
    _candle(g, 27, 13)
    _stars(g, 8, 0.3, pad=1, seed=13)
    _L(g)


def _s_twin_torches(g: Grid) -> None:
    _torch(g, 20, 13)
    _torch(g, 27, 13)
    _stars(g, 8, 0.34, pad=1, seed=14)
    _L(g)


def _s_rune_sigil(g: Grid) -> None:
    _rune_sigil(g, 24, 7)
    _stars(g, 7, 0.26, pad=1, seed=15)
    _L(g)


def _s_floating_wisps(g: Grid) -> None:
    _wisps(g)
    _stars(g, H, 0.2, pad=1, seed=16)
    _L(g)


def _s_magic_wand(g: Grid) -> None:
    _wand(g, 19, 12)
    _stars(g, 8, 0.28, pad=1, seed=17)
    _L(g)


def _s_hourglass(g: Grid) -> None:
    _hourglass(g, 24, 3)
    _stars(g, 7, 0.26, pad=1, seed=18)
    _L(g)


def _s_crystal_cluster(g: Grid) -> None:
    _crystals(g, 13, [(20, 5), (23, 7), (27, 6), (30, 4)])
    _ground(g, 14, _P["crysBase"])
    _stars(g, 8, 0.28, pad=1, seed=19)
    _L(g)


def _s_will_o_wisp(g: Grid) -> None:
    # a four-pointed light: dim tips, mint arms, pale core. Corners make it read
    # as a box, and a halo or scattered motes read as more starfield.
    for dx, dy in ((0, -2), (0, 2), (-2, 0), (2, 0)):
        _px(g, 24 + dx, 8 + dy, _P["teal"])
    _spark(g, 24, 8, _P["wisp"])
    _px(g, 24, 8, _P["bright"])
    _stars(g, H, 0.18, pad=1, seed=20)
    _L(g)


def _s_quill_ink(g: Grid) -> None:
    _quill(g, 20, 12)
    _stars(g, 7, 0.24, pad=1, seed=21)
    _L(g)


def _s_glowing_mushrooms(g: Grid) -> None:
    _ground(g, 13, _P["ground"])
    _mush(g, 21, 13, 4, _P["cap1"], _P["cap1D"])
    _mush(g, 26, 13, 5, _P["cap2"], _P["cap2D"])
    _stars(g, 8, 0.3, pad=1, seed=22)
    _L(g)


def _s_campfire(g: Grid) -> None:
    _ground(g, 14, _P["mtnNear"])
    _campfire(g, 24, 13)
    _stars(g, 8, 0.34, pad=1, seed=23)
    _L(g)


def _s_fireflies(g: Grid) -> None:
    for x, y in [(20, 3), (26, 5), (22, 8), (28, 7), (24, 11)]:
        _firefly(g, x, y)
    _stars(g, 6, 0.18, pad=1, seed=24)
    _L(g)


def _s_spider_web(g: Grid) -> None:
    _spider(g)
    _L(g)


def _s_bats(g: Grid) -> None:
    _bat(g, 22, 3)
    _bat(g, 27, 6)
    _bat(g, 24, 10)
    _stars(g, H, 0.22, pad=1, seed=26)
    _L(g)


def _s_raven(g: Grid) -> None:
    _raven(g, 24, 12)
    _stars(g, 8, 0.24, pad=1, seed=27)
    _L(g)


def _s_mirror(g: Grid) -> None:
    _mirror(g, 24, 7)
    _stars(g, 6, 0.2, pad=1, seed=28)
    _L(g)


def _s_black_cat(g: Grid) -> None:
    _cat(g, 25, 12)
    _stars(g, 8, 0.24, pad=1, seed=29)
    _L(g)


def _s_coffee(g: Grid) -> None:
    _coffee(g, 22, 12)
    _stars(g, 6, 0.2, pad=1, seed=31)
    _L(g)


def _s_terminal(g: Grid) -> None:
    _terminal_win(g, 19, 3)
    _stars(g, 5, 0.18, pad=1, seed=32)
    _L(g)


def _s_diff(g: Grid) -> None:
    # The panel sits low, its foot level with the imp's, which leaves the sky above it
    # deep enough for a real scatter; `pad` keeps those stars off the panel's roof.
    _diff_panel(g, 18, 5)
    _stars(g, 5, 0.16, pad=1, seed=35)
    _L(g)


def _s_desk_book(g: Grid) -> None:
    _desk(g, 19, 11)
    _stars(g, 6, 0.2, pad=1, seed=33)
    _L(g)


def _s_the_bug(g: Grid) -> None:
    _beetle(g, 24, 7)
    _stars(g, 6, 0.18, pad=1, seed=34)
    _L(g)


def _s_open_tome(g: Grid) -> None:
    """One tome, open: parchment pages fanning up and out from a wooden spine.
    Flat grey pages on a base read as an altar rather than a book."""
    for y, (a, b) in ((6, (19, 20)), (7, (19, 21)), (8, (19, 22)),
                      (9, (19, 23)), (10, (19, 23))):
        for x in range(a, b + 1):
            _px(g, x, y, _P["paper"])
        for x in range(48 - b, 48 - a + 1):
            _px(g, x, y, _P["paper"])
        _px(g, a, y, _P["paperD"])
        _px(g, 48 - a, y, _P["paperD"])
    for y in range(9, 12):
        _px(g, 24, y, _P["wood"])
    for x in range(20, 29):
        _px(g, x, 11, _P["wood"])
    for x in (21, 22):
        _px(g, x, 9, _P["ink"])
    for x in (26, 27):
        _px(g, x, 9, _P["ink"])
    for x in (20, 21, 22):
        _px(g, x, 10, _P["ink"])
    for x in (26, 27, 28):
        _px(g, x, 10, _P["ink"])
    for x in range(22, 27):
        _px(g, x, 12, _P["metalD"])
    _px(g, 24, 13, _P["metalD"])
    _px(g, 23, 14, _P["metalD"])
    _px(g, 24, 14, _P["metalD"])
    _px(g, 25, 14, _P["metalD"])
    _stars(g, 6, 0.22, pad=1, seed=34)
    _L(g)


def _s_loaded_dice(g: Grid) -> None:
    _dice(g, 19, 13)
    _stars(g, 6, 0.22, pad=1, seed=36)
    _L(g)


def _s_sunrise(g: Grid) -> None:
    for x in range(18, W):
        for y in range(6):
            if (x + y) % 5 == 0:
                _px(g, x, y, _P["sky"])
    _sunrise(g)
    _L(g)


# --- scene pool: (builder, phrase) ---

SCENES: list[tuple[SceneBuilder, str]] = [
    (_s_crescent_stars, "the familiar awakens."),
    (_s_full_moon_cloud, "the night is thine."),
    (_s_constellation, "the omens are aligning."),
    (_s_ringed_planet, "a world, from nothing."),
    (_s_comet, "the heavens blaze forth. name your wish."),
    (_s_starfield, "speak — your imp is listening."),
    (_s_void, "conjured from the void."),
    (_s_summoning_circle, "the circle's drawn. what do we summon?"),
    (_s_portal, "the portal is open. step through."),
    (_s_crystal_ball, "gaze in — what shall we build?"),
    (_s_cauldron, "stir the cauldron — what's brewing?"),
    (_s_lantern, "the lantern's lit. lead the way."),
    (_s_twin_candles, "lean in. whisper into the flame."),
    (_s_twin_torches, "the torches are lit. let's begin."),
    (_s_rune_sigil, "the runes are ready. what shall we carve?"),
    (_s_floating_wisps, "the wisps know the way."),
    (_s_magic_wand, "speak, and it shall be built."),
    (_s_hourglass, "the sand is falling. time to begin."),
    (_s_crystal_cluster, "from nothing, something."),
    (_s_will_o_wisp, "a light beckons in the dark."),
    (_s_quill_ink, "the quill is dipped. say the word."),
    (_s_glowing_mushrooms, "the toadstools are glowing. take your pick."),
    (_s_campfire, "by emberlight, we code."),
    (_s_fireflies, "daemons, at your command."),
    (_s_spider_web, "what shall we catch?"),
    (_s_bats, "creatures of the night, assemble."),
    (_s_raven, "tell me what haunts your codebase."),
    (_s_mirror, "the abyss gazes. what do you see?"),
    (_s_black_cat, "be still, the night brings counsel."),
    (_s_coffee, "another night, another diff."),
    (_s_terminal, "mischief, compiled."),
    (_s_diff, "summon your next diff."),
    (_s_desk_book, "coding, by candlelight."),
    (_s_the_bug, "something stirs, waiting expectantly."),
    (_s_open_tome, "the tome is open. choose a spell."),
    (_s_loaded_dice, "the dice are cast."),
    (_s_sunrise, "the day is thine."),
]

MIN_WIDTH = 32


def choose_index() -> int:
    return random.randrange(len(SCENES))


def scene_lines(index: int, *, theme: str, color: bool) -> list[str]:
    """Deterministically render scene `index` to half-block lines."""
    _use_theme(theme)
    g = _make_grid()
    SCENES[index][0](g)
    return _encode_grid(g, color=color)


def splash_block(*, theme: str, width: int, color: bool) -> str:
    """The opening splash: a random scene over a centered grey-blue
    italic tagline (centered relative to the art's rendered width)."""
    index = choose_index()
    phrase = SCENES[index][1]
    if not color or width < MIN_WIDTH:
        return phrase
    lines = scene_lines(index, theme=theme, color=color)
    art_w = max((_visible_width(ln) for ln in lines), default=0)
    pad = " " * max(0, (art_w - len(phrase)) // 2)
    tag = pad + _fg_open(_build_palette(theme)["tag"]) + _ITALIC + phrase + _RESET
    return "\n".join(lines) + "\n" + tag
