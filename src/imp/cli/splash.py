"""The startup splash: a once-printed, randomly-chosen compact pixel-art scene.

A verbatim port of the v5 gallery (gallery-50-v5.html): the engine math, the ~89-role
palette, and the scene builders are translated 1:1 from the gallery's JavaScript. The
dark table is the gallery's `P` hexes; light is re-derived for a near-white surface;
ansi maps every role to one of the 16 named SGR colours.
"""
from __future__ import annotations

import datetime
import math
import random
from collections.abc import Callable

Color = tuple[int, int, int] | str
Grid = list[list["Color | None"]]
SceneBuilder = Callable[[Grid], None]

W = 30
H = 15

_RESET = "\x1b[0m"
_ITALIC = "\x1b[3m"

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
# `voidCore`, the gallery's inline '#06040a' void centre). light/ansi are derived from it.
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
    "pot": (0x34, 0x30, 0x4A), "potD": (0x21, 0x1E, 0x30), "rim": (0x4A, 0x45, 0x64),
    "brew": (0x7B, 0xBF, 0x86), "brewB": (0xB6, 0xE6, 0xBD), "steam": (0x5A, 0x60, 0x72),
    "crysLit": (0x6F, 0xA0, 0xB8), "crysShade": (0x3A, 0x5A, 0x70), "crysTip": (0xCF, 0xEA, 0xF2),
    "crysBase": (0x2A, 0x42, 0x50),
    "mushStem": (0xCD, 0xC6, 0xD2), "cap1": (0xA8, 0x84, 0xB5), "cap1D": (0x7A, 0x5A, 0x90),
    "cap2": (0x9A, 0x6A, 0xD0), "cap2D": (0x6A, 0x4A, 0x90),
    "ground": (0x1A, 0x16, 0x22), "groundGlow": (0x24, 0x1C, 0x30), "mushGlow": (0x3A, 0x2F, 0x45),
    "planetA": (0xB0, 0x7A, 0x4A), "planetB": (0xD2, 0xA0, 0x70), "planetC": (0x7A, 0x52, 0x30),
    "ring": (0xC2, 0xCA, 0xD8),
    "torchPole": (0x5A, 0x56, 0x68), "torchBr": (0x46, 0x42, 0x5A), "cloud": (0x2C, 0x33, 0x46),
    "glass": (0x2C, 0x28, 0x40), "orbHi": (0xFF, 0xF4, 0xE8), "orbRim": (0x6A, 0x64, 0x88),
    "orbStand": (0x4A, 0x46, 0x58),
    "lanMetal": (0x6A, 0x66, 0x78), "lanCore": (0xFF, 0xCF, 0x86), "lanGlow": (0xFF, 0x9A, 0x3C),
    "bookPage": (0xAE, 0xB6, 0xC8), "bookPageD": (0x71, 0x7A, 0x90),
    "bookSpine": (0x3A, 0x35, 0x50),
    "bookInk": (0x5B, 0x60, 0x75),
    "boltCore": (0xEA, 0xF2, 0xFF), "boltGlow": (0x7F, 0xA2, 0xCA),
    "spark": (0xFF, 0xCF, 0x86), "wisp": (0x8F, 0xE0, 0xC8), "dim": (0x3F, 0x4A, 0x5E),
    "metal": (0x8A, 0x84, 0x96), "metalD": (0x5A, 0x55, 0x68),
    "ravenC": (0x2A, 0x2A, 0x36), "beak": (0xFF, 0xB2, 0x4A),
    "pumpkin": (0xFF, 0x8C, 0x2A), "pumpkinD": (0xC2, 0x61, 0x1F),
    "pumpkinGlow": (0xFF, 0xD9, 0xA0),
    "stem": (0x5A, 0x7A, 0x3A),
    "catC": (0x26, 0x22, 0x2E), "catEye": (0x9B, 0xE8, 0x6A),
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
    "cap2": (0x6A, 0x3A, 0xA0), "cap2D": (0x4A, 0x2A, 0x80),
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
    "pot": "bright_black", "potD": "black", "rim": "bright_black",
    "brew": "green", "brewB": "bright_green", "steam": "bright_black",
    "crysLit": "cyan", "crysShade": "blue", "crysTip": "bright_cyan", "crysBase": "blue",
    "mushStem": "white", "cap1": "magenta", "cap1D": "bright_black",
    "cap2": "bright_magenta", "cap2D": "magenta",
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
    "catC": "black", "catEye": "bright_green",
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


