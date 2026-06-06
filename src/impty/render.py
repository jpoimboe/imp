"""Render a pyte screen to a PNG — the faithful *visual*, not just the glyphs.

view.py answers "what characters are on screen"; this answers "what does it
look like" — fg/bg color, bold weight, box-drawing alignment. That's the
difference that matters when judging whether a TUI looks clean. The per-cell
style is already captured by pyte (see view.Cell); here we paint it.

Pure-ish: takes a pyte screen, writes an image. No pty/process/IO beyond the
file write, so it's drivable from a script or a future `snapshot` MCP tool."""

from __future__ import annotations

import contextlib
import re
from typing import Any

from PIL import Image, ImageDraw, ImageFont

# A dark terminal theme for the mockup. The 16 ANSI names map to ONE cohesive,
# desaturated family (low-saturation pastels on near-black, in the spirit of
# Selenized / ayu-mirage) so the render reads as a single designed surface rather
# than the default Tango neon — the saturated cyan/green were the loudest, so they
# recede the most. This affects only the snap-ui PNG (real terminals paint ANSI
# from the user's own theme); it exists so colour cohesion can be judged from the
# mockup. Every hue is reachable from ANSI-16, so nothing here steers the design
# toward colours the codes can't emit.
_BG = (0x1B, 0x1D, 0x21)
_FG = (0xC5, 0xC8, 0xC6)

_PALETTE: dict[str, tuple[int, int, int]] = {
    "black": (0x2A, 0x2D, 0x32),
    "red": (0xC0, 0x6A, 0x66),
    "green": (0x90, 0xA9, 0x59),  # muted olive, not neon lime
    "brown": (0xC4, 0xA0, 0x00),  # pyte's name for ANSI yellow
    "yellow": (0xC9, 0xA5, 0x54),
    "blue": (0x6E, 0x90, 0xB8),
    "magenta": (0xA8, 0x84, 0xB5),
    "cyan": (0x5E, 0xA0, 0x9E),  # muted teal, not neon cyan
    "white": (0xC5, 0xC8, 0xC6),
    "brightblack": (0x55, 0x57, 0x53),  # chrome / breadcrumbs — almost invisible
    "brightred": (0xD6, 0x7B, 0x76),
    "brightgreen": (0xA1, 0xBA, 0x6A),
    "brightbrown": (0xDB, 0xBC, 0x6A),
    "brightyellow": (0xDB, 0xBC, 0x6A),
    "brightblue": (0x7F, 0xA2, 0xCA),
    "brightmagenta": (0xBA, 0x96, 0xC6),
    "brightcyan": (0x6F, 0xB3, 0xB0),
    "brightwhite": (0xE6, 0xE8, 0xE6),
}

# Adwaita Mono — monospace with box-drawing AND the symbol glyphs imp's TUI uses
# (✓ ↳ ⏺ · → ⟨⟩ │). Noto Sans Mono (the earlier choice) renders ✓/↳/⏺ as .notdef
# tofu, so those markers vanished from the mockup; Adwaita has them. Separate
# Regular/Bold files (not a variable font), so bold selects the Bold file.
_MONO = "/usr/share/fonts/adwaita-mono-fonts/AdwaitaMono-Regular.ttf"
_MONO_BOLD = "/usr/share/fonts/adwaita-mono-fonts/AdwaitaMono-Bold.ttf"


def _load_font(path: str, size: int, bold: bool) -> ImageFont.FreeTypeFont:
    font = ImageFont.truetype(path, size)
    # non-variable font: the weight axis is absent, so selection is a no-op
    with contextlib.suppress(OSError, ValueError, AttributeError):
        font.set_variation_by_name(b"Bold" if bold else b"Regular")
    return font


# pyte models bold/italics/underscore/reverse but NOT faint (SGR 2 isn't in its
# graphics table), so dim text would otherwise render at full intensity. We can't
# recover it from the screen — it has to be rewritten in the byte stream BEFORE
# pyte parses it. imp only ever emits `dim` (CSI 2 m) on default-fg chrome
# (metadata / thinking / tiers), so substituting a flat dim-gray foreground is
# faithful; the trailing reset (CSI 0 m) clears it like any other attribute.
_FAINT_RE = re.compile("\x1b\\[2m")
_FAINT_GRAY = "\x1b[38;2;133;133;133m"


def show_faint(ansi: str) -> str:
    """Rewrite the faint SGR (CSI 2 m) into an explicit dim-gray fg so the
    renderer shows dimmed text. Apply to a byte/str stream before feeding pyte."""
    return _FAINT_RE.sub(_FAINT_GRAY, ansi)


def _rgb(name: str, default: tuple[int, int, int]) -> tuple[int, int, int]:
    """Resolve a pyte color (a 16-color name, a 6-hex string, or 'default')."""
    if name == "default":
        return default
    hit = _PALETTE.get(name)
    if hit is not None:
        return hit
    if len(name) == 6:  # pyte stores 256/truecolor as a bare hex string
        try:
            return (int(name[0:2], 16), int(name[2:4], 16), int(name[4:6], 16))
        except ValueError:
            pass
    return default


# A light-terminal surface, for previewing the `light` theme (dark text on a
# near-white background, mirroring what a light terminal paints).
_LIGHT_BG = (0xFA, 0xFA, 0xF8)
_LIGHT_FG = (0x2A, 0x2A, 0x2A)


def render_screen(
    screen: Any,
    path: str,
    *,
    cell_w: int = 9,
    cell_h: int = 19,
    font_size: int = 16,
    pad: int = 12,
    bg: tuple[int, int, int] | None = None,
    fg: tuple[int, int, int] | None = None,
) -> str:
    """Paint the current viewport of `screen` to a PNG at `path`. Returns path.

    Walks the cell grid, resolving each cell's fg/bg/bold/reverse to pixels.
    Cell size is fixed (monospace), so columns stay aligned and box-drawing
    characters meet — the thing that makes a TUI look crisp or broken.

    `bg`/`fg` override the default dark surface — pass `_LIGHT_BG`/`_LIGHT_FG`
    to preview the `light` theme on a near-white background."""
    surface_bg = bg if bg is not None else _BG
    surface_fg = fg if fg is not None else _FG
    rows = screen.lines
    cols = screen.columns
    w = cols * cell_w + 2 * pad
    h = rows * cell_h + 2 * pad
    img = Image.new("RGB", (w, h), surface_bg)
    draw = ImageDraw.Draw(img)
    font = _load_font(_MONO, font_size, bold=False)
    font_bold = _load_font(_MONO_BOLD, font_size, bold=True)

    for y in range(rows):
        row = screen.buffer[y]
        for x in range(cols):
            ch = row[x]
            data = ch.data or " "
            fg_px = _rgb(ch.fg, surface_fg)
            bg_px = _rgb(ch.bg, surface_bg)
            if ch.reverse:
                fg_px, bg_px = bg_px, fg_px
            px = pad + x * cell_w
            py = pad + y * cell_h
            if bg_px != surface_bg:
                draw.rectangle([px, py, px + cell_w, py + cell_h], fill=bg_px)
            if data != " ":
                draw.text(
                    (px, py),
                    data,
                    font=font_bold if ch.bold else font,
                    fill=fg_px,
                )
    img.save(path)
    return path
