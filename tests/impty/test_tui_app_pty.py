from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

from impty import Regime, TerminalSession
from tests.impty.e2e_support import can_pty

_HARNESS = str(Path(__file__).with_name("tui_harness.py"))

pytestmark = pytest.mark.skipif(not can_pty(), reason="pty not available")

# The app repaints continuously (status spinner), so wait_idle never settles on
# a quiet window — a blind wait always burns its whole cap and, under the full
# suite's -n auto CPU load, a fixed cap can expire before a starved child has
# rendered the reaction. The background drainer feeds pyte live, so instead we
# poll the rendered screen for the reaction we expect: fast when it lands, and
# tolerant of load up to a generous ceiling.
_HARD = 20.0


def _session(scenario: str, result: str = "") -> TerminalSession:
    return TerminalSession([sys.executable, _HARNESS, scenario, result],
                           cols=80, rows=24)


def _await(s: TerminalSession, ready, *, what: str, hard: float = _HARD) -> str:
    deadline = time.monotonic() + hard
    while True:
        win = "\n".join(s.lines(limit=s.rows))
        if ready(win):
            return win
        if time.monotonic() > deadline:
            raise AssertionError(f"{what} not reached within {hard}s; last screen:\n{win}")
        time.sleep(0.05)


def _await_cursor_bottom(s: TerminalSession, *, hard: float = _HARD) -> None:
    deadline = time.monotonic() + hard
    while True:
        _, y = s.cursor()
        if y == s.rows - 1:
            return
        if time.monotonic() > deadline:
            raise AssertionError(f"prompt cursor never reached the bottom row within {hard}s")
        time.sleep(0.05)


def _wait_alt_screen(s: TerminalSession, hard: float = _HARD) -> None:
    deadline = time.monotonic() + hard
    while time.monotonic() < deadline:
        if s.regime() is Regime.SCREEN:
            return
        time.sleep(0.02)
    raise AssertionError("alt-screen never entered")


def test_expanded_render_and_pageup_scroll_and_ctrl_o_toggle() -> None:
    with _session("view") as s:
        _await(s, lambda w: s.is_alt_screen(), what="alt-screen")   # criterion 2/8

        s.send("\x0f")                                     # Ctrl-O -> full mode (lean is default)
        win = _await(s, lambda w: "command=echo" in w, what="expanded render")
        assert "echo 0" not in win                        # tail-pinned: earliest off-screen

        s.send("\x1b[5~")                                 # PageUp
        _await(s, lambda w: "echo 0" in w, what="scroll reveals earliest")   # criterion 3

        s.send("\x0f")                                     # Ctrl-O -> lean
        # lean: no expanded tool lines, burst summary present (criterion 4)
        _await(s, lambda w: "command=echo" not in w and "call" in w, what="lean toggle")


def test_exit_restores_screen_and_prints_collapsed_transcript() -> None:
    with _session("exit") as s:
        _wait_alt_screen(s)
        assert s.regime() is Regime.SCREEN                 # in alt-screen while running
        s.send("q")                                        # gated exit (race-free vs a timer)
        s.run_to_completion(hard=_HARD)                    # past app.exit() + transcript print
        assert s.regime() is Regime.SCROLL                 # alt screen restored (criterion 8)
        body = "\n".join(s.lines())
        assert "spec · complete" in body                     # collapsed transcript in scrollback
        assert "command=echo" not in body         # transcript is lean (full=False) (criterion 8)


def test_in_app_prompt_accepts_line_with_slash_handling_and_completion(tmp_path) -> None:
    result = tmp_path / "reply.txt"
    with _session("prompt", str(result)) as s:
        _await_cursor_bottom(s)                        # prompt buffer at the bottom (criterion 6)

        s.send("/help\r")                                  # slash handling against the buffer
        _await(s, lambda w: "slash command" in w.lower(), what="/help output")

        s.send("/sk\t")                                    # completion: /sk -> /skills (builtin)
        _await(s, lambda w: "skills" in w, what="/skills completion")

        s.send("\x15")                                     # Ctrl-U: clear the line
        s.send("go ahead\r")                               # plain line resolves the solicit
        s.run_to_completion(hard=_HARD)
        assert result.read_text() == "go ahead"       # the typed reply was returned (criterion 6)


def test_scroll_and_ctrl_o_reach_viewport_during_active_solicit(tmp_path) -> None:
    # Finding 4: while a wait_for_user solicit owns the prompt buffer, the
    # app-level navigation keys must still drive the viewport (scroll to read
    # context / toggle verbosity while being asked a question), and the navigation
    # bytes must NOT leak into the answer. Locks in that precedence — the keys are
    # bound eager so the embedded prompt buffer can never shadow them.
    result = tmp_path / "reply.txt"
    with _session("promptscroll", str(result)) as s:
        _await_cursor_bottom(s)                        # solicit prompt active at the bottom

        s.send("\x0f")                                     # Ctrl-O -> full mode *during solicit*
        win = _await(s, lambda w: "command=echo" in w, what="expanded render during solicit")
        assert "echo 0" not in win                    # earliest is off-screen

        s.send("\x1b[5~")                                  # PageUp *during the solicit*
        _await(s, lambda w: "echo 0" in w, what="scroll during solicit")   # scrolled, not shadowed

        s.send("\x0f")                                     # Ctrl-O -> lean *during the solicit*
        _await(s, lambda w: "command=echo" not in w and "call" in w,
               what="lean toggle during solicit")          # toggled, not open-line

        s.send("go ahead\r")                               # the buffer is still clean: resolves
        s.run_to_completion(hard=_HARD)
        assert result.read_text() == "go ahead"       # no stray nav bytes leaked into the answer
