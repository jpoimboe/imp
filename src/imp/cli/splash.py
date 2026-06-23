"""The startup splash: a once-printed, randomly-chosen compact pixel-art scene."""
from __future__ import annotations

import datetime
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

# Dark is the anchor ember palette (spec hexes); light is re-tuned to stay legible
# on a near-white surface; ansi uses only the 16 named SGR colors.
_DARK: dict[str, Color | None] = {
    "impBody": (0xFF, 0x9A, 0x3C), "impHorn": (0xC2, 0x61, 0x1F),
    "impGlint": (0xFF, 0xE9, 0xC8), "impEye": (0x2A, 0x16, 0x06),
    "tag": (0x9A, 0xA0, 0xA8),
    "star": (0x78, 0x98, 0xBE), "starBright": (0x7F, 0xA2, 0xCA),
    "starDim": (0x55, 0x57, 0x53),
    "moon": (0xB8, 0xC2, 0xD6), "moonGlow": (0x39, 0x43, 0x5C),
    "cloud": (0x2C, 0x33, 0x46),
    "fire1": (0xB8, 0x6A, 0x1F), "fire2": (0xD2, 0x95, 0x4A), "fire3": (0xF0, 0xCF, 0x80),
    "fireTip": (0xFF, 0xF0, 0xC0), "glowWarm": (0x5A, 0x4A, 0x2A),
    "ground": (0x1A, 0x16, 0x22), "groundGlow": (0x24, 0x1C, 0x30),
    "sun": (0xFF, 0xB2, 0x4A), "sunGlow": (0x5A, 0x3A, 0x1A),
    "branch": (0x5A, 0x3A, 0x1F), "peak": (0x3A, 0x3F, 0x4E), "snow": (0xD8, 0xE0, 0xEE),
    "planet": (0xC0, 0x7A, 0x3A), "planetRing": (0x9A, 0xB0, 0xD0),
    "aurora1": (0x4A, 0xC0, 0x8A), "aurora2": (0x7A, 0x6A, 0xC0),
    "firefly": (0xF0, 0xE0, 0x80), "fireflyGlow": (0x4A, 0x4A, 0x20),
    "wisp": (0x9A, 0xE0, 0xD0), "wispGlow": (0x2A, 0x4A, 0x4A),
    "mushroomStem": (0xC8, 0xC0, 0xA8), "mushroomCap": (0x6A, 0xC0, 0x9A),
    "mushroomGlow": (0x2A, 0x4A, 0x3A),
    "sigil": (0xB0, 0x7A, 0xE0), "sigilDim": (0x5A, 0x3A, 0x7A),
    "portal": (0x7A, 0xC8, 0xE0), "portalCore": (0x18, 0x24, 0x42),
    "glass": (0x7A, 0xA8, 0xC8), "glassCore": (0xD8, 0xEC, 0xF6),
    "lanternFrame": (0x8A, 0x6A, 0x3A),
    "crystal": (0x8A, 0xC0, 0xE0), "crystalLight": (0xD0, 0xE8, 0xF4),
    "iron": (0x33, 0x30, 0x3A), "brew": (0x7A, 0xC8, 0x6A),
    "bookCover": (0x8A, 0x33, 0x44), "bookPage": (0xEC, 0xE2, 0xC8),
    "sand": (0xE0, 0xC0, 0x70), "ink": (0x2A, 0x36, 0x5A), "quill": (0xD8, 0xD0, 0xE0),
    "creature": (0x1A, 0x18, 0x22), "creatureEye": (0xF0, 0x9A, 0x3C),
    "pumpkin": (0xE0, 0x7A, 0x2A), "gold": (0xE0, 0xC0, 0x60),
}
_LIGHT: dict[str, Color | None] = {
    "impBody": (0xC2, 0x61, 0x1F), "impHorn": (0x7A, 0x3A, 0x0E),
    "impGlint": (0x8A, 0x5A, 0x20), "impEye": (0x2A, 0x16, 0x06),
    "tag": (0x5A, 0x66, 0x78),
    "star": (0x1E, 0x44, 0x7E), "starBright": (0x27, 0x5A, 0xA0),
    "starDim": (0xA6, 0xAA, 0xA6),
    "moon": (0x6A, 0x72, 0x90), "moonGlow": None,
    "cloud": (0x8A, 0x93, 0xA8),
    "fire1": (0xC2, 0x46, 0x1A), "fire2": (0xD2, 0x70, 0x1F), "fire3": (0x8A, 0x6D, 0x12),
    "fireTip": (0x5E, 0x4A, 0x0C), "glowWarm": (0xC2, 0xA0, 0x60),
    "ground": (0xB6, 0xB6, 0xB6), "groundGlow": (0xA8, 0xA8, 0xA8),
    "sun": (0xC2, 0x61, 0x1F), "sunGlow": (0xD8, 0xB8, 0x78),
    "branch": (0x6A, 0x4A, 0x2A), "peak": (0x8A, 0x90, 0x9E), "snow": (0xC8, 0xCE, 0xD8),
    "planet": (0x9A, 0x5A, 0x20), "planetRing": (0x6A, 0x80, 0xA0),
    "aurora1": (0x2A, 0x8A, 0x5A), "aurora2": (0x5A, 0x4A, 0x8A),
    "firefly": (0xB0, 0x90, 0x20), "fireflyGlow": (0xD0, 0xC8, 0x90),
    "wisp": (0x2A, 0x8A, 0x8A), "wispGlow": (0xB0, 0xD0, 0xD0),
    "mushroomStem": (0x8A, 0x80, 0x68), "mushroomCap": (0x2A, 0x8A, 0x5A),
    "mushroomGlow": (0xB0, 0xD0, 0xC0),
    "sigil": (0x6A, 0x3A, 0xB0), "sigilDim": (0x9A, 0x80, 0xC0),
    "portal": (0x2A, 0x7A, 0xA0), "portalCore": (0x2A, 0x40, 0x60),
    "glass": (0x5A, 0x80, 0xA0), "glassCore": (0x2A, 0x5A, 0x80),
    "lanternFrame": (0x6A, 0x4A, 0x20),
    "crystal": (0x4A, 0x80, 0xA0), "crystalLight": (0x2A, 0x5A, 0x80),
    "iron": (0x4A, 0x46, 0x52), "brew": (0x2A, 0x8A, 0x4A),
    "bookCover": (0x9A, 0x3A, 0x4A), "bookPage": (0xC8, 0xBE, 0xA0),
    "sand": (0xB0, 0x8A, 0x30), "ink": (0x2A, 0x36, 0x5A), "quill": (0x6A, 0x6A, 0x80),
    "creature": (0x2A, 0x26, 0x32), "creatureEye": (0xC2, 0x46, 0x1A),
    "pumpkin": (0xB0, 0x5A, 0x1A), "gold": (0xB0, 0x90, 0x30),
}
_ANSI_P: dict[str, Color | None] = {
    "impBody": "bright_yellow", "impHorn": "yellow",
    "impGlint": "bright_white", "impEye": "black",
    "tag": "bright_black",
    "star": "white", "starBright": "bright_white", "starDim": "bright_black",
    "moon": "bright_white", "moonGlow": None,
    "cloud": "bright_black",
    "fire1": "red", "fire2": "bright_red", "fire3": "bright_yellow",
    "fireTip": "bright_yellow", "glowWarm": "bright_black",
    "ground": "bright_black", "groundGlow": "bright_black",
    "sun": "bright_yellow", "sunGlow": "yellow",
    "branch": "yellow", "peak": "bright_black", "snow": "bright_white",
    "planet": "yellow", "planetRing": "cyan",
    "aurora1": "green", "aurora2": "magenta",
    "firefly": "bright_yellow", "fireflyGlow": "bright_black",
    "wisp": "bright_cyan", "wispGlow": "cyan",
    "mushroomStem": "white", "mushroomCap": "green", "mushroomGlow": "bright_black",
    "sigil": "magenta", "sigilDim": "bright_black",
    "portal": "cyan", "portalCore": "blue",
    "glass": "cyan", "glassCore": "bright_white",
    "lanternFrame": "yellow",
    "crystal": "bright_cyan", "crystalLight": "bright_white",
    "iron": "bright_black", "brew": "green",
    "bookCover": "red", "bookPage": "white",
    "sand": "yellow", "ink": "blue", "quill": "bright_white",
    "creature": "bright_black", "creatureEye": "bright_red",
    "pumpkin": "yellow", "gold": "bright_yellow",
}


