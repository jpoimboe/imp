from __future__ import annotations

import math
from pathlib import Path

import pyte
import pytest
from PIL import Image

from impty.render import _MAX_DIM, _cell_metrics, light_surface, render_screen
from impty.screen import TerminalScreen


def _screen(cols: int, rows: int) -> TerminalScreen:
    """A screen of the given grid size with one line of text on it. Only the grid
    size drives the fit, so the content stays light — painting every cell costs
    ~30x more for the same dimensions."""
    screen = TerminalScreen(cols, rows, history=100, ratio=0.5)
    pyte.ByteStream(screen).feed(b"impty auto-fit")
    return screen


def _rendered_size(path: str) -> tuple[int, int]:
    """The (width, height) of a written PNG, decoded — a corrupt file fails here."""
    with Image.open(path) as img:
        img.load()
        return img.size


def test_light_surface_paints_a_pale_canvas_not_the_dark_default(tmp_path: Path) -> None:
    # The light option swaps the surface so an app's light theme previews on the
    # background it was tuned for, instead of the dark default (which inverts the
    # intended contrast). The corner is pure padding, so it is the surface bg.
    screen = TerminalScreen(20, 4, history=10, ratio=0.5)
    pyte.ByteStream(screen).feed(b"hi")
    bg, fg = light_surface()
    dark = Image.open(render_screen(screen, str(tmp_path / "d.png"))).convert("RGB")
    light = Image.open(
        render_screen(screen, str(tmp_path / "l.png"), bg=bg, fg=fg)).convert("RGB")
    assert dark.getpixel((0, 0)) == (0x1E, 0x1E, 0x1E)   # default dark surface
    assert light.getpixel((0, 0)) == bg                  # pale surface instead
    assert sum(bg) > sum(dark.getpixel((0, 0)))          # genuinely lighter


def test_render_full_includes_history_rows(tmp_path: Path) -> None:
    # full=True walks history.top + the live screen, so the image is taller than
    # the default current-frame render.
    screen = TerminalScreen(20, 4, history=100, ratio=0.5)
    stream = pyte.ByteStream(screen)
    for i in range(12):
        stream.feed(f"row {i}\r\n".encode())   # scroll rows into history
    frame = render_screen(screen, str(tmp_path / "frame.png"))
    full = render_screen(screen, str(tmp_path / "full.png"), full=True)
    with Image.open(full) as full_img, Image.open(frame) as frame_img:
        assert full_img.height > frame_img.height


def test_render_full_excludes_prealt_history_in_screen_regime(tmp_path: Path) -> None:
    # In alt (SCREEN) regime, full=True must NOT paint pre-alt scrollback above
    # the alt frame — it matches view.lines() (which skips scrollback in alt).
    screen = TerminalScreen(20, 4, history=100, ratio=0.5)
    stream = pyte.ByteStream(screen)
    for i in range(12):
        stream.feed(f"row {i}\r\n".encode())     # pre-alt rows scroll into history
    stream.feed(b"\x1b[?1049h\x1b[2J\x1b[HALT")  # enter alt, clear, write one row
    frame = render_screen(screen, str(tmp_path / "frame.png"))
    full = render_screen(screen, str(tmp_path / "full.png"), full=True)
    # No history prepended in SCREEN regime -> full is the same height as the frame.
    with Image.open(full) as full_img, Image.open(frame) as frame_img:
        assert full_img.height == frame_img.height


@pytest.mark.parametrize(("cols", "rows"), [(150, 44), (200, 50)])
def test_autofit_bounds_a_large_grid_without_collapsing_to_the_floor(
    tmp_path: Path, cols: int, rows: int
) -> None:
    # A grid this big rasterizes past 2000 px at a fixed 3x, and process_image then
    # resampled it — that resample is what inflated the payload. The bound must hold
    # at the EXACT measured metrics, so a linearly extrapolated scale that overshoots
    # by a pixel fails here; the lower assert catches a fit that gave up and floored.
    screen = _screen(cols, rows)
    auto = _rendered_size(render_screen(screen, str(tmp_path / "auto.png")))
    one = _rendered_size(render_screen(screen, str(tmp_path / "one.png"), scale=1))
    assert max(auto) <= _MAX_DIM
    assert max(auto) > max(one)


