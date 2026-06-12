from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

from impty import Regime, TerminalSession
from tests.impty.e2e_support import can_pty

_HARNESS = str(Path(__file__).with_name("tui_harness.py"))

pytestmark = pytest.mark.skipif(not can_pty(), reason="pty not available")

# The dark theme's bold structural accents (palette._DARK_RGB): phase "blue",
# step "step", workflow "magenta". The app paints the phase + step headers bold
# in these truecolor RGBs under the dark theme.
_DARK_ACCENTS = {(0x78, 0x98, 0xBE), (0x70, 0x90, 0xB0), (0xA8, 0x84, 0xB5)}


def test_dark_theme_accent_survives_into_the_png(tmp_path: Path) -> None:
    from PIL import Image

    from tests.impty.snapshot import snapshot

    out = tmp_path / "theme.png"
    env = {**os.environ, "IMP_THEME": "dark", "PROMPT_TOOLKIT_COLOR_DEPTH": "DEPTH_24_BIT"}
    with TerminalSession([sys.executable, _HARNESS, "theme", "/dev/null"],
                         cols=80, rows=24, env=env) as s:
        deadline = time.monotonic() + 12.0
        while time.monotonic() < deadline and s.regime() is not Regime.SCREEN:
            time.sleep(0.02)
        assert s.is_alt_screen() is True, "alt screen not reached within deadline"
        s.wait_idle(idle=0.3, hard=8.0)
        path = snapshot(s, str(out))

    assert path == str(out)
    img = Image.open(out).convert("RGB")
    colors = {c for _, c in img.getcolors(maxcolors=1 << 24)}
    # Bold glyph cores render at the exact fill color; at least one dark-theme
    # truecolor accent is present iff the app painted the dark theme.
    assert _DARK_ACCENTS & colors, f"no dark-theme accent in PNG; got {sorted(colors)[:20]}"