def _stars(g: Grid, max_y: int, dens: float) -> None:
    """Deterministic scatter of round(dens*34) stars, confined to x >= 19."""
    n = _round(dens * 34)
    for i in range(n):
        jx = 19 + math.floor(_hash(i + 1, 3) * (W - 19))
        jy = math.floor(_hash(i + 1, 7) * max_y)
        br = _hash(i + 2, 11)
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


def _meteor(g: Grid, x: int, y: int, length: int) -> None:
    for k in range(length):
        t = k / length
        _px(g, x + k, y - k, _P["bright"] if t < 0.4 else _P["star"])
    _spark(g, x, y, _P["bright"])


def _planet(g: Grid, cx: int, cy: int, r: int) -> None:
    _disc(g, cx, cy, r, r, _P["planetA"])
    for y in range(cy - r, cy + r + 1):
        if ((y - cy + 99) & 3) == 0:
            for x in range(cx - r, cx + r + 1):
                if 0 <= y < H and 0 <= x < W and g[y][x] is not None:
                    _px(g, x, y, _P["planetB"])
    for y in range(cy - r, cy + r + 1):
        for x in range(cx, cx + r + 1):
            dx = (x - cx) / r
            dy = (y - cy) / r
            if dx * dx + dy * dy <= 1 and dx > 0.38:
                _px(g, x, y, _P["planetC"])
    _ring(g, cx, cy, r + 2.5, r * 0.45, _P["ring"], 0.84, 1.12)


def _mountains(g: Grid, by: int, x0: int = 0) -> None:
    for x in range(x0, W):
        h = _round(4 + 2.5 * math.sin(x * 0.16) + 1.5 * math.sin(x * 0.07 + 1))
        for y in range(by - h, by + 1):
            _px(g, x, y, _P["mtnMid"])
        if h >= 7:
            _px(g, x, by - h, _P["snow"])
    for x in range(x0, W):
        h = _round(2 + 1.5 * math.sin(x * 0.13 + 4))
        for y in range(by - h, by + 1):
            _px(g, x, y, _P["mtnNear"])


def _aurora(g: Grid) -> None:
    cols = [_P["green"], _P["teal"], _P["violet"]]
    for x in range(18, W):
        for b in range(3):
            y = 2 + b * 2 + _round(1.5 * math.sin(x * 0.5 + b))
            if (x + b) & 1:
                _px(g, x, y, cols[b])
    _px(g, 22, 8, _P["star"])
    _px(g, 27, 9, _P["bright"])


# --- scene builders (imp drawn last so it sits above any full-width ground) ---

def _s_crescent_stars(g: Grid) -> None:
    _stars(g, 10, 0.5)
    _crescent(g, 24, 4)
    _L(g)


def _s_crescent_perch(g: Grid) -> None:
    _stars(g, 12, 0.5)
    _crescent(g, 24, 8)
    _L(g)


def _s_full_moon_cloud(g: Grid) -> None:
    _stars(g, 8, 0.4)
    _moon_glow(g, 24, 5, 4, False)
    _cloud(g, 24, 8, _P["steam"])
    _L(g)


def _s_meteor_shower(g: Grid) -> None:
    _stars(g, H, 0.24)
    _meteor(g, 19, 3, 5)
    _meteor(g, 25, 7, 4)
    _meteor(g, 22, 12, 4)
    _px(g, 28, 2, _P["bright"])
    _L(g)