def test_autofit_keeps_an_ordinary_terminal_at_the_full_3x_density(tmp_path: Path) -> None:
    # 80x24 already fits at 3x, so auto-fit must reproduce today's output exactly.
    # Compare against an explicit scale=3 render rather than hardcoding pixels: cell
    # metrics depend on which mono font _resolve_mono finds on the box.
    screen = _screen(80, 24)
    auto = _rendered_size(render_screen(screen, str(tmp_path / "auto.png")))
    three = _rendered_size(render_screen(screen, str(tmp_path / "three.png"), scale=3))
    assert auto == three


def test_autofit_sees_history_rows_not_just_the_live_frame(tmp_path: Path) -> None:
    # The fit runs after the grid is assembled, so a full=True history strip is
    # measured too — at a fixed 3x these ~300 rows rasterize ~11,800 px tall.
    screen = TerminalScreen(20, 4, history=400, ratio=0.5)
    stream = pyte.ByteStream(screen)
    for i in range(300):
        stream.feed(f"row {i}\r\n".encode())
    w, h = _rendered_size(render_screen(screen, str(tmp_path / "full.png"), full=True))
    assert w <= _MAX_DIM
    assert h <= _MAX_DIM


def test_an_explicit_scale_still_overrides_the_fit(tmp_path: Path) -> None:
    # A caller rendering for a human (scripts/snap_imp_ui.py) asks for 3x and gets
    # 3x, ceiling or no ceiling.
    screen = _screen(150, 44)
    auto = _rendered_size(render_screen(screen, str(tmp_path / "auto.png")))
    three = _rendered_size(render_screen(screen, str(tmp_path / "three.png"), scale=3))
    assert three[0] > auto[0]
    assert three[1] > auto[1]
    assert max(three) > _MAX_DIM


def test_a_sub_one_scale_renders_through_the_font_floor(tmp_path: Path) -> None:
    # scale is float|None now, so a fractional scale is representable. It resolves
    # through the same max(1, ...) floor as the fit, because _cell_metrics(0) raises.
    screen = _screen(80, 24)
    tiny = _rendered_size(render_screen(screen, str(tmp_path / "tiny.png"), scale=0.01))
    floor = _rendered_size(render_screen(screen, str(tmp_path / "floor.png"), scale=0.1))
    one = _rendered_size(render_screen(screen, str(tmp_path / "one.png"), scale=1))
    assert tiny == floor        # int(10 * 0.01) = 0 and int(10 * 0.1) = 1 both floor to 1
    assert max(tiny) < max(one)


def test_a_history_strip_too_tall_to_fit_renders_best_effort(tmp_path: Path) -> None:
    # Derive the row count from the floor's own metrics so the case binds whatever
    # font is installed: twice the rows that already overflow _MAX_DIM at font_px=1.
    # Without that floor the search walks to font_px=0 and _cell_metrics raises
    # "font size must be greater than 0, not 0". Keep the grid narrow — only height
    # binds this case, and 8 columns keeps the paint cheap.
    rows = 2 * math.ceil(_MAX_DIM / _cell_metrics(1)[1])
    screen = TerminalScreen(8, 4, history=rows + 10, ratio=0.5)
    stream = pyte.ByteStream(screen)
    for _ in range(rows):
        stream.feed(b"tall\r\n")
    # Discard the width: only height binds here, and an unused `w` trips ruff RUF059.
    _, h = _rendered_size(render_screen(screen, str(tmp_path / "tall.png"), full=True))
    assert h > _MAX_DIM          # the floor engaged; no fit was found
