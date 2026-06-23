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
_DARK: dict[str, "Color | None"] = {
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
_LIGHT: dict[str, "Color | None"] = {
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
_ANSI_P: dict[str, "Color | None"] = {
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


def _build_palette(theme: str) -> dict[str, "Color | None"]:
    return {"dark": _DARK, "light": _LIGHT, "ansi": _ANSI_P}.get(theme, _ANSI_P)


def _fg_open(c: "Color | None") -> str:
    if c is None:
        return ""
    if isinstance(c, tuple):
        return f"\x1b[38;2;{c[0]};{c[1]};{c[2]}m"
    return f"\x1b[{_NAMED[c][0]}m"


def _bg_open(c: "Color | None") -> str:
    if c is None:
        return ""
    if isinstance(c, tuple):
        return f"\x1b[48;2;{c[0]};{c[1]};{c[2]}m"
    return f"\x1b[{_NAMED[c][1]}m"


# The active palette, set by _use_theme before any scene/primitive runs. Scene
# builders are (grid) -> None, so the palette is module state.
_P: dict[str, "Color | None"] = {}


def _use_theme(theme: str) -> None:
    global _P
    _P = _build_palette(theme)
