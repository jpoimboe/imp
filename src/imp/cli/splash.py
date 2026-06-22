"""The startup splash: a once-printed, randomly-chosen pixel-art scene.

Ported from the canonical reference renderer
docs/superpowers/specs/mockups/2026-06-22-imp-startup-splash.html. Pixel art is
half-blocks: 1 art-pixel is 1 char column wide x 1/2 row tall; the H=28 canvas
encodes to 14 terminal rows. Geometry is deterministic (hash-based, never
`random`); only the scene index is random per launch. Anchor roles (imp body,
portal, runes, stars) come from palette.py so the splash stays in sync with the
rest of the UI; the many derived shades are splash-local constants ported
verbatim from the reference THEMES table.
"""
from __future__ import annotations

import math
import random
from collections.abc import Callable
from typing import Protocol

from imp.cli import palette as pal

Color = tuple[int, int, int] | str
Grid = list[list["Color | None"]]
SceneBuilder = Callable[[Grid, int], None]

_RESET = "\x1b[0m"

# name -> (fg SGR code, bg SGR code) for the 16 named ANSI colors.
_NAMED: dict[str, tuple[int, int]] = {
    "black": (30, 40), "red": (31, 41), "green": (32, 42), "yellow": (33, 43),
    "blue": (34, 44), "magenta": (35, 45), "cyan": (36, 46), "white": (37, 47),
    "bright_black": (90, 100), "bright_red": (91, 101), "bright_green": (92, 102),
    "bright_yellow": (93, 103), "bright_blue": (94, 104),
    "bright_magenta": (95, 105), "bright_cyan": (96, 106), "bright_white": (97, 107),
}

# Derived shades ported verbatim from the reference THEMES table (truecolor). The
# seven anchor roles below are overridden from palette.py in _build_palette; they
# are listed here too so the table reads 1:1 against the reference. `None` = null.
_RAW: dict[str, dict[str, str | None]] = {
    "dark": {
        "text": "#8fa0c8",
        "impO": "#c9a554", "impH": "#8f7028", "impW": "#fff4e8", "impB": "#2a2410",
        "star": "#7898be", "starD": "#555753", "bright": "#7fa2ca",
        "moon": "#b8c2d6", "moonRing": None, "moonGlow": "#39435c",
        "portal": "#a884b5", "portalG": "#6a5475", "portalI": "#d8b8e0",
        "portalSwirl": "#6a4a8a", "rune": "#5ea09e",
        "mtnFar": "#222838", "mtnMid": "#2c3346", "mtnNear": "#161b27", "snow": "#9fb0c8",
        "fire1": "#b86a1f", "fire2": "#d2954a", "fire3": "#f0cf80",
        "fireTip": "#fff0c0", "ember": "#d2954a", "glowWarm": "#5a4a2a",
        "waxA": "#cdc6d2", "waxB": "#8f8a98",
        "pot": "#34304a", "potD": "#211e30", "rim": "#4a4564",
        "brew": "#7bbf86", "brewB": "#b6e6bd", "steam": "#5a6072",
        "crysLit": "#6fa0b8", "crysShade": "#3a5a70", "crysTip": "#cfeaf2", "crysBase": "#2a4250",
        "mushStem": "#cdc6d2", "mushSpot": "#efe6f2", "cap1": "#a884b5", "cap1D": "#7a5a90",
        "cap2": "#9a6ad0", "cap2D": "#6a4a90", "cap3": "#a884b5",
        "ground": "#1a1622", "groundGlow": "#241c30", "mushGlow": "#3a2f45",
        "planetA": "#b07a4a", "planetB": "#d2a070", "planetC": "#7a5230", "ring": "#c2cad8",
        "torchPole": "#5a5668", "torchBr": "#46425a",
        "cloud": "#2c3346",
        "glass": "#2c2840", "mist": "#7a5e9a", "orbHi": "#fff4e8", "orbHi2": "#e6def0",
        "orbRim": "#6a6488", "orbStand": "#4a4658", "orbStand2": "#34304a",
        "lanMetal": "#6a6678", "lanMetal2": "#46425a", "lanCore": "#f0dca0",
        "lanGlow": "#c9a554", "lanHalo": "#5a4a2a",
        "bookPage": "#aeb6c8", "bookPageD": "#717a90", "bookSpine": "#3a3550", "bookInk": "#5b6075",
        "boltCore": "#eaf2ff", "boltGlow": "#7fa2ca", "storm": "#4a4660", "stormD": "#34304a",
        "ffWarmC": "#f0cf80", "ffWarmH": "#5a4a2a", "ffCoolC": "#8fe0c8", "ffCoolH": "#234a44",
        "wm": "#c9a554",
    },
    "light": {
        "text": "#355580",
        "impO": "#8a6d12", "impH": "#5e4a0c", "impW": "#fffdf3", "impB": "#2a2410",
        "star": "#1e447e", "starD": "#a6aaa6", "bright": "#275aa0",
        "moon": "#f1f1ec", "moonRing": "#6a7290", "moonGlow": None,
        "portal": "#7a4996", "portalG": "#a888bb", "portalI": "#c9a6d8",
        "portalSwirl": "#9a78b0", "rune": "#2a7370",
        "mtnFar": "#8a93a8", "mtnMid": "#6a7290", "mtnNear": "#4a5570", "snow": "#d6dae2",
        "fire1": "#c2461a", "fire2": "#d2701f", "fire3": "#b8901c",
        "fireTip": "#8a6d12", "ember": "#c2461a", "glowWarm": "#c2a060",
        "waxA": "#9a94a6", "waxB": "#7a7484",
        "pot": "#5a5568", "potD": "#3a3548", "rim": "#6a6578",
        "brew": "#3f8a52", "brewB": "#2e6a3c", "steam": "#8a90a0",
        "crysLit": "#3a6a80", "crysShade": "#27506a", "crysTip": "#2a7370", "crysBase": "#8a93a8",
        "mushStem": "#8a8494", "mushSpot": "#6a6478", "cap1": "#7a4996", "cap1D": "#5a3470",
        "cap2": "#8a4aa0", "cap2D": "#5a3470", "cap3": "#7a4996",
        "ground": "#c4c4c4", "groundGlow": "#b6b6b6", "mushGlow": "#b0a8b8",
        "planetA": "#8a5a2a", "planetB": "#a8743a", "planetC": "#5e3a18", "ring": "#6a7290",
        "torchPole": "#6a6578", "torchBr": "#4a4558",
        "cloud": "#8a93a8",
        "glass": "#9aa0ae", "mist": "#7a4996", "orbHi": "#fffdf3", "orbHi2": "#c8c2d2",
        "orbRim": "#5a5568", "orbStand": "#6a6578", "orbStand2": "#4a4558",
        "lanMetal": "#6a6578", "lanMetal2": "#4a4558", "lanCore": "#b8901c",
        "lanGlow": "#8a6d12", "lanHalo": "#c2a060",
        "bookPage": "#b8b2c0", "bookPageD": "#8a8496", "bookSpine": "#4a4558", "bookInk": "#6a6478",
        "boltCore": "#275aa0", "boltGlow": "#1e447e", "storm": "#8a93a8", "stormD": "#6a7290",
        "ffWarmC": "#b8901c", "ffWarmH": "#c2a060", "ffCoolC": "#2a7370", "ffCoolH": "#8aa0a0",
        "wm": "#8a6d12",
    },
}