def _build_palette(theme: str) -> dict[str, Color | None]:
    return {"dark": _DARK, "light": _LIGHT, "ansi": _ANSI_P}.get(theme, _ANSI_P)


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


# The active palette, set by _use_theme before any scene/primitive runs. Scene
# builders are (grid) -> None, so the palette is module state.
_P: dict[str, Color | None] = {}


def _use_theme(theme: str) -> None:
    global _P
    _P = _build_palette(theme)


def _make_grid() -> Grid:
    return [[None] * W for _ in range(H)]


def _round(v: float) -> int:
    """Round half toward +infinity (stable across the engine)."""
    import math
    return math.floor(v + 0.5)


def _imul(a: int, b: int) -> int:
    return ((a & 0xFFFFFFFF) * (b & 0xFFFFFFFF)) & 0xFFFFFFFF


def _hash(x: int, y: int) -> float:
    """Multiply-xor-shift hash returning a float in [0, 1); no RNG, so a chosen
    scene renders identically every time."""
    h = (_imul(int(x), 374761393) + _imul(int(y), 668265263)) & 0xFFFFFFFF
    h = _imul(h ^ (h >> 13), 1274126177)
    h ^= h >> 16
    return (h & 0xFFFFFFFF) / 4294967296.0


def _px(g: Grid, x: float, y: float, c: Color | None) -> None:
    xi = _round(x)
    yi = _round(y)
    if c is not None and 0 <= yi < len(g) and 0 <= xi < len(g[0]):
        g[yi][xi] = c


def _rect(g: Grid, x: int, y: int, w: int, h: int, c: Color | None) -> None:
    if c is None:
        return
    for j in range(h):
        for i in range(w):
            _px(g, x + i, y + j, c)


def _line(g: Grid, x0: float, y0: float, x1: float, y1: float, c: Color | None) -> None:
    if c is None:
        return
    x0i, y0i, x1i, y1i = _round(x0), _round(y0), _round(x1), _round(y1)
    dx = abs(x1i - x0i)
    dy = abs(y1i - y0i)
    sx = 1 if x0i < x1i else -1
    sy = 1 if y0i < y1i else -1
    e = dx - dy
    while True:
        _px(g, x0i, y0i, c)
        if x0i == x1i and y0i == y1i:
            break
        e2 = 2 * e
        if e2 > -dy:
            e -= dy
            x0i += sx
        if e2 < dx:
            e += dx
            y0i += sy


def _disc(g: Grid, cx: float, cy: float, rx: float, ry: float, c: Color | None) -> None:
    if c is None:
        return
    for y in range(len(g)):
        for x in range(len(g[0])):
            dx = (x - cx) / rx
            dy = (y - cy) / ry
            if dx * dx + dy * dy <= 1:
                g[y][x] = c


def _carve(g: Grid, cx: float, cy: float, rx: float, ry: float) -> None:
    for y in range(len(g)):
        for x in range(len(g[0])):
            dx = (x - cx) / rx
            dy = (y - cy) / ry
            if dx * dx + dy * dy <= 1:
                g[y][x] = None


def _ring(g: Grid, cx: float, cy: float, rx: float, ry: float,
          c: Color | None, inn: float, out: float) -> None:
    if c is None:
        return
    for y in range(len(g)):
        for x in range(len(g[0])):
            dx = (x - cx) / rx
            dy = (y - cy) / ry
            d = dx * dx + dy * dy
            if inn <= d <= out:
                _px(g, x, y, c)


