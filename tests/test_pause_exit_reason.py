from __future__ import annotations

from imp.cli.main import _exit_pause_reason


def test_engine_reason_wins_over_gesture() -> None:
    # The run already paused with reason "ctrl-c"; a Ctrl-D exit must not
    # contradict it with a second, different reason.
    assert _exit_pause_reason("ctrl-c", "ctrl-d") == "ctrl-c"
    assert _exit_pause_reason("esc", "ctrl-d") == "esc"


def test_gesture_used_when_no_engine_reason() -> None:
    assert _exit_pause_reason(None, "ctrl-d") == "ctrl-d"
