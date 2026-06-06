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

from imp.cli import palette as pal

# The mockup paints imp's OWN palette, sourced from imp.cli.palette so it can't
# drift from what imp emits live. Dark/light accent cells arrive as truecolor
# (imp emits `\x1b[38;2;…m`) and render their exact RGB directly via _rgb; the
# named map below is consulted only for the `ansi`-theme snapshot (named SGR
# codes) and for the surface defaults — both built from imp's representative
# (dark) palette so there is one source of truth.
_BG, _FG = pal.surface("dark")


def _named_palette() -> dict[str, tuple[int, int, int]]:
    """pyte color name → RGB, built from imp's live palette. Only the eight names
    imp actually emits need entries ('brown' is pyte's name for ANSI yellow,
    'brightblack' the dim chrome gray, 'brightblue' the user's turn); anything
    else falls back to the surface foreground/background."""
    rgb = pal.theme_rgb("dark")
    bg, fg = pal.surface("dark")
    return {
        "red": rgb["red"], "green": rgb["green"],
        "yellow": rgb["yellow"], "brown": rgb["yellow"],
        "blue": rgb["blue"], "magenta": rgb["magenta"], "cyan": rgb["cyan"],
        "brightblack": rgb["gray"], "brightblue": rgb["bright_blue"],
        "black": bg, "white": fg, "brightwhite": fg,
    }


_PALETTE: dict[str, tuple[int, int, int]] = _named_palette()

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
# Dim text (SGR 2) renders as the theme's chrome gray, sourced from imp's palette.
_FAINT_GRAY = "\x1b[38;2;{};{};{}m".format(*pal.theme_rgb("dark")["gray"])


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


# The light surface, for previewing the `light` theme — sourced from imp's palette
# (a warm cream with dark slate text, mirroring what a light terminal paints).
_LIGHT_BG, _LIGHT_FG = pal.surface("light")


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