# The ansi theme follows the design's named-SGR table directly (not the role
# mapping): only the 16 named colors, tuned for the common dark terminal.
_ANSI_TOKENS: dict[str, Color | None] = {
    "text": "white",
    "impO": "bright_yellow", "impH": "yellow", "impW": "bright_white", "impB": "black",
    "star": "white", "starD": "bright_black", "bright": "bright_white",
    "moon": "bright_white", "moonRing": None, "moonGlow": None,
    "portal": "bright_magenta", "portalG": "magenta", "portalI": "bright_magenta",
    "portalSwirl": "magenta", "rune": "bright_cyan",
    "mtnFar": "bright_black", "mtnMid": "bright_black", "mtnNear": "bright_black", "snow": "bright_white",
    "fire1": "red", "fire2": "bright_red", "fire3": "bright_yellow",
    "fireTip": "bright_yellow", "ember": "bright_red", "glowWarm": "bright_black",
    "waxA": "white", "waxB": "bright_black",
    "pot": "bright_black", "potD": "black", "rim": "bright_black",
    "brew": "green", "brewB": "bright_green", "steam": "bright_black",
    "crysLit": "cyan", "crysShade": "blue", "crysTip": "bright_cyan", "crysBase": "bright_black",
    "mushStem": "white", "mushSpot": "bright_white", "cap1": "bright_magenta", "cap1D": "magenta",
    "cap2": "bright_magenta", "cap2D": "magenta", "cap3": "bright_magenta",
    "ground": "bright_black", "groundGlow": "bright_black", "mushGlow": "bright_black",
    "planetA": "yellow", "planetB": "bright_yellow", "planetC": "red", "ring": "white",
    "torchPole": "bright_black", "torchBr": "bright_black",
    "cloud": "bright_black",
    "glass": "black", "mist": "magenta", "orbHi": "bright_white", "orbHi2": "white",
    "orbRim": "bright_black", "orbStand": "bright_black", "orbStand2": "black",
    "lanMetal": "bright_black", "lanMetal2": "bright_black", "lanCore": "bright_yellow",
    "lanGlow": "yellow", "lanHalo": "bright_black",
    "bookPage": "white", "bookPageD": "bright_black", "bookSpine": "black", "bookInk": "bright_black",
    "boltCore": "bright_white", "boltGlow": "bright_blue", "storm": "bright_black", "stormD": "black",
    "ffWarmC": "bright_yellow", "ffWarmH": "bright_black", "ffCoolC": "bright_cyan", "ffCoolH": "bright_black",
    "wm": "bright_yellow",
}


def _hex_to_rgb(h: str) -> tuple[int, int, int]:
    h = h.lstrip("#")
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))


