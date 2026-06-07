"""Render a pyte screen to a PNG — the faithful *visual*, not just the glyphs.

view.py answers "what characters are on screen"; this answers "what does it
look like" — fg/bg color, bold weight, box-drawing alignment. That's the
difference that matters when judging whether a TUI looks clean. The per-cell
style is already captured by pyte (see view.Cell); here we paint it.

Pure-ish: takes a pyte screen, writes an image. No pty/process/IO beyond the
file write, so it's drivable from a script or a future `snapshot` MCP tool."""

from __future__ import annotations

import contextlib
import os
import re
import shutil
import subprocess
from functools import cache
from typing import Any, cast

from PIL import Image, ImageDraw, ImageFont

from impty.screen import Regime

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

# Fonts are referenced by FAMILY, resolved to a file at runtime (see _fc_match /
# _resolve_*). The absolute `/usr/share/...` paths below are only fast-path HINTS
# for this dev box — on a system without them, fontconfig resolves the family
# instead, and the mono path ultimately falls back to the generic "monospace"
# family (always present where fontconfig is) so a capture never crashes for a
# missing font. Sizes are derived from the font's own metrics (see _cell_metrics).

# Source Code Pro — the default mockup typeface (10pt; see _DEFAULT_FONT_SIZE).
# Monospace, with the box-drawing + symbol glyphs imp's TUI uses (· → │ ⏸ ▾ ✓ ─).
_MONO_FAMILY = "Source Code Pro"
_MONO_HINTS = {
    False: "/usr/share/fonts/adobe-source-code-pro-fonts/SourceCodePro-Regular.otf",
    True:  "/usr/share/fonts/adobe-source-code-pro-fonts/SourceCodePro-Bold.otf",
}

# 10pt: PIL sizes a font in pixels at 72 DPI, so 10pt → font_size=10. Cells are
# derived from the font's own metrics (see _cell_metrics), so the grid stays
# aligned at any size without hand-tuned cell_w/cell_h.
_DEFAULT_FONT_SIZE = 10

# Supersample factor: the whole grid (font px, cell, pad) is multiplied by this,
# so a "10pt" capture is actually rasterized at 30px — a crisp, high-DPI PNG
# (true FreeType rendering at the larger size, not an upscale of a small image).
# The layout is identical; only the pixel density changes.
_DEFAULT_SCALE = 3

# Fallback fonts for codepoints the mono font lacks. Source Code Pro covers most
# of imp's chrome (· → │ ▾ ✓ ─) but NOT some UI symbols (⏸, ⚠) or emoji (which
# agents emit in prose, e.g. 👋); without a fallback those render as .notdef tofu
# (□), so a design capture lies about what a capable terminal shows. (family,
# path-hint) pairs, consulted per-glyph in priority order; the first whose cmap
# covers the codepoint wins. ORDER MATTERS: the TEXT-presentation symbol fonts
# come first so a glyph like ⏸ renders as a slim text mark (matching a real
# terminal), not Noto Emoji's chunky emoji-style pause; the emoji font is LAST so
# only true emoji (which the symbol fonts lack, e.g. 👋) reach it. Monochrome Noto
# Emoji (not the color bitmap font) so it scales to any cell size and paints in
# the cell's fg. A family that resolves nowhere just drops out (→ tofu again).
_FALLBACK_SPECS = (
    ("Noto Sans Symbols 2", "/usr/share/fonts/google-noto/NotoSansSymbols2-Regular.ttf"),
    ("DejaVu Sans", "/usr/share/fonts/dejavu-sans-fonts/DejaVuSans.ttf"),
    ("Noto Emoji", "/usr/share/fonts/google-noto-emoji-fonts/NotoEmoji-Regular.ttf"),
)


