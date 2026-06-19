from __future__ import annotations

import pytest

from imp.cli.render_summary import (
    StepRow,
    SummaryHeader,
    _fmt_compact,
    build_summary,
    render_summary,
    summary_total,
)


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


def _row(phase, step, secs, *, wait=0.0, errored=False, open_=False):
    start = "2026-01-01T00:00:00+00:00"
    end = None if open_ else f"2026-01-01T00:{secs // 60:02d}:{secs % 60:02d}+00:00"
    return StepRow(phase=phase, step=step, start_ts=start, end_ts=end,
                   wait_seconds=wait, errored=errored)


_HDR = SummaryHeader(workflow="feature", change_id="abc",
                     status="merged", total_seconds=2342.0)


def test_plain_output_has_no_sgr_and_expected_lines():
    rows = [_row("plan", "plan-writer", 90)]
    lines = render_summary(rows, header=_HDR, color=False, width=80)
    assert lines == [
        "feature  ·  abc  ·  merged  ·  39m02s",
        "plan",
        "  plan-writer  1m30s",
    ]


def test_plain_multi_step_shows_column_padding():
    rows = [
        _row("plan", "plan-writer", 90),
        _row("plan", "plan-review", 120),
        _row("implement", "verify", 30),
    ]
    lines = render_summary(rows, header=_HDR, color=False, width=80)
    assert lines == [
        "feature  ·  abc  ·  merged  ·  39m02s",
        "plan",
        "  plan-writer  1m30s",
        "  plan-review  2m00s",
        "implement",
        "  verify       0m30s",
    ]


def test_phase_hue_cycle_and_reset_at_phase_boundary():
    rows = [
        _row("plan", "plan-writer", 10),
        _row("plan", "plan-review", 20),
        _row("plan", "plan-review", 30),
        _row("implement", "task-lead", 40),
        _row("implement", "verify", 50),
    ]
    out = render_summary(rows, header=_HDR, color=True, width=80)

    def line(name: str) -> str:
        return next(ln for ln in out if name in ln)

    assert "\x1b[34m" in line("plan-writer")          # 1st hue: step-blue
    assert "\x1b[35m" in line("plan-review")          # 2nd hue: magenta
    assert "\x1b[34m" in line("task-lead")            # rotation RESET at new phase
    assert "\x1b[35m" in line("verify")               # 2nd hue again post-reset


def test_repeat_reuses_its_phase_scoped_hue():
    rows = [_row("plan", "plan-review", 20), _row("plan", "plan-review", 30)]
    out = render_summary(rows, header=_HDR, color=True, width=80)
    review_lines = [ln for ln in out if "plan-review" in ln]
    assert len(review_lines) == 2
    assert all("\x1b[34m" in ln for ln in review_lines)  # both step-blue (1st hue when alone)


def test_errored_step_renders_bold_red_overriding_hue():
    rows = [_row("implement", "task-lead", 10, errored=True)]
    out = render_summary(rows, header=_HDR, color=True, width=80)
    assert "\x1b[1m\x1b[31mtask-lead" in next(ln for ln in out if "task-lead" in ln)


def test_wait_annotation_only_at_or_above_threshold():
    rows = [_row("implement", "verify", 588, wait=330.0),
            _row("implement", "task-lead", 60, wait=4.0)]
    out = render_summary(rows, header=_HDR, color=False, width=80)
    verify = next(ln for ln in out if "verify" in ln)
    task = next(ln for ln in out if "task-lead" in ln)
    assert "⏸ 5m30s wait" in verify
    assert "wait" not in task                          # sub-5s wait is omitted


def test_open_trailing_step_renders_running():
    rows = [_row("implement", "task-lead", 0, open_=True)]
    out = render_summary(rows, header=_HDR, color=False, width=80)
    assert "  task-lead  running" in out[-1]


def test_header_status_color_and_workflow_magenta():
    out = render_summary([], header=_HDR, color=True, width=80)
    assert "\x1b[1m\x1b[35mfeature\x1b[0m" in out[0]   # workflow bold magenta
    assert "\x1b[32mmerged\x1b[0m" in out[0]           # merged status green


def test_summary_total_is_first_to_last_timestamp():
    events = [{"timestamp": "2026-01-01T00:00:00+00:00"},
              {"timestamp": "2026-01-01T00:39:02+00:00"}]
    assert summary_total(events) == 2342.0
    assert summary_total([]) is None