def _build_palette(theme: str) -> dict[str, Color | None]:
    """The role -> color-token map for `theme`. ansi yields named tokens; dark/
    light yield (r,g,b) tuples, with the six anchor roles + wordmark pulled from
    palette.py so the splash stays in sync with the rest of the UI."""
    if theme == "ansi":
        return dict(_ANSI_TOKENS)
    raw = _RAW.get(theme, _RAW["dark"])
    out: dict[str, Color | None] = {
        k: (_hex_to_rgb(v) if v is not None else None) for k, v in raw.items()
    }
    rgb = pal.theme_rgb(theme)
    out["impO"] = rgb["yellow"]
    out["portal"] = rgb["magenta"]
    out["rune"] = rgb["cyan"]
    out["star"] = rgb["blue"]
    out["starD"] = rgb["gray"]
    out["bright"] = rgb["bright_blue"]
    out["wm"] = rgb["yellow"]
    return out


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


H = 28
GL = 0.382

# The current palette, set by _use_theme before any scene/primitive runs. The
# scene builders are (grid, W) -> None per spec, so the palette is module state,
# mirroring the reference renderer's global `P`.
_P: dict[str, Color | None] = {}


def _use_theme(theme: str) -> None:
    global _P
    _P = _build_palette(theme)


def _make_grid(w: int) -> Grid:
    return [[None] * w for _ in range(H)]


def _round(v: float) -> int:
    """JS Math.round: round half toward +infinity."""
    return math.floor(v + 0.5)


def _imul(a: int, b: int) -> int:
    """JS Math.imul: unsigned 32-bit low word."""
    return ((a & 0xFFFFFFFF) * (b & 0xFFFFFFFF)) & 0xFFFFFFFF


def _hash(x: int, y: int) -> float:
    """Reference multiply-xor-shift hash, returns a float in [0, 1)."""
    h = (_imul(int(x), 374761393) + _imul(int(y), 668265263)) & 0xFFFFFFFF
    h = _imul(h ^ (h >> 13), 1274126177)
    h ^= h >> 16
    return (h & 0xFFFFFFFF) / 4294967296.0


def _px(g: Grid, x: float, y: float, c: Color | None) -> None:
    xi = _round(x)
    yi = _round(y)
    if c is not None and 0 <= yi < len(g) and 0 <= xi < len(g[0]):
        g[yi][xi] = c


def _discE(g: Grid, cx: float, cy: float, rx: float, ry: float, c: Color | None) -> None:
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