def _s_constellation(g: Grid) -> None:
    _stars(g, H, 0.16)
    p = [(19, 3), (23, 2), (26, 5), (24, 8), (28, 9)]
    for k in range(len(p) - 1):
        _line(g, p[k][0], p[k][1], p[k + 1][0], p[k + 1][1], _P["dim"])
    for i, (x, y) in enumerate(p):
        _px(g, x, y, _P["star"] if i % 2 else _P["bright"])
    _L(g)


def _s_ringed_planet(g: Grid) -> None:
    _stars(g, H, 0.18)
    _planet(g, 24, 7, 3)
    _L(g)


def _s_mountain_moon(g: Grid) -> None:
    _stars(g, 9, 0.4)
    _crescent(g, 25, 4)
    _mountains(g, 14, 15)
    _ground(g, 14, _P["mtnNear"])
    _L(g)


def _s_comet(g: Grid) -> None:
    _stars(g, H, 0.26)
    for k in range(10):
        c = _P["bright"] if k < 2 else (_P["star"] if k < 5 else _P["starD"])
        _px(g, 28 - k, 2 + k, c)
    _disc(g, 28, 2, 1.4, 1.4, _P["bright"])
    _L(g)


def _s_aurora(g: Grid) -> None:
    _stars(g, 6, 0.2)
    _aurora(g)
    _L(g)


def _s_starfield(g: Grid) -> None:
    _stars(g, H, 0.5)
    _spark(g, 24, 4, _P["bright"])
    _spark(g, 28, 9, _P["star"])
    _L(g)


# --- scene pool: (builder, verbatim gallery phrase, when-tag) in gallery 1->50 order ---

ANY = "ANY"
DAY = "DAY"
NIGHT = "NIGHT"

SCENES: list[tuple[SceneBuilder, str, str]] = [
    (_s_crescent_stars, "the familiar awakens.", ANY),
    (_s_crescent_perch, "perched and ready.", ANY),
    (_s_full_moon_cloud, "what are we building tonight?", NIGHT),
    (_s_meteor_shower, "make a wish.", ANY),
    (_s_constellation, "drawn in the stars.", ANY),
    (_s_ringed_planet, "a world, from nothing.", ANY),
    (_s_mountain_moon, "welcome back.", ANY),
    (_s_comet, "something wicked this way ships.", ANY),
    (_s_aurora, "the sky's showing off — let's match it.", ANY),
    (_s_starfield, "your imp is listening.", ANY),
]

MIN_WIDTH = 32


def _now_hour() -> int:
    return datetime.datetime.now().hour


def _period(hour: int) -> str:
    return "night" if (hour >= 20 or hour < 5) else "day"


def _eligible() -> list[int]:
    period = _period(_now_hour())
    out: list[int] = []
    for i, (_builder, _phrase, when) in enumerate(SCENES):
        if (when == ANY
                or (when == DAY and period == "day")
                or (when == NIGHT and period == "night")):
            out.append(i)
    return out


def choose_index() -> int:
    return random.choice(_eligible())


def scene_lines(index: int, *, theme: str, color: bool) -> list[str]:
    """Deterministically render scene `index` to half-block lines."""
    _use_theme(theme)
    g = _make_grid()
    SCENES[index][0](g)
    return _encode_grid(g, color=color)


def splash_block(*, theme: str, width: int, color: bool) -> str:
    """The opening splash: a random eligible scene over a grey-blue italic tagline."""
    index = choose_index()
    phrase = SCENES[index][1]
    if not color or width < MIN_WIDTH:
        return phrase
    lines = scene_lines(index, theme=theme, color=color)
    tag = _fg_open(_build_palette(theme)["tag"]) + _ITALIC + phrase + _RESET
    return "\n".join(lines) + "\n" + tag
