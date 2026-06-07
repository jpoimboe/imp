from __future__ import annotations

from pathlib import Path

import pyte
from PIL import Image

from impty.render import render_screen
from impty.screen import TerminalScreen


def test_render_full_includes_history_rows(tmp_path: Path) -> None:
    # full=True walks history.top + the live screen, so the image is taller than
    # the default current-frame render.
    screen = TerminalScreen(20, 4, history=100, ratio=0.5)
    stream = pyte.ByteStream(screen)
    for i in range(12):
        stream.feed(f"row {i}\r\n".encode())   # scroll rows into history
    frame = render_screen(screen, str(tmp_path / "frame.png"))
    full = render_screen(screen, str(tmp_path / "full.png"), full=True)
    assert Image.open(full).height > Image.open(frame).height


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
    assert Image.open(full).height == Image.open(frame).height