def _ringE(g: Grid, cx: float, cy: float, rx: float, ry: float,
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


def _glowFill(g: Grid, cx: float, cy: float, rx: float, ry: float, c: Color | None) -> None:
    if c is None:
        return
    for y in range(len(g)):
        for x in range(len(g[0])):
            dx = (x - cx) / rx
            dy = (y - cy) / ry
            if dx * dx + dy * dy < 0.66 and (x + y) & 1 and g[y][x] is None:
                g[y][x] = c


def _glowRing(g: Grid, cx: float, cy: float, r: float, c: Color | None) -> None:
    if c is None:
        return
    for y in range(len(g)):
        for x in range(len(g[0])):
            dx = (x - cx) / (r + 2.4)
            dy = (y - cy) / (r + 2.4)
            d = dx * dx + dy * dy
            if 0.62 <= d <= 1.0 and (x + y) & 1 and g[y][x] is None:
                _px(g, x, y, c)


def _rect(g: Grid, x: int, y: int, w: int, h: int, c: Color | None) -> None:
    if c is None:
        return
    for j in range(h):
        for i in range(w):
            _px(g, x + i, y + j, c)


def _line(g: Grid, x0: float, y0: float, x1: float, y1: float, c: Color | None) -> None:
    if c is None:
        return
    x0i = int(x0); y0i = int(y0); x1i = int(x1); y1i = int(y1)
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


def _stars(g: Grid, maxY: float, dens: float) -> None:
    w = len(g[0])
    s = 3
    gy = 0
    while gy * s < maxY:
        gx = 0
        while gx * s < w:
            if _hash(gx, gy) >= dens:
                gx += 1
                continue
            jx = gx * s + int(_hash(gx + 101, gy) * s)
            jy = gy * s + int(_hash(gx, gy + 57) * s)
            if jy >= maxY:
                gx += 1
                continue
            br = _hash(gx + 7, gy + 13)
            color = _P["bright"] if br < 0.12 else (_P["star"] if br < 0.55 else _P["starD"])
            _px(g, jx, jy, color)
            gx += 1
        gy += 1


def _sparkle(g: Grid, x: float, y: float, c: Color | None) -> None:
    xi = _round(x)
    yi = _round(y)
    _px(g, xi, yi, c)
    _px(g, xi - 1, yi, c)
    _px(g, xi + 1, yi, c)
    _px(g, xi, yi - 1, c)
    _px(g, xi, yi + 1, c)


# Locked mascot sprite (15 rows x 16 cols). Legend: o=body, O=horn/dark-edge,
# W=eye glint, b=eye, space=transparent.
IMP = [
    "   O        O   ", "  O          O  ", "  O          O  ", "   O        O   ",
    "    OO    OO    ",
    "    oooooooo    ", "   oooooooooo   ", "  oooWbooWbooo  ", "  ooobboobbooo  ",
    "  oooooooooooo  ", "  oooooooooooo  ", "   oooooooooo   ", "    oooooooo    ",
    "    oo    oo    ", "   oo      oo   ",
]

# Locked "imp" wordmark (6 rows x 13 cols). X = pixel.
WM = ["XX           ", "             ", "XX XXXXX XXX ", "XX X X X X  X", "XX X X X XXX ", "         X   "]


def _impAt(g: Grid, xc: float, oy: int) -> None:
    m = {"o": _P["impO"], "O": _P["impH"], "W": _P["impW"], "b": _P["impB"]}
    ox = _round(xc - 8)
    for r in range(len(IMP)):
        for c in range(len(IMP[r])):
            ch = IMP[r][c]
            if ch == " " or ch not in m:
                continue
            _px(g, ox + c, oy + r, m[ch])


def _moonG(g: Grid, cx: int, cy: int, r: int, cres: bool) -> None:
    _glowRing(g, cx, cy, r, _P["moonGlow"])
    _discE(g, cx, cy, r, r, _P["moon"])
    if _P["moonRing"]:
        _ringE(g, cx, cy, r + 0.6, r + 0.6, _P["moonRing"], 0.7, 1.28)
    if cres:
        _carve(g, cx + _round(r * 0.65), cy - 1, r, r)


def _candle2(g: Grid, x: int, baseY: int, h: int) -> None:
    for j in range(h):
        _px(g, x, baseY - j, _P["waxA"])
        _px(g, x + 1, baseY - j, _P["waxB"])
    _px(g, x, baseY - h, _P["fire2"])
    _px(g, x + 1, baseY - h, _P["fire2"])
    _px(g, x, baseY - h - 1, _P["fire3"])
    _px(g, x, baseY - h - 2, _P["fireTip"])
    _px(g, x - 2, baseY - h - 1, _P["glowWarm"])
    _px(g, x + 2, baseY - h - 1, _P["glowWarm"])
    _px(g, x, baseY - h - 4, _P["glowWarm"])


def _cauldron2(g: Grid, cx: int, topY: int, w: int) -> None:
    r = w / 2
    y = topY
    while y < topY + r:
        x = int(cx - r)
        while x <= cx + r:
            dx = (x - cx) / r
            dy = (y - topY) / r
            d = dx * dx + dy * dy
            if d <= 1:
                _px(g, x, y, _P["potD"] if d > 0.66 else _P["pot"])
            x += 1
        y += 1
    x = int(cx - r - 1)
    while x <= cx + r + 1:
        if abs((x - cx) / (r + 1)) <= 1:
            _px(g, x, topY - 1, _P["rim"])
            _px(g, x, topY, _P["rim"])
        x += 1
    x = int(cx - r + 1)
    while x <= cx + r - 1:
        _px(g, x, topY + 1, _P["brew"] if (x + topY) & 1 else _P["brewB"])
        x += 1
    for i in range(5):
        bx = int(cx - r + 2 + int(_hash(i + 3, 2) * (2 * r - 3)))
        by = int(topY - 1 - int(_hash(i, 5) * 4))
        _px(g, bx, by, _P["brewB"])
    _px(g, cx - 2, topY - 4, _P["steam"])
    _px(g, cx + 1, topY - 6, _P["steam"])
    _px(g, cx, topY - 8, _P["steam"])
    _px(g, int(cx - r + 1), int(topY + r), _P["potD"])
    _px(g, int(cx + r - 1), int(topY + r), _P["potD"])


def _portal2(g: Grid, cx: int, cy: int) -> None:
    _ringE(g, cx, cy, 7, 11, _P["portal"], 0.82, 1.06)
    _ringE(g, cx, cy, 5.4, 8.6, _P["portalI"], 0.78, 1.06)
    for y in range(cy - 11, cy + 11):
        if not (0 <= y < len(g)):
            continue
        for x in range(cx - 7, cx + 7):
            dx = (x - cx) / 7
            dy = (y - cy) / 11
            if dx * dx + dy * dy < 0.6 and 0 <= x < len(g[0]) and g[y][x] is None:
                _px(g, x, y, _P["portalSwirl"] if (x + y) & 1 else _P["portalG"])
    _sparkle(g, cx, cy - 6, _P["portalI"])
    _sparkle(g, cx - 2, cy + 4, _P["rune"])


def _crystal2(g: Grid, x: int, baseY: int, h: int) -> None:
    for j in range(h):
        w = int((h - j) / 2)
        for dx in range(-w, w + 1):
            _px(g, x + dx, baseY - j, _P["crysShade"] if dx < 0 else _P["crysLit"])
    _px(g, x, baseY - h, _P["crysTip"])
    _px(g, x, baseY - h + 1, _P["crysTip"])


def _mushroom2(g: Grid, x: int, baseY: int, h: int, cap: Color | None, capD: Color | None) -> None:
    for j in range(h):
        _px(g, x, baseY - j, _P["mushStem"])
    _discE(g, x, baseY - h, 3, 2, cap)
    _px(g, x - 2, baseY - h + 1, capD)
    _px(g, x + 2, baseY - h + 1, capD)
    _px(g, x - 1, baseY - h - 1, _P["mushSpot"])
    _px(g, x + 1, baseY - h, _P["mushSpot"])
    _px(g, x - 2, baseY - 1, _P["mushGlow"])
    _px(g, x + 2, baseY - 1, _P["mushGlow"])


def _planet2(g: Grid, cx: int, cy: int, r: int) -> None:
    _discE(g, cx, cy, r, r, _P["planetA"])
    for y in range(cy - r, cy + r + 1):
        if ((y - cy + 99) & 3) == 0:
            for x in range(cx - r, cx + r + 1):
                if 0 <= y < len(g) and 0 <= x < len(g[0]) and g[y][x] is not None:
                    _px(g, x, y, _P["planetB"])
    for y in range(cy - r, cy + r + 1):
        for x in range(cx, cx + r + 1):
            dx = (x - cx) / r
            dy = (y - cy) / r
            if dx * dx + dy * dy <= 1 and dx > 0.38:
                _px(g, x, y, _P["planetC"])
    _ringE(g, cx, cy, r + 4, r * 0.5, _P["ring"], 0.84, 1.12)


def _torch2(g: Grid, x: int, baseY: int, h: int) -> None:
    for j in range(h):
        _px(g, x, baseY - j, _P["torchPole"])
    _px(g, x - 1, baseY - h + 2, _P["torchBr"])
    _px(g, x + 1, baseY - h + 2, _P["torchBr"])
    _discE(g, x, baseY - h - 1, 2, 3, _P["fire2"])
    _discE(g, x, baseY - h - 1, 1, 2, _P["fire3"])
    _px(g, x, baseY - h - 3, _P["fire3"])
    _px(g, x, baseY - h - 4, _P["fireTip"])
    _px(g, x - 2, baseY - h - 1, _P["glowWarm"])
    _px(g, x + 2, baseY - h - 1, _P["glowWarm"])


def _cloud2(g: Grid, cx: int, cy: int, c: Color | None) -> None:
    _discE(g, cx, cy, 6, 2, c)
    _discE(g, cx - 4, cy + 1, 3, 2, c)
    _discE(g, cx + 4, cy + 1, 4, 2, c)
    _discE(g, cx - 1, cy - 1, 3, 2, c)


def _meteor2(g: Grid, x: int, y: int, length: int, bright: bool) -> None:
    for k in range(length):
        t = k / length
        if t < 0.3:
            c = _P["bright"] if bright else _P["star"]
        elif t < 0.7:
            c = _P["star"]
        else:
            c = _P["starD"]
        _px(g, x + k, y - k, c)
    _sparkle(g, x, y, _P["bright"] if bright else _P["star"])


def _mountains3(g: Grid, baseY: int) -> None:
    w = len(g[0])
    for x in range(w):
        h = _round(4 + 2 * math.sin(x * 0.09 + 2))
        for y in range(baseY - h, baseY + 1):
            if 0 <= y < len(g) and g[y][x] is None:
                _px(g, x, y, _P["mtnFar"])
    for x in range(w):
        h = _round(7 + 3 * math.sin(x * 0.16) + 2 * math.sin(x * 0.07 + 1))
        for y in range(baseY - h, baseY + 1):
            _px(g, x, y, _P["mtnMid"])
        if h >= 10:
            _px(g, x, baseY - h, _P["snow"])
            _px(g, x, baseY - h + 1, _P["snow"])
    for x in range(w):
        h = _round(3 + 2 * math.sin(x * 0.13 + 4))
        for y in range(baseY - h, baseY + 1):
            _px(g, x, y, _P["mtnNear"])


def _firefly(g: Grid, x: int, y: int, warm: bool) -> None:
    core = _P["ffWarmC"] if warm else _P["ffCoolC"]
    halo = _P["ffWarmH"] if warm else _P["ffCoolH"]
    _px(g, x, y, core)
    _px(g, x - 1, y, halo)
    _px(g, x + 1, y, halo)
    _px(g, x, y - 1, halo)
    _px(g, x, y + 1, halo)


def _spellbook(g: Grid, cx: int, baseY: int) -> None:
    for dx in range(-7, 8):
        x = cx + dx
        a = abs(dx)
        top = baseY - 3 - _round(a * 0.35)
        for y in range(top, baseY + 1):
            _px(g, x, y, _P["bookPageD"] if a >= 6 else _P["bookPage"])
    for y in range(baseY - 4, baseY + 1):
        _px(g, cx, y, _P["bookSpine"])
    for dx in range(-5, -1):
        _px(g, cx + dx, baseY - 2, _P["bookInk"])
    for dx in range(2, 6):
        _px(g, cx + dx, baseY - 2, _P["bookInk"])
    _px(g, cx - 2, baseY - 7, _P["rune"])
    _px(g, cx + 2, baseY - 9, _P["portal"])
    _px(g, cx, baseY - 11, _P["rune"])


def _stormcloud(g: Grid, cx: int, cy: int) -> None:
    _discE(g, cx, cy, 5, 2, _P["storm"])
    _discE(g, cx - 4, cy + 1, 3, 2, _P["storm"])
    _discE(g, cx + 4, cy + 1, 3, 2, _P["storm"])
    _discE(g, cx, cy + 1, 6, 2, _P["stormD"])


def _bolt2(g: Grid, x0: int, y0: int, y1: int) -> None:
    pts = [
        [x0, y0],
        [x0 - 2, y0 + _round((y1 - y0) * 0.35)],
        [x0 + 2, y0 + _round((y1 - y0) * 0.62)],
        [x0 - 1, y1],
    ]
    for k in range(len(pts) - 1):
        _line(g, pts[k][0] + 1, pts[k][1], pts[k + 1][0] + 1, pts[k + 1][1], _P["boltGlow"])
    for k in range(len(pts) - 1):
        _line(g, pts[k][0], pts[k][1], pts[k + 1][0], pts[k + 1][1], _P["boltCore"])
    _line(g, pts[1][0], pts[1][1], pts[1][0] + 3, pts[1][1] + 3, _P["boltCore"])


def _crystalball(g: Grid, cx: int, cy: int, r: int) -> None:
    _discE(g, cx, cy, r, r, _P["glass"])
    _glowFill(g, cx, cy, r, r, _P["mist"])
    _px(g, cx - 1, cy, _P["portal"])
    _px(g, cx + 1, cy + 1, _P["rune"])
    _px(g, cx + 1, cy - 1, _P["portalI"])
    _px(g, cx - 2, cy - 2, _P["orbHi"])
    _px(g, cx - 1, cy - 2, _P["orbHi2"])
    _ringE(g, cx, cy, r + 0.4, r + 0.4, _P["orbRim"], 0.78, 1.18)
    for dx in range(-2, 3):
        _px(g, cx + dx, cy + r + 1, _P["orbStand"])
    _px(g, cx - 3, cy + r + 2, _P["orbStand2"])
    _px(g, cx + 3, cy + r + 2, _P["orbStand2"])
    _px(g, cx - 2, cy + r + 2, _P["orbStand"])
    _px(g, cx + 2, cy + r + 2, _P["orbStand"])
    _px(g, cx, cy + r + 2, _P["orbStand"])


def _lantern2(g: Grid, x: int, topY: int, length: int) -> None:
    for j in range(length):
        _px(g, x, topY + j, _P["lanMetal2"])
    by = topY + length
    _px(g, x, by, _P["lanMetal"])
    _px(g, x - 1, by + 1, _P["lanMetal"])
    _px(g, x + 1, by + 1, _P["lanMetal"])
    _rect(g, x - 2, by + 2, 5, 4, _P["lanMetal2"])
    _rect(g, x - 1, by + 3, 3, 2, _P["lanGlow"])
    _px(g, x, by + 3, _P["lanCore"])
    _px(g, x - 2, by + 3, _P["lanMetal"])
    _px(g, x + 2, by + 3, _P["lanMetal"])
    _px(g, x, by + 2, _P["lanMetal"])
    _px(g, x - 1, by + 6, _P["lanMetal"])
    _px(g, x, by + 6, _P["lanMetal"])
    _px(g, x + 1, by + 6, _P["lanMetal"])
    _px(g, x, by + 7, _P["lanMetal"])
    _px(g, x - 3, by + 4, _P["lanHalo"])
    _px(g, x + 3, by + 4, _P["lanHalo"])


def _scene1(g: Grid, W: int) -> None:
    cx = int(W * GL)
    mb = int(W * 0.72)
    _stars(g, 9, 0.34)
    rx = int(min(22, max(13, W * 0.13)))
    _ringE(g, cx, 22, rx, 3, _P["portal"], 0.6, 1.18)
    _ringE(g, cx, 22, rx - 3, 2.4, _P["portalG"], 0.5, 1.3)
    _glowFill(g, cx, 22, rx, 3, _P["portalG"])
    a = 0.0
    while a < 360:
        t = a * math.pi / 180
        _sparkle(g, cx + math.cos(t) * (rx + 1), 22 + math.sin(t) * 4, _P["rune"])
        a += 137.5
    _moonG(g, mb, 6, 3, True)
    _impAt(g, cx, 6)


def _scene2(g: Grid, W: int) -> None:
    cx = int(W * GL)
    mb = int(W * 0.72)
    _stars(g, H, 0.32)
    _moonG(g, mb, int(H * 0.24), 4, True)
    _sparkle(g, cx - int(W * 0.12), 5, _P["bright"])
    _sparkle(g, cx + int(W * 0.16), 3, _P["star"])
    _impAt(g, cx, 9)


def _scene3(g: Grid, W: int) -> None:
    cx = int(W * GL)
    _stars(g, H, 0.30)
    _glowRing(g, cx + 5, 13, 8, _P["moonGlow"])
    _discE(g, cx + 5, 13, 8, 8, _P["moon"])
    if _P["moonRing"]:
        _ringE(g, cx + 5, 13, 8.6, 8.6, _P["moonRing"], 0.72, 1.2)
    _carve(g, cx + 11, 10, 8, 8)
    _sparkle(g, cx + 13, 5, _P["bright"])
    _impAt(g, cx - 3, 9)


def _scene4(g: Grid, W: int) -> None:
    cx = int(W * GL)
    _stars(g, 9, 0.32)
    _impAt(g, cx, 6)
    _candle2(g, cx - 12, 24, 7)
    _candle2(g, cx + 12, 24, 7)


def _scene5(g: Grid, W: int) -> None:
    cx = int(W * GL)
    mb = int(W * 0.72)
    _stars(g, 9, 0.30)
    _moonG(g, mb, 5, 3, True)
    _impAt(g, cx, 3)
    _cauldron2(g, cx, 18, 18)


def _scene6(g: Grid, W: int) -> None:
    cx = int(W * GL)
    _stars(g, 8, 0.30)
    _portal2(g, cx, 14)
    _impAt(g, cx + 11, 7)


def _scene7(g: Grid, W: int) -> None:
    cx = int(W * GL)
    _stars(g, 9, 0.32)
    _mountains3(g, 23)
    _impAt(g, cx - 5, 7)
    _line(g, cx + 6, 23, cx + 14, 21, _P["glowWarm"])
    _line(g, cx + 6, 21, cx + 14, 23, _P["glowWarm"])
    for i in range(6):
        w = 3 - (i // 2)
        for x in range(cx + 10 - w, cx + 10 + w + 1):
            _px(g, x, 22 - i, _P["fire1"] if i < 2 else (_P["fire2"] if i < 4 else _P["fire3"]))
    for k in range(3):
        _px(g, cx + 9 + (k & 1), 12 - k * 2, _P["ember"])


def _scene8(g: Grid, W: int) -> None:
    cx = int(W * GL)
    _stars(g, H, 0.16)
    n = max(5, int(W / 16))
    p: list[list[int]] = []
    for i in range(n):
        p.append([int((i + 0.5) * W / n),
                  3 + _round(3 * abs(math.sin(i * 1.3))) + int(_hash(i + 2, 4) * 3)])
    for k in range(len(p) - 1):
        _line(g, p[k][0], p[k][1], p[k + 1][0], p[k + 1][1], _P["starD"])
    for i, q in enumerate(p):
        if i % 2:
            _px(g, q[0], q[1], _P["star"])
        else:
            _sparkle(g, q[0], q[1], _P["bright"])
    _impAt(g, cx, 11)


def _scene9(g: Grid, W: int) -> None:
    cx = int(W * GL)
    _stars(g, 9, 0.26)
    for x in range(W):
        _px(g, x, 25, _P["ground"])
        if x & 1:
            _px(g, x, 24, _P["groundGlow"])
    _impAt(g, cx, 7)
    _mushroom2(g, cx - 12, 25, 5, _P["cap1"], _P["cap1D"])
    _mushroom2(g, cx + 12, 25, 6, _P["cap2"], _P["cap2D"])
    _mushroom2(g, cx - 6, 25, 3, _P["cap3"], _P["cap1D"])


def _scene10(g: Grid, W: int) -> None:
    cx = int(W * GL)
    _stars(g, 9, 0.28)
    _impAt(g, cx, 7)
    _crystal2(g, cx - 13, 25, 7)
    _crystal2(g, cx - 9, 25, 4)
    _crystal2(g, cx + 11, 25, 6)
    _crystal2(g, cx + 15, 25, 4)
    _px(g, cx - 11, 24, _P["crysBase"])
    _px(g, cx + 13, 24, _P["crysBase"])


def _scene11(g: Grid, W: int) -> None:
    cx = int(W * GL)
    mb = int(W * 0.72)
    _stars(g, H, 0.32)
    _planet2(g, mb, 8, 5)
    _impAt(g, cx, 8)


def _scene12(g: Grid, W: int) -> None:
    cx = int(W * GL)
    _stars(g, 9, 0.30)
    _impAt(g, cx, 4)
    _crystalball(g, cx + 14, 16, 5)


def _scene13(g: Grid, W: int) -> None:
    cx = int(W * GL)
    _stars(g, 9, 0.30)
    _impAt(g, cx, 7)
    _torch2(g, cx - 13, 24, 8)
    _torch2(g, cx + 13, 24, 8)


def _scene14(g: Grid, W: int) -> None:
    cx = int(W * GL)
    _stars(g, 9, 0.30)
    _impAt(g, cx, 2)
    _spellbook(g, cx, 24)


def _scene15(g: Grid, W: int) -> None:
    cx = int(W * GL)
    mb = int(W * 0.72)
    _stars(g, 6, 0.28)
    _stormcloud(g, mb, 5)
    _bolt2(g, mb, 8, 20)
    _impAt(g, cx, 9)


def _scene16(g: Grid, W: int) -> None:
    cx = int(W * GL)
    mb = int(W * 0.72)
    _stars(g, H, 0.34)
    _moonG(g, mb, int(H * 0.22), 4, False)
    _cloud2(g, mb - 1, int(H * 0.22) + 3, _P["cloud"])
    _impAt(g, cx, 6)


def _scene17(g: Grid, W: int) -> None:
    cx = int(W * GL)
    _stars(g, H, 0.24)
    n = max(3, int(W / 40))
    for i in range(n):
        x = int(_hash(i + 1, 7) * (W - 8))
        y = 2 + int(_hash(i + 1, 9) * 9)
        length = 4 + int(_hash(i, 3) * 4)
        _meteor2(g, x, y, length, i % 3 == 0)
    _impAt(g, cx, 9)


def _scene18(g: Grid, W: int) -> None:
    cx = int(W * GL)
    _stars(g, 9, 0.28)
    _lantern2(g, cx + 13, 0, 5)
    _impAt(g, cx, 8)


def _scene19(g: Grid, W: int) -> None:
    cx = int(W * GL)
    mb = int(W * 0.72)
    hor = int(H * 0.70)
    _stars(g, hor - 2, 0.32)
    _moonG(g, mb, int(H * 0.22), 4, True)
    _mountains3(g, hor)
    _impAt(g, cx, hor - 15)


def _scene20(g: Grid, W: int) -> None:
    cx = int(W * GL)
    _stars(g, 6, 0.14)
    gy = 1
    while gy * 4 < 23:
        gx = 0
        while gx * 4 < W:
            if _hash(gx + 9, gy + 5) >= 0.20:
                gx += 1
                continue
            fx = gx * 4 + int(_hash(gx, gy + 3) * 4)
            fy = gy * 4 + int(_hash(gx + 3, gy) * 4)
            _firefly(g, fx, fy, _hash(gx, gy) < 0.4)
            gx += 1
        gy += 1
    _impAt(g, cx, 7)


SCENES: list[tuple[int, str, SceneBuilder]] = [
    (1, "a familiar, summoned to help you ship.", _scene1),
    (2, "the familiar awakens.", _scene2),
    (3, "two horns, perched and ready.", _scene3),
    (4, "whisper your wish.", _scene4),
    (5, "stir the cauldron — what's brewing?", _scene5),
    (6, "the portal is open. step through.", _scene6),
    (7, "by emberlight, we code.", _scene7),
    (8, "drawn in the stars.", _scene8),
    (9, "let's make some mischief.", _scene9),
    (10, "small imp, big leverage.", _scene10),
    (11, "conjured from the void.", _scene11),
    (12, "gaze in — what shall we build?", _scene12),
    (13, "ready to raise some hell in the diff.", _scene13),
    (14, "every great hack needs a familiar.", _scene14),
    (15, "mischief, compiled.", _scene15),
    (16, "what are we building tonight?", _scene16),
    (17, "make a wish.", _scene17),
    (18, "point me at the problem.", _scene18),
    (19, "welcome back.", _scene19),
    (20, "your imp is listening.", _scene20),
]


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


def _encode_grid(g: Grid, rows: int, *, color: bool) -> list[str]:
    return [_encode_row(g, 2 * k, 2 * k + 1, color=color) for k in range(rows)]


def _wordmark_grid() -> Grid:
    wm = _P["wm"]
    return [[wm if ch == "X" else None for ch in row] for row in WM]


def render_scene(index: int, *, theme: str, width: int,
                 color: bool) -> tuple[list[str], str]:
    """Build scene `index` at `width` in `theme`; return (lines, phrase)."""
    _use_theme(theme)
    g = _make_grid(width)
    _num, phrase, builder = SCENES[index]
    builder(g, width)
    lines = _encode_grid(g, 14, color=color)
    lines += _encode_grid(_wordmark_grid(), 3, color=color)
    return lines, phrase


class _Rng(Protocol):
    def randrange(self, n: int, /) -> int: ...


def choose_index(rng: "_Rng | None" = None) -> int:
    r: _Rng = rng if rng is not None else random
    return r.randrange(len(SCENES))


def styled_phrase(phrase: str, *, theme: str, color: bool) -> str:
    if not color:
        return phrase
    return _fg_open(_build_palette(theme)["text"]) + phrase + _RESET


def render_splash(*, theme: str, width: int, color: bool,
                  rng: "_Rng | None" = None) -> tuple[list[str], str]:
    return render_scene(choose_index(rng), theme=theme, width=width, color=color)


def splash_block(*, theme: str, width: int, color: bool,
                 rng: "_Rng | None" = None) -> str:
    """The full printable splash: art + wordmark + styled phrase, newline-joined."""
    lines, phrase = render_splash(theme=theme, width=width, color=color, rng=rng)
    return "\n".join([*lines, styled_phrase(phrase, theme=theme, color=color)])
