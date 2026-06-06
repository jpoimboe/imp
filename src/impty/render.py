"""Render a pyte screen to a PNG — the faithful *visual*, not just the glyphs.

view.py answers "what characters are on screen"; this answers "what does it
look like" — fg/bg color, bold weight, box-drawing alignment. That's the
difference that matters when judging whether a TUI looks clean. The per-cell
style is already captured by pyte (see view.Cell); here we paint it.

Pure-ish: takes a pyte screen, writes an image. No pty/process/IO beyond the
file write, so it's drivable from a script or a future `snapshot` MCP tool."""

from __future__ import annotations

import contextlib
from typing import Any

from PIL import Image, ImageDraw, ImageFont

# A dark terminal theme. Default fg/bg are what an unstyled cell paints as;
# the 16 named ANSI colors use a standard, slightly-muted palette so the
# render reads like a real modern terminal rather than 1980s neon.
_BG = (0x1E, 0x1E, 0x1E)
_FG = (0xD0, 0xD0, 0xD0)

_PALETTE: dict[str, tuple[int, int, int]] = {
    "black": (0x2E, 0x34, 0x36),
    "red": (0xCC, 0x57, 0x57),
    "green": (0x8A, 0xE2, 0x34),
    "brown": (0xC4, 0xA0, 0x00),  # pyte's name for ANSI yellow
    "yellow": (0xCE, 0xA8, 0x36),
    "blue": (0x72, 0x9F, 0xCF),
    "magenta": (0xAD, 0x7F, 0xA8),
    "cyan": (0x34, 0xE2, 0xE2),
    "white": (0xD0, 0xD0, 0xD0),
    "brightblack": (0x55, 0x57, 0x53),
    "brightred": (0xEF, 0x29, 0x29),
    "brightgreen": (0x8A, 0xE2, 0x34),
    "brightbrown": (0xFC, 0xE9, 0x4F),
    "brightyellow": (0xFC, 0xE9, 0x4F),
    "brightblue": (0x72, 0x9F, 0xCF),
    "brightmagenta": (0xAD, 0x7F, 0xA8),
    "brightcyan": (0x34, 0xE2, 0xE2),
    "brightwhite": (0xEE, 0xEE, 0xEC),
}

# Noto Sans Mono — a variable font with full box-drawing + block-element
# coverage (Liberation Mono renders those as .notdef tofu, which would
# misrepresent any TUI that uses block glyphs for logos/borders/markers).
_MONO = "/usr/share/fonts/google-noto-vf/NotoSansMono[wght].ttf"
_MONO_BOLD = _MONO  # same file; weight is selected via the variation axis


def _load_font(path: str, size: int, bold: bool) -> ImageFont.FreeTypeFont:
    font = ImageFont.truetype(path, size)
    # non-variable font: the weight axis is absent, so selection is a no-op
    with contextlib.suppress(OSError, ValueError, AttributeError):
        font.set_variation_by_name(b"Bold" if bold else b"Regular")
    return font


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


def render_screen(
    screen: Any,
    path: str,
    *,
    cell_w: int = 9,
    cell_h: int = 19,
    font_size: int = 16,
    pad: int = 12,
) -> str:
    """Paint the current viewport of `screen` to a PNG at `path`. Returns path.

    Walks the cell grid, resolving each cell's fg/bg/bold/reverse to pixels.
    Cell size is fixed (monospace), so columns stay aligned and box-drawing
    characters meet — the thing that makes a TUI look crisp or broken."""
    rows = screen.lines
    cols = screen.columns
    w = cols * cell_w + 2 * pad
    h = rows * cell_h + 2 * pad
    img = Image.new("RGB", (w, h), _BG)
    draw = ImageDraw.Draw(img)
    font = _load_font(_MONO, font_size, bold=False)
    font_bold = _load_font(_MONO_BOLD, font_size, bold=True)

    for y in range(rows):
        row = screen.buffer[y]
        for x in range(cols):
            ch = row[x]
            data = ch.data or " "
            fg = _rgb(ch.fg, _FG)
            bg = _rgb(ch.bg, _BG)
            if ch.reverse:
                fg, bg = bg, fg
            px = pad + x * cell_w
            py = pad + y * cell_h
            if bg != _BG:
                draw.rectangle([px, py, px + cell_w, py + cell_h], fill=bg)
            if data != " ":
                draw.text(
                    (px, py),
                    data,
                    font=font_bold if ch.bold else font,
                    fill=fg,
                )
    img.save(path)
    return path
