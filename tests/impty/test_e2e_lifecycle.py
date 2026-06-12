from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

from impty import Regime, TerminalSession
from tests.impty.matchers import assert_screen

_HARNESS = str(Path(__file__).with_name("tui_harness.py"))


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


def _wait_for(s: TerminalSession, needle: str, hard: float = 60.0) -> None:
    deadline = time.monotonic() + hard
    while time.monotonic() < deadline:
        if needle in "\n".join(s.lines(limit=s.rows)):
            return
        time.sleep(0.02)
    raise AssertionError(f"{needle!r} never appeared:\n" + "\n".join(s.lines(limit=s.rows)))


def test_tui_lifecycle_renders_real_engine_tool_calls() -> None:
    with TerminalSession([sys.executable, _HARNESS, "run", "/dev/null"],
                         cols=80, rows=24) as s:
        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline and s.regime() is not Regime.SCREEN:
            time.sleep(0.02)
        assert s.is_alt_screen() is True, "alt-screen never entered"

        # The engine emits all tool_call events in one batch (scripted provider),
        # then executes the 40 bash subprocesses sequentially (~20s total).
        # Wait for the last call to appear in the viewport, then wait for the
        # run to finish (spinner stops when step_completed is received).
        _wait_for(s, "command=echo 39")
        assert_screen(s, contains=("command=echo",), excludes=("echo 0",),
                      limit=s.rows)

        # PageUp until echo 0 is visible. The real engine renders 2 lines per call
        # (command + JSON output) and the buffer also contains the full agent system
        # prompt, so the exact press count varies. Stop as soon as echo 0 appears
        # (pressing past it would over-scroll into the system-prompt region).
        for _ in range(20):
            since = time.monotonic()
            s.send("\x1b[5~")
            s.wait_idle(idle=0.3, hard=2.0, since=since)
            if "echo 0" in "\n".join(s.lines(limit=s.rows)):
                break
        assert_screen(s, contains=("echo 0",), limit=s.rows)

        # Ctrl-O toggles lean view, collapsing expanded tool-call lines.
        since = time.monotonic()
        s.send("\x0f")
        s.wait_idle(idle=0.3, hard=5.0, since=since)
        assert_screen(s, excludes=("command=echo",), contains=("call",),
                      limit=s.rows)
