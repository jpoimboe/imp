from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

from impty import Regime, TerminalSession
from tests.impty.e2e_support import can_pty

_HARNESS = str(Path(__file__).with_name("tui_harness.py"))

pytestmark = pytest.mark.skipif(not can_pty(), reason="pty not available")


def _session(scenario: str, result: str = "") -> TerminalSession:
    return TerminalSession([sys.executable, _HARNESS, scenario, result],
                           cols=80, rows=24)


def _wait_alt_screen(s: TerminalSession, hard: float = 8.0) -> None:
    # The run's status spinner repaints every 0.1s, so wait_idle never settles;
    # poll the (live) regime until the app has entered the alternate screen.
    deadline = time.monotonic() + hard
    while time.monotonic() < deadline:
        if s.regime() is Regime.SCREEN:
            return
        time.sleep(0.02)
    raise AssertionError("alt-screen never entered")


def test_expanded_render_and_pageup_scroll_and_ctrl_o_toggle() -> None:
    with _session("view") as s:
        s.wait_idle(idle=0.3, hard=8.0)
        assert s.is_alt_screen() is True                 # alternate screen (criterion 2/8)

        since = time.monotonic()
        s.send("\x0f")                                     # Ctrl-O -> full mode (lean is default)
        s.wait_idle(idle=0.3, hard=5.0, since=since)
        win = "\n".join(s.lines(limit=s.rows))
        assert "command=echo" in win                     # expanded (full=True) (criterion 2)
        assert "echo 0" not in win                        # tail-pinned: earliest off-screen

        since = time.monotonic()
        s.send("\x1b[5~")                                 # PageUp
        s.wait_idle(idle=0.3, hard=5.0, since=since)
        win_up = "\n".join(s.lines(limit=s.rows))
        assert "echo 0" in win_up                    # scrolled up reveals earliest (criterion 3)

        since = time.monotonic()
        s.send("\x0f")                                     # Ctrl-O -> lean
        s.wait_idle(idle=0.3, hard=5.0, since=since)
        win_lean = "\n".join(s.lines(limit=s.rows))
        assert "command=echo" not in win_lean    # no expanded tool lines (criterion 4)
        assert "call" in win_lean                          # lean burst summary present


def test_exit_restores_screen_and_prints_collapsed_transcript() -> None:
    with _session("exit") as s:
        _wait_alt_screen(s)
        assert s.regime() is Regime.SCREEN                 # in alt-screen while running
        s.send("q")                                        # gated exit (race-free vs a timer)
        s.run_to_completion(hard=8.0)                      # past app.exit() + transcript print
        assert s.regime() is Regime.SCROLL                 # alt screen restored (criterion 8)
        body = "\n".join(s.lines())
        assert "spec complete" in body                     # collapsed transcript in scrollback
        assert "command=echo" not in body         # transcript is lean (full=False) (criterion 8)


def test_in_app_prompt_accepts_line_with_slash_handling_and_completion(tmp_path) -> None:
    result = tmp_path / "reply.txt"
    with _session("prompt", str(result)) as s:
        s.wait_idle(idle=0.3, hard=8.0)
        _, y = s.cursor()
        assert y == s.rows - 1                        # prompt buffer at the bottom (criterion 6)

        since = time.monotonic()
        s.send("/help\r")                                  # slash handling against the buffer
        s.wait_idle(idle=0.3, hard=5.0, since=since)
        assert "slash command" in "\n".join(s.lines(limit=s.rows)).lower()

        since = time.monotonic()
        s.send("/sk\t")                                    # completion: /sk -> /skills (builtin)
        s.wait_idle(idle=0.3, hard=5.0, since=since)
        assert "skills" in "\n".join(s.lines(limit=s.rows))

        s.send("\x15")                                     # Ctrl-U: clear the line
        since = time.monotonic()
        s.send("go ahead\r")                               # plain line resolves the solicit
        s.run_to_completion(hard=8.0)
        assert result.read_text() == "go ahead"       # the typed reply was returned (criterion 6)


def test_scroll_and_ctrl_o_reach_viewport_during_active_solicit(tmp_path) -> None:
    # Finding 4: while a wait_for_user solicit owns the prompt buffer, the
    # app-level navigation keys must still drive the viewport (scroll to read
    # context / toggle verbosity while being asked a question), and the navigation
    # bytes must NOT leak into the answer. Locks in that precedence — the keys are
    # bound eager so the embedded prompt buffer can never shadow them.
    result = tmp_path / "reply.txt"
    with _session("promptscroll", str(result)) as s:
        s.wait_idle(idle=0.3, hard=8.0)
        _, y = s.cursor()
        assert y == s.rows - 1                        # solicit prompt active at the bottom

        since = time.monotonic()
        s.send("\x0f")                                     # Ctrl-O -> full mode *during solicit*
        s.wait_idle(idle=0.3, hard=5.0, since=since)
        win = "\n".join(s.lines(limit=s.rows))
        assert "command=echo" in win                  # expanded + tail-pinned
        assert "echo 0" not in win                    # earliest is off-screen

        since = time.monotonic()
        s.send("\x1b[5~")                                  # PageUp *during the solicit*
        s.wait_idle(idle=0.3, hard=5.0, since=since)
        assert "echo 0" in "\n".join(s.lines(limit=s.rows))   # scrolled up, not shadowed

        since = time.monotonic()
        s.send("\x0f")                                     # Ctrl-O -> lean *during the solicit*
        s.wait_idle(idle=0.3, hard=5.0, since=since)
        assert "command=echo" not in "\n".join(s.lines(limit=s.rows))  # toggled, not open-line

        since = time.monotonic()
        s.send("go ahead\r")                               # the buffer is still clean: resolves
        s.run_to_completion(hard=8.0)
        assert result.read_text() == "go ahead"       # no stray nav bytes leaked into the answer
