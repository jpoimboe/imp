from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from impty.screen import TerminalScreen
from impty.stream import TerminalStream

# Bytes tmux 3.7c wrote to its 80x24 outer tty; provenance and per-step
# capture-pane grids are in the .meta.json. Static data: regenerating depends on
# the tmux version, read timing, and impty's own replies.
_FIXTURE_DIR = Path(__file__).with_name("fixtures") / "tmux"
_RAW = _FIXTURE_DIR / "tmux-3.7c-su-sd-dl.raw"
_META = _FIXTURE_DIR / "tmux-3.7c-su-sd-dl.meta.json"


def test_tmux_replay_matches_capture_pane_at_every_step() -> None:
    meta: dict[str, Any] = json.loads(_META.read_text())
    raw = _RAW.read_bytes()
    assert len(raw) == meta["bytes"] == meta["steps"][-1]["offset"]
    cols, rows = meta["size"]
    screen = TerminalScreen(cols, rows, history=2000, ratio=0.5)
    stream = TerminalStream(screen)

    got: list[tuple[str, list[str], list[int]]] = []
    want: list[tuple[str, list[str], list[int]]] = []
    offset = 0
    for step in meta["steps"]:
        stream.feed(raw[offset : step["offset"]])
        offset = step["offset"]
        rows_now = [line.rstrip() for line in screen.display]
        got.append((step["label"], rows_now, [screen.cursor.x, screen.cursor.y]))
        want.append((step["label"], step["expected"], step["cursor_xy"]))
    # One comparison over every step: SD is only observable at the second step.
    assert got == want
