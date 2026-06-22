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

from imp.cli import palette as pal

Color = tuple[int, int, int] | str
Grid = list[list["Color | None"]]

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
