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


SCENES: list[tuple[SceneBuilder, str | Callable[[], str]]] = [
    (_crescent_stars, "the familiar awakens."),
    (_full_moon_cloud, _tonight_or_today),
    (_meteor_shower, "make a wish."),
    (_starfield, "your imp is listening."),
    (_campfire, "by emberlight, we code."),
    (_sunrise, "what are we building today?"),
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