@cache
def _fc_match(pattern: str, want: str | None = None) -> str | None:
    """Resolve a fontconfig pattern to a file path via `fc-match`. fc-match always
    returns SOME font, so when `want` is given we only accept a result whose family
    contains it — guarding against silently picking an unrelated family for a font
    that isn't installed. Returns None when fc-match is absent (e.g. a bare macOS
    box) or yields nothing usable, letting callers degrade gracefully."""
    fc = shutil.which("fc-match")
    if not fc:
        return None
    try:
        r = subprocess.run([fc, "-f", "%{file}\t%{family}", pattern],
                           capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    path, _, family = r.stdout.partition("\t")
    if want and want.lower() not in family.lower():
        return None
    return path if path and os.path.exists(path) else None


@cache
def _resolve_mono(bold: bool) -> str | None:
    """A concrete monospace font path: the bundled hint if present, else Source
    Code Pro via fontconfig, else ANY monospace family (so the grid stays aligned
    on a box without SCP), else None → _load_font degrades to PIL's builtin so a
    capture never crashes for a missing font."""
    style = "Bold" if bold else "Regular"
    hint = _MONO_HINTS[bold]
    if os.path.exists(hint):
        return hint
    return (_fc_match(f"{_MONO_FAMILY}:style={style}", want=_MONO_FAMILY)
            or _fc_match(f"monospace:style={style}"))


@cache
def _resolve_fallbacks() -> tuple[str, ...]:
    """Concrete paths for the glyph-fallback fonts: each spec's bundled hint if
    present, else fontconfig by family. Families that resolve nowhere drop out."""
    out: list[str] = []
    for family, hint in _FALLBACK_SPECS:
        p = hint if os.path.exists(hint) else _fc_match(family, want=family)
        if p:
            out.append(p)
    return tuple(out)


@cache
def _load_font(path: str | None, size: int, bold: bool) -> ImageFont.FreeTypeFont:
    """Load a font at `size`; `path=None` (or a load failure) falls back to PIL's
    builtin so the renderer never raises for a missing font file."""
    # load_default(size) returns a FreeTypeFont at runtime (Pillow ≥10), but its
    # stub is the broader FreeTypeFont|ImageFont union — cast to keep the metrics
    # API (getmetrics/getlength) typed.
    try:
        if path is None:
            return cast(ImageFont.FreeTypeFont, ImageFont.load_default(size))
        font = ImageFont.truetype(path, size)
        # variable fonts only: pick the weight axis; a no-op on static OTFs.
        with contextlib.suppress(OSError, ValueError, AttributeError):
            font.set_variation_by_name(b"Bold" if bold else b"Regular")
        return font
    except (OSError, ValueError):
        return cast(ImageFont.FreeTypeFont, ImageFont.load_default(size))


@cache
def _coverage(path: str | None) -> frozenset[int]:
    """The set of codepoints a font's cmap can render. Empty if the path is None,
    missing, or fontTools can't read it (→ the font is simply skipped)."""
    if not path:
        return frozenset()
    try:
        from fontTools.ttLib import TTFont
        return frozenset(TTFont(path, fontNumber=0, lazy=True).getBestCmap())
    except Exception:
        return frozenset()


@cache
def _fallback_chain() -> tuple[tuple[str, frozenset[int]], ...]:
    """Resolved fallback fonts paired with their coverage, in priority order."""
    return tuple(
        (p, _coverage(p)) for p in _resolve_fallbacks() if _coverage(p)
    )


@cache
def _cell_metrics(size: int) -> tuple[int, int]:
    """The (cell_w, cell_h) the mono font wants at `size`: the advance width of a
    monospace glyph and the font's ascent+descent line height. Deriving the grid
    from the font (rather than hand-tuned constants) keeps columns aligned and
    box-drawing characters meeting whatever font/size the renderer uses."""
    import math
    f = _load_font(_resolve_mono(False), size, bold=False)
    asc, desc = f.getmetrics()
    return max(1, math.ceil(f.getlength("0"))), max(1, asc + desc)


def _font_for(ch: str, size: int, bold: bool) -> ImageFont.FreeTypeFont:
    """The font to paint `ch` with: the mono font when it covers the glyph,
    otherwise the first fallback that does, else the mono font (a tofu box is
    still better than crashing). Keeps columns aligned because every font is drawn
    at the same cell origin; a wide emoji may bleed past one cell, which is
    acceptable in a design mockup and rare in practice."""
    mono = _resolve_mono(bold)
    cp = ord(ch[0])
    if cp in _coverage(mono):
        return _load_font(mono, size, bold)
    for path, cov in _fallback_chain():
        if cp in cov:
            return _load_font(path, size, bold)
    return _load_font(mono, size, bold)


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
    cell_w: int | None = None,
    cell_h: int | None = None,
    font_size: int = _DEFAULT_FONT_SIZE,
    pad: int = 12,
    scale: int = _DEFAULT_SCALE,
    bg: tuple[int, int, int] | None = None,
    fg: tuple[int, int, int] | None = None,
    palette: dict[str, tuple[int, int, int]] | None = None,
    full: bool = False,
) -> str:
    """Paint `screen` to a PNG at `path`. Returns path.

    Renders the live rows x cols frame by default; `full=True` (scroll shape)
    prepends scrolled-off history above it — a tall strip, so it is opt-in.

    Walks the cell grid, resolving each cell's fg/bg/bold/reverse to pixels.
    Cell size is fixed (monospace), so columns stay aligned and box-drawing
    characters meet — the thing that makes a TUI look crisp or broken. `cell_w`/
    `cell_h` default to the mono font's own metrics at `font_size` (Source Code
    Pro 10pt), so the grid matches the glyphs; pass them only to override.

    `scale` supersamples the whole render (font px, cells, pad) for a crisp,
    high-DPI PNG — the glyphs are rasterized at `font_size * scale`, not upscaled.

    `bg`/`fg` override the default dark surface and `palette` the named-color map
    (pyte color name → RGB) — an app-aware caller passes its own theme so the PNG
    matches what that app paints. Defaults are a neutral dark terminal."""
    surface_bg = bg if bg is not None else _BG
    surface_fg = fg if fg is not None else _FG
    colors = palette if palette is not None else _PALETTE
    # Supersample: rasterize the font at scale*size and scale the grid to match.
    font_px = font_size * scale
    derived_w, derived_h = _cell_metrics(font_px)
    cell_w = derived_w if cell_w is None else cell_w * scale
    cell_h = derived_h if cell_h is None else cell_h * scale
    pad *= scale
    cols = screen.columns
    # In scroll shape, full=True prepends scrolled-off history.top above the live
    # screen (a potentially very tall strip — up to history rows — hence opt-in).
    # Default (and alt shape) renders just the live rows x cols frame.
    live_rows = [screen.buffer[y] for y in range(screen.lines)]
    # full=True prepends scrolled-off history.top — but ONLY in scroll shape.
    # In SCREEN (alt) regime there is no scrollback (history.top may still hold
    # PRE-alt rows, which must NOT leak above the alt frame), matching view.lines().
    in_scroll = getattr(screen, "regime", None) is not Regime.SCREEN
    history = list(screen.history.top) if (full and in_scroll) else []
    grid = history + live_rows
    rows = len(grid)
    w = cols * cell_w + 2 * pad
    h = rows * cell_h + 2 * pad
    img = Image.new("RGB", (w, h), surface_bg)
    draw = ImageDraw.Draw(img)

    for y, row in enumerate(grid):
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
                # Per-glyph font: the mono font for chrome, a fallback for emoji /
                # symbols it lacks (so 👋, ⚠, … don't render as tofu boxes).
                draw.text(
                    (px, py),
                    data,
                    font=_font_for(data, font_px, ch.bold),
                    fill=fg_px,
                )
    img.save(path)
    return path
