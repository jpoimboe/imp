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

# impty is a GENERIC pty/pyte harness — it knows nothing about imp. The defaults
# below are a neutral dark terminal so a standalone render (e.g. the live MCP
# snapshot) just works. An app-aware caller that wants its OWN theme in the PNG
# (scripts/snap_imp_ui.py renders imp's Selenized palette) injects it via
# render_screen(palette=, bg=, fg=) and show_faint(gray=); nothing here imports
# the app. Truecolor cells (`\x1b[38;2;…m`) always render their exact RGB via
# _rgb regardless, so an injected palette only matters for named-SGR content.
# The default surface is a common dark terminal (#1e1e1e on white text).
_BG = (0x1E, 0x1E, 0x1E)
_FG = (0xFF, 0xFF, 0xFF)

_PALETTE: dict[str, tuple[int, int, int]] = {
    "black": (0x00, 0x00, 0x00), "red": (0xCD, 0x00, 0x00),
    "green": (0x00, 0xCD, 0x00), "brown": (0xCD, 0xCD, 0x00),
    "yellow": (0xCD, 0xCD, 0x00), "blue": (0x33, 0x66, 0xCC),
    "magenta": (0xCD, 0x00, 0xCD), "cyan": (0x00, 0xCD, 0xCD),
    "white": (0xE5, 0xE5, 0xE5), "brightblack": (0x7F, 0x7F, 0x7F),
    "brightred": (0xFF, 0x00, 0x00), "brightgreen": (0x00, 0xFF, 0x00),
    "brightbrown": (0xFF, 0xFF, 0x00), "brightyellow": (0xFF, 0xFF, 0x00),
    "brightblue": (0x55, 0x88, 0xFF), "brightmagenta": (0xFF, 0x00, 0xFF),
    "brightcyan": (0x00, 0xFF, 0xFF), "brightwhite": (0xFF, 0xFF, 0xFF),
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
# Neutral dim-gray for faint text; a caller can pass its theme's chrome gray.
_FAINT_GRAY = "\x1b[38;2;127;127;127m"


def show_faint(ansi: str, gray: str = _FAINT_GRAY) -> str:
    """Rewrite the faint SGR (CSI 2 m) into an explicit dim-gray fg so the
    renderer shows dimmed text. Apply to a byte/str stream before feeding pyte.
    `gray` overrides the dim-gray fg (pass a theme's chrome color as a full SGR)."""
    return _FAINT_RE.sub(gray, ansi)


def _rgb(name: str, default: tuple[int, int, int],
         palette: dict[str, tuple[int, int, int]]) -> tuple[int, int, int]:
    """Resolve a pyte color (a 16-color name, a 6-hex string, or 'default')."""
    if name == "default":
        return default
    hit = palette.get(name)
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
    bg: tuple[int, int, int] | None = None,
    fg: tuple[int, int, int] | None = None,
    palette: dict[str, tuple[int, int, int]] | None = None,
) -> str:
    """Paint the current viewport of `screen` to a PNG at `path`. Returns path.

    Walks the cell grid, resolving each cell's fg/bg/bold/reverse to pixels.
    Cell size is fixed (monospace), so columns stay aligned and box-drawing
    characters meet — the thing that makes a TUI look crisp or broken.

    `bg`/`fg` override the default dark surface and `palette` the named-color map
    (pyte color name → RGB) — an app-aware caller passes its own theme so the PNG
    matches what that app paints. Defaults are a neutral dark terminal."""
    surface_bg = bg if bg is not None else _BG
    surface_fg = fg if fg is not None else _FG
    colors = palette if palette is not None else _PALETTE
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
            fg_px = _rgb(ch.fg, surface_fg, colors)
            bg_px = _rgb(ch.bg, surface_bg, colors)
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
