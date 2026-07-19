from __future__ import annotations

from imp.engine.state import classify_pause_halt


def test_clean_yield_reasons_are_none() -> None:
    assert classify_pause_halt("awaiting_user", None) is None
    assert classify_pause_halt("awaiting_turn", None) is None
    assert classify_pause_halt("interrupt", None) is None


def test_failure_reasons_are_error() -> None:
    for reason in ("internal_error", "unknown_step", "max_loops",
                   "retry_ceiling", "reauth_needed", "clean_tree",
                   "context_window", "auth", "rate_limit", "", "anything_else"):
        assert classify_pause_halt(reason, None) == "error"


def test_interrupt_reason_wins_over_engine_reason() -> None:
    # A human interrupt (ctrl-c/ctrl-d/sigterm) parked over an error reason still
    # classifies clean — mirrors the display path's interrupt_reason precedence.
    assert classify_pause_halt("internal_error", "ctrl-c") is None
    assert classify_pause_halt("api", "sigterm") is None