def _glow_ring(g: Grid, cx: float, cy: float, r: float, c: Color | None) -> None:
    if c is None:
        return
    ring_expand = 2.0
    ring_inner = 0.62
    for y in range(len(g)):
        for x in range(len(g[0])):
            dx = (x - cx) / (r + ring_expand)
            dy = (y - cy) / (r + ring_expand)
            d = dx * dx + dy * dy
            if ring_inner <= d <= 1.0 and (x + y) & 1 and g[y][x] is None:
                _px(g, x, y, c)


def _spark(g: Grid, x: float, y: float, c: Color | None) -> None:
    xi = _round(x)
    yi = _round(y)
    _px(g, xi, yi, c)
    _px(g, xi - 1, yi, c)
    _px(g, xi + 1, yi, c)
    _px(g, xi, yi - 1, c)
    _px(g, xi, yi + 1, c)


def _stars(g: Grid, y0: int, y1: int, dens: float) -> None:
    """Hash-placed stars confined to the accent columns (x >= 19) so they never
    collide with the imp and a chosen scene renders identically every time."""
    s = 3
    bright_frac, dim_frac = 0.12, 0.55
    gy = 0
    while gy * s < y1:
        gx = 0
        while gx * s < W:
            if _hash(gx, gy) >= dens:
                gx += 1
                continue
            jx = gx * s + int(_hash(gx + 101, gy) * s)
            jy = gy * s + int(_hash(gx, gy + 57) * s)
            if jx < 19 or jy < y0 or jy >= y1:
                gx += 1
                continue
            br = _hash(gx + 7, gy + 13)
            color = (_P["starBright"] if br < bright_frac
                     else (_P["star"] if br < dim_frac else _P["starDim"]))
            _px(g, jx, jy, color)
            gx += 1
        gy += 1


def _ground(g: Grid, y: int, *, glow: bool = False) -> None:
    for x in range(W):
        _px(g, x, y, _P["ground"])
        if glow and x & 1:
            _px(g, x, y - 1, _P["groundGlow"])


# Locked mascot sprite (15 rows x 16 cols). Legend: o=body, O=horn/dark-edge,
# W=eye glint, b=eye, space=transparent. Top-left pixel lands at grid column 2.
IMP: tuple[str, ...] = (
    "   O        O   ", "  O          O  ", "  O          O  ", "   O        O   ",
    "    OO    OO    ",
    "    oooooooo    ", "   oooooooooo   ", "  oooWbooWbooo  ", "  ooobboobbooo  ",
    "  oooooooooooo  ", "  oooooooooooo  ", "   oooooooooo   ", "    oooooooo    ",
    "    oo    oo    ", "   oo      oo   ",
)


def _left_imp(g: Grid) -> None:
    m = {"o": _P["impBody"], "O": _P["impHorn"], "W": _P["impGlint"], "b": _P["impEye"]}
    for r, row in enumerate(IMP):
        for c, ch in enumerate(row):
            color = m.get(ch)
            if color is not None:
                _px(g, 2 + c, r, color)


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


# --- prop builders (one accent each), all in the accent band (x >= 18) ---

def _moon(g: Grid, cx: int, cy: int, r: int, *, crescent: bool) -> None:
    _glow_ring(g, cx, cy, r, _P["moonGlow"])
    _disc(g, cx, cy, r, r, _P["moon"])
    if crescent:
        _carve(g, cx + _round(r * 0.7), cy - 1, r, r)


def _cloud(g: Grid, cx: int, cy: int) -> None:
    _disc(g, cx, cy, 4, 1, _P["cloud"])
    _disc(g, cx - 2, cy + 1, 2, 1, _P["cloud"])
    _disc(g, cx + 2, cy + 1, 2, 1, _P["cloud"])


def _meteor(g: Grid, x: int, y: int, length: int) -> None:
    for k in range(length):
        t = k / length
        c = (_P["starBright"] if t < 0.34
             else (_P["star"] if t < 0.7 else _P["starDim"]))
        _px(g, x + k, y - k, c)
    _spark(g, x, y, _P["starBright"])


