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
