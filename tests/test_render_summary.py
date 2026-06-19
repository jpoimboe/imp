from __future__ import annotations

import pytest

from imp.cli.render_summary import StepRow, build_summary, _fmt_compact


def _started(step: str, phase: str, ts: str) -> dict:
    return {"type": "step_started", "timestamp": ts,
            "payload": {"step": step, "phase": phase}}


def _completed(step: str, ts: str) -> dict:
    return {"type": "step_completed", "timestamp": ts,
            "payload": {"step": step, "action": {"verb": "continue"}}}


def _await(ts: str) -> dict:
    return {"type": "awaiting_user_input", "timestamp": ts, "payload": {}}


def test_phase_grouping_in_execution_order():
    rows = build_summary([
        _started("spec-writer", "spec", "2026-01-01T00:00:00+00:00"),
        _completed("spec-writer", "2026-01-01T00:01:30+00:00"),
        _started("plan-writer", "plan", "2026-01-01T00:01:35+00:00"),
        _completed("plan-writer", "2026-01-01T00:04:45+00:00"),
    ])
    assert [(r.phase, r.step) for r in rows] == [
        ("spec", "spec-writer"), ("plan", "plan-writer")]
    assert rows[0].wall_seconds == 90.0
    assert rows[1].wall_seconds == 190.0


def test_loop_bounce_back_yields_second_occurrence():
    rows = build_summary([
        _started("plan-writer", "plan", "2026-01-01T00:00:00+00:00"),
        _completed("plan-writer", "2026-01-01T00:01:00+00:00"),
        _started("plan-review", "plan", "2026-01-01T00:01:00+00:00"),
        _completed("plan-review", "2026-01-01T00:02:00+00:00"),
        _started("plan-writer", "plan", "2026-01-01T00:02:00+00:00"),
        _completed("plan-writer", "2026-01-01T00:02:30+00:00"),
    ])
    assert [r.step for r in rows] == ["plan-writer", "plan-review", "plan-writer"]


def test_resume_reentry_folds_gap_into_one_occurrence():
    rows = build_summary([
        _started("verify", "implement", "2026-01-01T00:00:00+00:00"),
        _await("2026-01-01T00:01:00+00:00"),
        _started("verify", "implement", "2026-01-01T00:06:00+00:00"),  # resume
        _completed("verify", "2026-01-01T00:06:30+00:00"),
    ])
    assert len(rows) == 1
    assert rows[0].wall_seconds == 390.0          # 00:00:00 → 00:06:30
    assert rows[0].wait_seconds == 300.0          # await 00:01:00 → resume 00:06:00


def test_wall_versus_wait_arithmetic():
    rows = build_summary([
        _started("verify", "implement", "2026-01-01T00:00:00+00:00"),
        _await("2026-01-01T00:00:30+00:00"),
        _completed("verify", "2026-01-01T00:02:00+00:00"),
    ])
    assert rows[0].wall_seconds == 120.0          # start → complete
    assert rows[0].wait_seconds == 90.0           # await → next structural event


def test_open_trailing_step_has_no_wall():
    rows = build_summary([
        _started("spec-writer", "spec", "2026-01-01T00:00:00+00:00"),
        _completed("spec-writer", "2026-01-01T00:00:10+00:00"),
        _started("task-lead", "implement", "2026-01-01T00:00:10+00:00"),
    ])
    assert rows[-1].step == "task-lead"
    assert rows[-1].end_ts is None
    assert rows[-1].wall_seconds is None


def test_terminal_error_marks_open_step_errored():
    rows = build_summary([
        _started("task-lead", "implement", "2026-01-01T00:00:00+00:00"),
        {"type": "error", "timestamp": "2026-01-01T00:00:05+00:00",
         "payload": {"message": "boom", "category": "runner"}},
    ])
    assert rows[-1].errored is True


def test_interrupt_config_error_does_not_mark_errored():
    rows = build_summary([
        _started("task-lead", "implement", "2026-01-01T00:00:00+00:00"),
        {"type": "error", "timestamp": "2026-01-01T00:00:05+00:00",
         "payload": {"category": "config", "kind": "interrupted"}},
    ])
    assert rows[-1].errored is False


@pytest.mark.parametrize("seconds, expected", [
    (90, "1m30s"),
    (4, "0m04s"),
    (588, "9m48s"),
    (330, "5m30s"),
    (2342, "39m02s"),
    (0, "0m00s"),
])
def test_fmt_compact(seconds, expected):
    assert _fmt_compact(seconds) == expected