def _fire(g: Grid, x: int, base_y: int, h: int) -> None:
    for j in range(h):
        c = (_P["fire1"] if j < h // 3
             else (_P["fire2"] if j < 2 * h // 3 else _P["fire3"]))
        _px(g, x, base_y - j, c)
        if j < h - 2:
            _px(g, x - 1, base_y - j, _P["fire1"])
            _px(g, x + 1, base_y - j, _P["fire1"])
    _px(g, x, base_y - h, _P["fireTip"])
    _px(g, x - 2, base_y - h + 1, _P["glowWarm"])
    _px(g, x + 2, base_y - h + 1, _P["glowWarm"])


def _sun(g: Grid, cx: int, cy: int, r: int) -> None:
    _glow_ring(g, cx, cy, r, _P["sunGlow"])
    _disc(g, cx, cy, r, r, _P["sun"])


def _constellation(g: Grid) -> None:
    nodes = [(20, 2), (24, 4), (27, 3), (23, 8), (26, 10)]
    for i in range(len(nodes) - 1):
        x0, y0 = nodes[i]
        x1, y1 = nodes[i + 1]
        _line(g, x0, y0, x1, y1, _P["starDim"])
    for x, y in nodes:
        _spark(g, x, y, _P["starBright"])


def _comet(g: Grid, x: int, y: int, length: int) -> None:
    for k in range(length):
        t = k / length
        c = (_P["starBright"] if t < 0.3
             else (_P["star"] if t < 0.6 else _P["starDim"]))
        _px(g, x - k, y + k, c)
    _spark(g, x, y, _P["starBright"])


def _branch(g: Grid, x0: int, x1: int, y: int) -> None:
    _line(g, x0, y, x1, y, _P["branch"])
    _px(g, x1, y - 1, _P["branch"])


def _mountains(g: Grid, base_y: int) -> None:
    for cx, h in ((23, 6), (27, 7)):
        for j in range(h):
            for dx in range(-j, j + 1):
                _px(g, cx + dx, base_y - h + j + 1, _P["peak"])
        cap_y = base_y - h + 1
        _px(g, cx, cap_y, _P["snow"])
        _px(g, cx - 1, cap_y + 1, _P["snow"])
        _px(g, cx + 1, cap_y + 1, _P["snow"])


def _planet(g: Grid, cx: int, cy: int, r: int) -> None:
    _disc(g, cx, cy, r, r, _P["planet"])
    _ring(g, cx, cy, r + 2.5, (r + 2.5) * 0.42, _P["planetRing"], 0.80, 1.0)


def _galaxy(g: Grid, cx: int, cy: int) -> None:
    pts = [(0, 0), (2, 0), (-2, 0), (3, 1), (-3, -1), (4, 1), (-4, -1),
           (1, -1), (-1, 1), (5, 1), (-5, -1), (2, 2), (-2, -2)]
    for k, (dx, dy) in enumerate(pts):
        near = abs(dx) + abs(dy) < 3
        c = _P["starBright"] if near else (_P["star"] if k % 2 else _P["starDim"])
        _px(g, cx + dx, cy + dy, c)
    _spark(g, cx, cy, _P["starBright"])


def _aurora(g: Grid) -> None:
    for off, c in ((0, _P["aurora1"]), (1, _P["aurora2"])):
        for x in range(19, W):
            y = 3 + off + ((x // 2) % 2)
            _px(g, x, y, c)
            _px(g, x, y + 2, c)


def _firefly(g: Grid, x: int, y: int) -> None:
    _px(g, x, y, _P["firefly"])
    _px(g, x - 1, y, _P["fireflyGlow"])
    _px(g, x + 1, y, _P["fireflyGlow"])
    _px(g, x, y - 1, _P["fireflyGlow"])
    _px(g, x, y + 1, _P["fireflyGlow"])


def _wisp(g: Grid, cx: int, cy: int, r: int) -> None:
    _glow_ring(g, cx, cy, r, _P["wispGlow"])
    inner = max(1, r - 2)
    _disc(g, cx, cy, inner, inner, _P["wisp"])


def _candle(g: Grid, x: int, base_y: int, h: int) -> None:
    for j in range(h):
        _px(g, x, base_y - j, _P["glowWarm"])
    _px(g, x, base_y - h, _P["fire3"])
    _px(g, x, base_y - h - 1, _P["fireTip"])


def _mushroom(g: Grid, x: int, base_y: int, h: int) -> None:
    for j in range(h):
        _px(g, x, base_y - j, _P["mushroomStem"])
    cap_y = base_y - h
    for dx in (-2, -1, 0, 1, 2):
        _px(g, x + dx, cap_y, _P["mushroomCap"])
    for dx in (-1, 0, 1):
        _px(g, x + dx, cap_y - 1, _P["mushroomCap"])
    _px(g, x, cap_y + 1, _P["mushroomGlow"])


def _orb(g: Grid, cx: int, cy: int, r: int) -> None:
    _px(g, cx - 1, cy + r + 1, _P["branch"])
    _px(g, cx, cy + r + 1, _P["branch"])
    _px(g, cx + 1, cy + r + 1, _P["branch"])
    _disc(g, cx, cy, r, r, _P["glass"])
    _px(g, cx - 1, cy - 1, _P["glassCore"])
    _px(g, cx, cy - 1, _P["glassCore"])


def _lantern(g: Grid, x: int, top_y: int) -> None:
    _px(g, x, top_y, _P["lanternFrame"])
    _rect(g, x - 2, top_y + 1, 5, 1, _P["lanternFrame"])
    for j in range(1, 5):
        _px(g, x - 2, top_y + 1 + j, _P["lanternFrame"])
        _px(g, x + 2, top_y + 1 + j, _P["lanternFrame"])
    _rect(g, x - 2, top_y + 6, 5, 1, _P["lanternFrame"])
    _px(g, x, top_y + 4, _P["fire1"])
    _px(g, x, top_y + 3, _P["fire3"])
    _px(g, x, top_y + 2, _P["fireTip"])


def _crystal(g: Grid, x: int, base_y: int, h: int) -> None:
    for j in range(h):
        spread = (h - j) // 2
        for dx in range(-spread, spread + 1):
            _px(g, x + dx, base_y - j, _P["crystal"])
    _px(g, x, base_y - h, _P["crystalLight"])


def _summon_ring(g: Grid, cx: int, cy: int) -> None:
    _ring(g, cx, cy, 6, 2.4, _P["sigilDim"], 0.72, 1.0)
    for dx in (-5, -3, 0, 3, 5):
        _px(g, cx + dx, cy, _P["sigil"])
    _spark(g, cx, cy, _P["sigil"])


def _portal_gate(g: Grid, cx: int, cy: int) -> None:
    _disc(g, cx, cy, 3, 5, _P["portalCore"])
    _ring(g, cx, cy, 4, 6, _P["portal"], 0.62, 1.0)
    _glow_ring(g, cx, cy, 5, _P["portal"])


def _cauldron(g: Grid, cx: int, base_y: int) -> None:
    _disc(g, cx, base_y - 1, 4, 2, _P["iron"])
    _carve(g, cx, base_y - 3, 3, 1)
    _rect(g, cx - 4, base_y - 3, 9, 1, _P["iron"])
    _px(g, cx - 3, base_y + 1, _P["iron"])
    _px(g, cx + 3, base_y + 1, _P["iron"])
    _px(g, cx - 1, base_y - 4, _P["brew"])
    _px(g, cx + 2, base_y - 5, _P["brew"])
    _px(g, cx, base_y - 6, _P["brew"])


def _grimoire(g: Grid, cx: int, cy: int) -> None:
    _rect(g, cx - 5, cy, 5, 4, _P["bookPage"])
    _rect(g, cx + 1, cy, 5, 4, _P["bookPage"])
    for j in range(4):
        _px(g, cx, cy + j, _P["bookCover"])
        _px(g, cx - 5, cy + j, _P["bookCover"])
        _px(g, cx + 5, cy + j, _P["bookCover"])
    _px(g, cx, cy - 2, _P["brew"])


def _potion_shelf(g: Grid, shelf_y: int) -> None:
    _rect(g, 19, shelf_y, 11, 1, _P["iron"])
    for x, liquid in ((21, _P["brew"]), (24, _P["bookCover"]), (27, _P["glass"])):
        _px(g, x, shelf_y - 1, liquid)
        _px(g, x, shelf_y - 2, liquid)
        _px(g, x, shelf_y - 3, _P["glass"])


def _hourglass(g: Grid, cx: int, top_y: int) -> None:
    _rect(g, cx - 3, top_y, 7, 1, _P["branch"])
    _rect(g, cx - 3, top_y + 7, 7, 1, _P["branch"])
    walls = {1: 3, 2: 2, 3: 1, 4: 1, 5: 2, 6: 3}
    for j, d in walls.items():
        _px(g, cx - d, top_y + j, _P["glass"])
        _px(g, cx + d, top_y + j, _P["glass"])
    for dx in (-2, -1, 0, 1, 2):
        _px(g, cx + dx, top_y + 1, _P["sand"])
    _px(g, cx, top_y + 4, _P["sand"])
    for dx in (-2, -1, 0, 1, 2):
        _px(g, cx + dx, top_y + 6, _P["sand"])


def _scroll(g: Grid, cx: int, cy: int) -> None:
    _rect(g, cx - 4, cy, 9, 4, _P["bookPage"])
    for j in range(4):
        _px(g, cx - 5, cy + j, _P["branch"])
        _px(g, cx + 5, cy + j, _P["branch"])
    _line(g, cx - 3, cy + 1, cx + 2, cy + 1, _P["ink"])
    _line(g, cx - 3, cy + 2, cx + 1, cy + 2, _P["ink"])


def _quill_ink(g: Grid, base_x: int, base_y: int) -> None:
    _rect(g, base_x - 2, base_y, 5, 2, _P["ink"])
    _px(g, base_x, base_y - 1, _P["ink"])
    for k in range(5):
        _px(g, base_x + 1 + k, base_y - 1 - k, _P["quill"])
    _px(g, base_x + 6, base_y - 6, _P["quill"])
    _px(g, base_x + 4, base_y - 4, _P["bookPage"])


def _raven(g: Grid, cx: int, base_y: int) -> None:
    _disc(g, cx, base_y - 1, 3, 2, _P["creature"])
    _disc(g, cx + 2, base_y - 3, 1, 1, _P["creature"])
    _px(g, cx + 4, base_y - 3, _P["fire2"])
    _line(g, cx - 3, base_y - 1, cx - 5, base_y + 1, _P["creature"])
    _px(g, cx + 2, base_y - 3, _P["creatureEye"])


def _black_cat(g: Grid, cx: int, base_y: int) -> None:
    _disc(g, cx, base_y - 1, 3, 2, _P["creature"])
    _disc(g, cx - 2, base_y - 3, 2, 1, _P["creature"])
    _px(g, cx - 3, base_y - 4, _P["creature"])
    _px(g, cx - 1, base_y - 4, _P["creature"])
    _line(g, cx + 3, base_y, cx + 5, base_y - 2, _P["creature"])
    _px(g, cx + 5, base_y - 3, _P["creature"])
    _px(g, cx - 3, base_y - 3, _P["creatureEye"])
    _px(g, cx - 1, base_y - 3, _P["creatureEye"])


def _bat(g: Grid, cx: int, cy: int) -> None:
    _px(g, cx, cy, _P["creature"])
    _px(g, cx - 1, cy - 1, _P["creature"])
    _px(g, cx - 2, cy, _P["creature"])
    _px(g, cx + 1, cy - 1, _P["creature"])
    _px(g, cx + 2, cy, _P["creature"])


def _pumpkin(g: Grid, cx: int, cy: int) -> None:
    _disc(g, cx, cy, 4, 3, _P["pumpkin"])
    _px(g, cx, cy - 4, _P["branch"])
    _px(g, cx - 2, cy - 1, _P["impEye"])
    _px(g, cx + 2, cy - 1, _P["impEye"])
    _px(g, cx, cy, _P["impEye"])
    _line(g, cx - 2, cy + 1, cx + 2, cy + 1, _P["impEye"])
    _px(g, cx - 1, cy, _P["fire3"])
    _px(g, cx + 1, cy, _P["fire3"])


def _skeleton_key(g: Grid, cx: int, cy: int) -> None:
    _ring(g, cx, cy, 2, 2, _P["gold"], 0.40, 1.0)
    _line(g, cx + 2, cy, cx + 7, cy, _P["gold"])
    _px(g, cx + 6, cy + 1, _P["gold"])
    _px(g, cx + 7, cy + 1, _P["gold"])


def _top_hat(g: Grid, cx: int, base_y: int) -> None:
    _rect(g, cx - 5, base_y, 11, 1, _P["creature"])
    _rect(g, cx - 3, base_y - 5, 7, 5, _P["creature"])
    _rect(g, cx - 3, base_y - 1, 7, 1, _P["bookCover"])


def _wand(g: Grid, base_x: int, base_y: int) -> None:
    _line(g, base_x, base_y, base_x + 5, base_y - 5, _P["branch"])
    tip_x, tip_y = base_x + 6, base_y - 6
    _spark(g, tip_x, tip_y, _P["gold"])
    _px(g, tip_x, tip_y, _P["glassCore"])
    _px(g, base_x + 2, base_y - 4, _P["gold"])
    _px(g, base_x - 1, base_y - 2, _P["gold"])


def _tarot(g: Grid, cx: int, base_y: int) -> None:
    for dx, lift in ((-4, 0), (0, 1), (4, 0)):
        x = cx + dx
        top = base_y - 5 - lift
        _rect(g, x - 1, top, 3, 6, _P["bookPage"])
        for j in range(6):
            _px(g, x - 1, top + j, _P["bookCover"])
            _px(g, x + 1, top + j, _P["bookCover"])
    _px(g, cx, base_y - 3, _P["sigil"])
    _spark(g, cx, base_y - 3, _P["sigil"])


def _web(g: Grid, cx: int, cy: int) -> None:
    for ex, ey in ((cx - 10, cy + 3), (cx - 8, cy + 9), (cx - 3, cy + 10), (cx, cy + 10)):
        _line(g, cx, cy, ex, ey, _P["starDim"])
    _ring(g, cx, cy, 9, 9, _P["starDim"], 0.18, 0.28)
    _ring(g, cx, cy, 9, 9, _P["starDim"], 0.62, 0.74)


def _spider(g: Grid, x: int, y: int) -> None:
    _line(g, x, 0, x, y - 2, _P["starDim"])
    _disc(g, x, y, 2, 1, _P["creature"])
    for dx in (-3, -2, 2, 3):
        _px(g, x + dx, y - 1, _P["creature"])
        _px(g, x + dx, y + 1, _P["creature"])
    _px(g, x - 1, y, _P["creatureEye"])
    _px(g, x + 1, y, _P["creatureEye"])


def _ouroboros(g: Grid, cx: int, cy: int, r: int) -> None:
    _ring(g, cx, cy, r, r, _P["brew"], 0.55, 1.0)
    _disc(g, cx + r, cy, 1, 1, _P["brew"])
    _px(g, cx + r, cy - 1, _P["gold"])


def _bug(g: Grid, cx: int, cy: int) -> None:
    _disc(g, cx, cy, 3, 2, _P["bookCover"])
    _line(g, cx, cy - 2, cx, cy + 2, _P["creature"])
    _disc(g, cx, cy - 3, 1, 1, _P["creature"])
    _px(g, cx, cy - 3, _P["creatureEye"])
    _px(g, cx - 1, cy, _P["impEye"])
    _px(g, cx + 1, cy + 1, _P["impEye"])
    for dx in (-3, 3):
        _px(g, cx + dx, cy, _P["creature"])
        _px(g, cx + dx, cy + 1, _P["creature"])
    _px(g, cx - 1, cy - 4, _P["creature"])
    _px(g, cx + 1, cy - 4, _P["creature"])


def _coffee(g: Grid, cx: int, base_y: int) -> None:
    _rect(g, cx - 2, base_y - 3, 5, 4, _P["bookPage"])
    _px(g, cx + 3, base_y - 2, _P["bookPage"])
    _px(g, cx + 4, base_y - 1, _P["bookPage"])
    _px(g, cx + 3, base_y, _P["bookPage"])
    _rect(g, cx - 1, base_y - 3, 3, 1, _P["branch"])
    _px(g, cx - 1, base_y - 5, _P["cloud"])
    _px(g, cx, base_y - 6, _P["cloud"])
    _px(g, cx + 1, base_y - 5, _P["cloud"])


def _terminal(g: Grid, cx: int, top_y: int) -> None:
    _rect(g, cx - 5, top_y, 11, 7, _P["iron"])
    _rect(g, cx - 4, top_y + 1, 9, 4, _P["portalCore"])
    _px(g, cx - 3, top_y + 2, _P["brew"])
    _rect(g, cx - 2, top_y + 2, 3, 1, _P["brew"])
    _px(g, cx + 3, top_y + 3, _P["fireTip"])
    _px(g, cx, top_y + 7, _P["iron"])
    _rect(g, cx - 2, top_y + 8, 5, 1, _P["iron"])


def _gear(g: Grid, cx: int, cy: int, r: int) -> None:
    _ring(g, cx, cy, r, r, _P["gold"], 0.45, 1.0)
    for dx, dy in ((0, -r - 1), (0, r + 1), (-r - 1, 0), (r + 1, 0)):
        _px(g, cx + dx, cy + dy, _P["gold"])
    _px(g, cx, cy, _P["iron"])


def _die(g: Grid, x: int, y: int, pips: list[tuple[int, int]], pip_color: Color | None) -> None:
    _rect(g, x, y, 4, 4, _P["bookPage"])
    for px_, py_ in pips:
        _px(g, x + px_, y + py_, pip_color)


# --- time-of-day tagline ---

def _now_hour() -> int:
    return datetime.datetime.now().hour


def _tonight_or_today() -> str:
    h = _now_hour()
    word = "tonight" if (h >= 18 or h < 5) else "today"
    return f"what are we building {word}?"


# --- scene builders (imp drawn last so it sits above any full-width ground) ---

def _crescent_stars(g: Grid) -> None:
    _stars(g, 0, 9, 0.30)
    _moon(g, 24, 4, 3, crescent=True)
    _left_imp(g)


def _full_moon_cloud(g: Grid) -> None:
    _stars(g, 0, 7, 0.28)
    _moon(g, 23, 4, 4, crescent=False)
    _cloud(g, 24, 8)
    _left_imp(g)


def _meteor_shower(g: Grid) -> None:
    _stars(g, 0, 12, 0.18)
    _meteor(g, 20, 4, 4)
    _meteor(g, 25, 8, 5)
    _left_imp(g)


def _starfield(g: Grid) -> None:
    _stars(g, 0, 14, 0.45)
    _left_imp(g)


def _campfire(g: Grid) -> None:
    _stars(g, 0, 6, 0.22)
    _ground(g, 13, glow=True)
    _fire(g, 24, 12, 5)
    _left_imp(g)


def _sunrise(g: Grid) -> None:
    _ground(g, 13, glow=True)
    _sun(g, 24, 12, 4)
    _left_imp(g)


def _constellation_scene(g: Grid) -> None:
    _stars(g, 0, 13, 0.20)
    _constellation(g)
    _left_imp(g)


def _comet_scene(g: Grid) -> None:
    _stars(g, 0, 12, 0.15)
    _comet(g, 28, 2, 6)
    _left_imp(g)


def _the_void(g: Grid) -> None:
    _stars(g, 0, 14, 0.10)
    _glow_ring(g, 24, 7, 5, _P["starDim"])
    _spark(g, 24, 7, _P["star"])
    _left_imp(g)


def _crescent_perch(g: Grid) -> None:
    _stars(g, 0, 6, 0.18)
    _moon(g, 25, 4, 3, crescent=True)
    _branch(g, 14, 28, 13)
    _left_imp(g)


def _mountain_moon(g: Grid) -> None:
    _stars(g, 0, 5, 0.16)
    _moon(g, 24, 3, 3, crescent=False)
    _mountains(g, 13)
    _left_imp(g)


def _ringed_planet(g: Grid) -> None:
    _stars(g, 0, 14, 0.12)
    _planet(g, 24, 7, 4)
    _left_imp(g)


def _galaxy_scene(g: Grid) -> None:
    _stars(g, 0, 14, 0.10)
    _galaxy(g, 25, 7)
    _left_imp(g)


def _aurora_scene(g: Grid) -> None:
    _stars(g, 0, 12, 0.12)
    _aurora(g)
    _left_imp(g)


def _fireflies(g: Grid) -> None:
    for x, y in [(21, 4), (26, 6), (23, 9), (28, 3), (20, 11)]:
        _firefly(g, x, y)
    _left_imp(g)


def _will_o_wisp(g: Grid) -> None:
    _stars(g, 0, 6, 0.10)
    _wisp(g, 25, 7, 4)
    _spark(g, 21, 4, _P["wisp"])
    _left_imp(g)


def _floating_wisps(g: Grid) -> None:
    for x, y in [(22, 3), (27, 6), (24, 10)]:
        _wisp(g, x, y, 2)
    _left_imp(g)


def _twin_candles(g: Grid) -> None:
    _ground(g, 13, glow=True)
    _candle(g, 22, 12, 3)
    _candle(g, 26, 12, 3)
    _left_imp(g)


def _twin_torches(g: Grid) -> None:
    _ground(g, 13, glow=True)
    _fire(g, 21, 12, 4)
    _fire(g, 27, 12, 4)
    _left_imp(g)


def _glowing_mushrooms(g: Grid) -> None:
    _ground(g, 13, glow=True)
    _mushroom(g, 22, 12, 2)
    _mushroom(g, 27, 12, 3)
    _left_imp(g)


def _summoning_circle(g: Grid) -> None:
    _ground(g, 13, glow=True)
    _summon_ring(g, 24, 11)
    _left_imp(g)


def _open_portal(g: Grid) -> None:
    _portal_gate(g, 25, 7)
    _left_imp(g)


def _rune_sigil(g: Grid) -> None:
    _stars(g, 0, 6, 0.10)
    _ring(g, 24, 6, 4, 3, _P["sigilDim"], 0.78, 1.0)
    for x, y in ((20, 5), (24, 2), (28, 5), (22, 9), (26, 9)):
        _px(g, x, y, _P["sigil"])
        _px(g, x, y - 1, _P["sigilDim"])
    _spark(g, 24, 6, _P["sigil"])
    _left_imp(g)


def _crystal_ball_scene(g: Grid) -> None:
    _stars(g, 0, 5, 0.10)
    _orb(g, 24, 7, 3)
    _left_imp(g)


def _lantern_scene(g: Grid) -> None:
    _stars(g, 0, 4, 0.10)
    _lantern(g, 24, 4)
    _left_imp(g)


def _crystal_cluster(g: Grid) -> None:
    _ground(g, 13, glow=True)
    _crystal(g, 22, 12, 4)
    _crystal(g, 25, 12, 6)
    _crystal(g, 28, 12, 3)
    _left_imp(g)


def _cauldron_scene(g: Grid) -> None:
    _ground(g, 13, glow=True)
    _cauldron(g, 24, 11)
    _left_imp(g)


def _grimoire_scene(g: Grid) -> None:
    _stars(g, 0, 4, 0.08)
    _grimoire(g, 24, 8)
    _left_imp(g)


def _potion_shelf_scene(g: Grid) -> None:
    _ground(g, 13, glow=True)
    _potion_shelf(g, 11)
    _left_imp(g)


def _hourglass_scene(g: Grid) -> None:
    _stars(g, 0, 3, 0.08)
    _hourglass(g, 24, 3)
    _left_imp(g)


def _scroll_scene(g: Grid) -> None:
    _stars(g, 0, 4, 0.08)
    _scroll(g, 24, 7)
    _left_imp(g)


def _quill_scene(g: Grid) -> None:
    _ground(g, 13, glow=True)
    _quill_ink(g, 22, 11)
    _left_imp(g)


def _raven_scene(g: Grid) -> None:
    _stars(g, 0, 5, 0.10)
    _moon(g, 27, 3, 2, crescent=False)
    _branch(g, 18, 29, 12)
    _raven(g, 23, 11)
    _left_imp(g)


def _black_cat_scene(g: Grid) -> None:
    _ground(g, 13, glow=True)
    _moon(g, 27, 3, 2, crescent=True)
    _black_cat(g, 23, 12)
    _left_imp(g)


def _bats_scene(g: Grid) -> None:
    _stars(g, 0, 6, 0.12)
    _moon(g, 25, 4, 3, crescent=False)
    for x, y in ((20, 3), (24, 5), (27, 8), (22, 9)):
        _bat(g, x, y)
    _left_imp(g)


def _jack_o_lantern_scene(g: Grid) -> None:
    _ground(g, 13, glow=True)
    _pumpkin(g, 24, 9)
    _left_imp(g)


def _skeleton_key_scene(g: Grid) -> None:
    _stars(g, 0, 5, 0.08)
    _skeleton_key(g, 21, 7)
    _left_imp(g)


def _top_hat_scene(g: Grid) -> None:
    _ground(g, 13, glow=True)
    _top_hat(g, 24, 12)
    _left_imp(g)


def _wand_scene(g: Grid) -> None:
    _stars(g, 0, 6, 0.08)
    _wand(g, 20, 12)
    _left_imp(g)


def _tarot_scene(g: Grid) -> None:
    _ground(g, 13, glow=True)
    _tarot(g, 24, 12)
    _left_imp(g)


def _fairy_ring(g: Grid) -> None:
    _ground(g, 13, glow=True)
    _ring(g, 24, 12, 6, 2, _P["mushroomGlow"], 0.6, 1.0)
    for x in (20, 24, 28):
        _mushroom(g, x, 12, 2)
    _left_imp(g)


def _spider_web_scene(g: Grid) -> None:
    _web(g, 29, 1)
    _spider(g, 24, 7)
    _left_imp(g)


def _ouroboros_scene(g: Grid) -> None:
    _stars(g, 0, 6, 0.08)
    _ouroboros(g, 24, 7, 4)
    _left_imp(g)


def _bug_scene(g: Grid) -> None:
    _ground(g, 13, glow=True)
    _bug(g, 24, 9)
    _left_imp(g)


def _coffee_scene(g: Grid) -> None:
    _ground(g, 13, glow=True)
    _coffee(g, 23, 12)
    _left_imp(g)


def _terminal_scene(g: Grid) -> None:
    _terminal(g, 24, 3)
    _left_imp(g)


def _desk_book(g: Grid) -> None:
    _rect(g, 18, 12, 12, 1, _P["branch"])
    _rect(g, 20, 10, 3, 2, _P["bookPage"])
    _rect(g, 24, 10, 3, 2, _P["bookPage"])
    _px(g, 23, 10, _P["bookCover"])
    _px(g, 23, 11, _P["bookCover"])
    _candle(g, 28, 11, 2)
    _left_imp(g)


def _clockwork(g: Grid) -> None:
    _stars(g, 0, 5, 0.06)
    _gear(g, 23, 6, 3)
    _gear(g, 28, 10, 2)
    _left_imp(g)


def _stacked_tomes(g: Grid) -> None:
    _ground(g, 13, glow=True)
    for x, y, w, cover in (
        (19, 12, 9, _P["bookCover"]),
        (20, 11, 8, _P["sigil"]),
        (19, 10, 9, _P["brew"]),
        (21, 9, 6, _P["iron"]),
    ):
        _rect(g, x, y, w, 1, cover)
        _px(g, x + w - 1, y, _P["bookPage"])
    _left_imp(g)


def _loaded_dice(g: Grid) -> None:
    _ground(g, 13, glow=True)
    _die(g, 19, 9, [(1, 1), (2, 2)], _P["impEye"])
    _die(g, 24, 9, [(0, 0), (3, 0), (0, 3), (3, 3), (1, 1), (2, 2)], _P["creatureEye"])
    _left_imp(g)


SCENES: list[tuple[SceneBuilder, str | Callable[[], str]]] = [
    (_crescent_stars, "the familiar awakens."),
    (_full_moon_cloud, _tonight_or_today),
    (_meteor_shower, "make a wish."),
    (_starfield, "your imp is listening."),
    (_campfire, "by emberlight, we code."),
    (_sunrise, "what are we building today?"),
    (_constellation_scene, "drawn in the stars."),
    (_comet_scene, "something wicked this way ships."),
    (_the_void, "conjured from the void."),
    (_crescent_perch, "perched and ready."),
    (_mountain_moon, "welcome back."),
    (_ringed_planet, "a world, from nothing."),
    (_galaxy_scene, "lost in the stack? i know the way."),
    (_aurora_scene, "the sky's showing off — let's match it."),
    (_fireflies, "the lights are out to play."),
    (_will_o_wisp, "follow the light."),
    (_floating_wisps, "the void is listening."),
    (_twin_candles, "whisper your wish."),
    (_twin_torches, "let's raise a little hell."),
    (_glowing_mushrooms, "let's make some mischief."),
    (_summoning_circle, "the circle is drawn."),
    (_open_portal, "the portal is open. step through."),
    (_rune_sigil, "the runes are ready."),
    (_crystal_ball_scene, "gaze in — what shall we build?"),
    (_lantern_scene, "the lantern's lit. lead the way."),
    (_crystal_cluster, "small imp, big leverage."),
    (_cauldron_scene, "stir the cauldron — what's brewing?"),
    (_grimoire_scene, "the grimoire is open."),
    (_potion_shelf_scene, "every spell has a price — name yours."),
    (_hourglass_scene, "time's a construct. ship anyway."),
    (_scroll_scene, "the ink is wet. dictate."),
    (_quill_scene, "name your desire."),
    (_raven_scene, "tell me what haunts your codebase."),
    (_black_cat_scene, "back so soon? good."),
    (_bats_scene, "creatures of the night, assemble."),
    (_jack_o_lantern_scene, "trouble? my specialty."),
    (_skeleton_key_scene, "i know a shortcut — only slightly cursed."),
    (_top_hat_scene, "now you see it. now you ship it."),
    (_wand_scene, "speak, and it shall be built."),
    (_tarot_scene, "pick a card. any card."),
    (_fairy_ring, "step inside the ring."),
    (_spider_web_scene, "caught in a bug? i'll cut it loose."),
    (_ouroboros_scene, "round and round the refactor goes."),
    (_bug_scene, "show me the bug. i'll deal with it."),
    (_coffee_scene, "another night, another diff."),
    (_terminal_scene, "mischief, compiled."),
    (_desk_book, "light the candles. we begin."),
    (_clockwork, "let's build the machine."),
    (_stacked_tomes, "i've read everything. ask."),
    (_loaded_dice, "the dice are loaded in your favor."),
]

MIN_WIDTH = 32


def choose_index() -> int:
    return random.randrange(len(SCENES))


def _resolve_tagline(tagline: str | Callable[[], str]) -> str:
    return tagline() if callable(tagline) else tagline


def scene_lines(index: int, *, theme: str, color: bool) -> list[str]:
    """Deterministically render scene `index` to half-block lines."""
    _use_theme(theme)
    g = _make_grid()
    SCENES[index][0](g)
    return _encode_grid(g, color=color)


def splash_block(*, theme: str, width: int, color: bool) -> str:
    """The opening splash: a random scene over a grey-blue italic tagline."""
    index = choose_index()
    tagline = _resolve_tagline(SCENES[index][1])
    if not color or width < MIN_WIDTH:
        return tagline
    lines = scene_lines(index, theme=theme, color=color)
    tag = _fg_open(_build_palette(theme)["tag"]) + _ITALIC + tagline + _RESET
    return "\n".join(lines) + "\n" + tag
