from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from PIL import Image

from impty import TerminalSession


def _can_pty() -> bool:
    try:
        import pty
        m, s = pty.openpty()
        os.close(m)
        os.close(s)
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _can_pty(), reason="pty not available")


def test_snapshot_carries_a_truecolor_cell_into_the_png(tmp_path: Path) -> None:
    from tests.impty.snapshot import snapshot

    # Paint one cell with an unmistakable truecolor BACKGROUND (a solid block, so
    # no anti-aliasing softens the color the way a glyph stroke would), then idle.
    prog = (
        "import sys, time; "
        "sys.stdout.write('\\x1b[48;2;10;20;200m \\x1b[0m'); "
        "sys.stdout.flush(); time.sleep(30)"
    )
    out = tmp_path / "shot.png"
    with TerminalSession([sys.executable, "-c", prog], cols=20, rows=5) as s:
        s.wait_idle(idle=0.3, hard=8.0)
        path = snapshot(s, str(out))

    assert path == str(out)
    img = Image.open(out).convert("RGB")        # opens iff a valid PNG was written
    colors = {c for _, c in img.getcolors(maxcolors=1 << 24)}
    assert (10, 20, 200) in colors               # the truecolor cell rendered exactly
