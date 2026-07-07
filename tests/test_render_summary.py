from __future__ import annotations

import pytest

from imp.cli.narrative import TokenTotals
from imp.cli.render_summary import (
    StepRow,
    SubagentRow,
    SummaryHeader,
    _dur_text,
    _fmt_cost,
    _fmt_tokens,
    _sub_body,
    _tok_total,
    build_summary,
    header_totals,
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
        "feature  ·  abc  ·  merged  ·  39m 2s  ·  —  ·  —",
        "plan",
        "  plan-writer  1m 30s  —  —",
    ]


def test_plain_multi_step_shows_column_padding():
    rows = [
        _row("plan", "plan-writer", 90),
        _row("plan", "plan-review", 120),
        _row("implement", "verify", 30),
    ]
    lines = render_summary(rows, header=_HDR, color=False, width=80)
    assert lines == [
        "feature  ·  abc  ·  merged  ·  39m 2s  ·  —  ·  —",
        "plan",
        "  plan-writer  1m 30s  —  —",
        "  plan-review   2m 0s  —  —",
        "implement",
        "  verify          30s  —  —",
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


def test_no_wait_badge_and_duration_excludes_wait():
    rows = [_row("implement", "verify", 588, wait=330.0)]
    out = render_summary(rows, header=_HDR, color=False, width=80)
    verify = next(ln for ln in out if "verify" in ln)
    assert "⏸" not in verify
    assert "wait" not in verify
    assert "4m 18s" in verify     # 588s wall − 330s wait = 258s work
    assert "9m 48s" not in verify  # NOT the raw 588s wall clock


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


def test_step_row_work_seconds_is_wall_minus_wait():
    row = StepRow(phase="implement", step="verify",
                  start_ts="2026-01-01T00:00:00+00:00",
                  end_ts="2026-01-01T00:02:00+00:00",
                  wait_seconds=90.0)
    assert row.wall_seconds == 120.0
    assert row.work_seconds == 30.0


def test_step_row_work_seconds_floors_at_zero():
    row = StepRow(phase="implement", step="verify",
                  start_ts="2026-01-01T00:00:00+00:00",
                  end_ts="2026-01-01T00:00:10+00:00",
                  wait_seconds=999.0)
    assert row.work_seconds == 0.0


def test_step_row_work_seconds_is_none_while_open():
    row = StepRow(phase="implement", step="verify",
                  start_ts="2026-01-01T00:00:00+00:00", end_ts=None,
                  wait_seconds=30.0)
    assert row.wall_seconds is None
    assert row.work_seconds is None


def test_step_row_new_usage_fields_default_empty():
    row = StepRow(phase="plan", step="plan-writer", start_ts=None)
    assert row.agent_id is None
    assert row.cost == 0.0
    assert row.tokens.input == 0
    assert row.tokens.output == 0
    assert row.subagents == []


def test_subagent_row_wall_seconds_and_defaults():
    sub = SubagentRow(name="explorer",
                      start_ts="2026-01-01T00:00:00+00:00",
                      end_ts="2026-01-01T00:00:45+00:00")
    assert sub.wall_seconds == 45.0
    assert sub.label is None
    assert sub.errored is False
    assert sub.cost == 0.0
    assert sub.tokens.input == 0


def _context(agent_id: str, ts: str) -> dict:
    return {"type": "step_context", "agent_id": agent_id, "timestamp": ts,
            "payload": {}}


def _llm(agent_id: str, ts: str, *, input=0, output=0,
         cache_read=0, cache_creation=0) -> dict:
    return {"type": "llm_response", "agent_id": agent_id, "timestamp": ts,
            "payload": {"usage": {"input_tokens": input, "output_tokens": output,
                                  "cache_read_input_tokens": cache_read,
                                  "cache_creation_input_tokens": cache_creation}}}


def _qr(agent_id: str, ts: str, cost: float) -> dict:
    return {"type": "query_result", "agent_id": agent_id, "timestamp": ts,
            "payload": {"total_cost_usd": cost}}


def _subcall(event_id: str, block_id: str, agent_id: str, ts: str,
             name: str, label: str | None = None) -> dict:
    return {"type": "tool_call", "id": event_id, "agent_id": agent_id, "timestamp": ts,
            "payload": {"name": "sub_agent", "id": block_id, "id_ns": "block",
                        "label": label, "input": {"subagent_name": name}}}


def _subresult(block_id: str, agent_id: str, ts: str, error: bool = False) -> dict:
    return {"type": "tool_result", "agent_id": agent_id, "timestamp": ts,
            "payload": {"id": block_id, "error": error}}


def test_step_own_tokens_summed_from_matching_agent_id():
    rows = build_summary([
        _started("plan-writer", "plan", "2026-01-01T00:00:00+00:00"),
        _llm("a1", "2026-01-01T00:00:05+00:00",
             input=100, output=20, cache_read=5, cache_creation=3),
        _llm("a1", "2026-01-01T00:00:10+00:00", input=10, output=2),
        _completed("plan-writer", "2026-01-01T00:00:20+00:00"),
    ])
    assert rows[0].agent_id == "a1"
    assert rows[0].tokens.input == 110
    assert rows[0].tokens.output == 22
    assert rows[0].tokens.cache_read == 5
    assert rows[0].tokens.cache_creation == 3


def test_step_own_cost_summed_from_matching_agent_id():
    rows = build_summary([
        _started("plan-writer", "plan", "2026-01-01T00:00:00+00:00"),
        _context("a1", "2026-01-01T00:00:01+00:00"),
        _qr("a1", "2026-01-01T00:00:05+00:00", 0.34),
        _qr("a1", "2026-01-01T00:00:10+00:00", 0.06),
        _completed("plan-writer", "2026-01-01T00:00:20+00:00"),
    ])
    assert rows[0].cost == pytest.approx(0.40)


def test_usage_from_other_agent_id_is_not_attributed():
    rows = build_summary([
        _started("plan-writer", "plan", "2026-01-01T00:00:00+00:00"),
        _context("a1", "2026-01-01T00:00:01+00:00"),
        _llm("other", "2026-01-01T00:00:05+00:00", input=999),
        _qr("other", "2026-01-01T00:00:06+00:00", 5.0),
        _completed("plan-writer", "2026-01-01T00:00:20+00:00"),
    ])
    assert rows[0].tokens.input == 0
    assert rows[0].cost == 0.0


def test_step_tokens_accumulate_across_resume_reentry():
    rows = build_summary([
        _started("verify", "implement", "2026-01-01T00:00:00+00:00"),
        _llm("a1", "2026-01-01T00:00:05+00:00", input=50),
        _await("2026-01-01T00:01:00+00:00"),
        _started("verify", "implement", "2026-01-01T00:06:00+00:00"),  # resume
        _llm("a1", "2026-01-01T00:06:05+00:00", input=30),
        _completed("verify", "2026-01-01T00:06:30+00:00"),
    ])
    assert len(rows) == 1
    assert rows[0].agent_id == "a1"
    assert rows[0].tokens.input == 80


def test_sub_prefixed_agent_does_not_establish_step_mapping_or_tokens():
    rows = build_summary([
        _started("task-lead", "implement", "2026-01-01T00:00:00+00:00"),
        _llm("sub:ev1", "2026-01-01T00:00:05+00:00", input=999),
        _llm("a1", "2026-01-01T00:00:10+00:00", input=10),
        _completed("task-lead", "2026-01-01T00:00:20+00:00"),
    ])
    assert rows[0].agent_id == "a1"
    assert rows[0].tokens.input == 10


def test_subagent_captured_with_name_label_and_duration():
    rows = build_summary([
        _started("task-lead", "implement", "2026-01-01T00:00:00+00:00"),
        _context("lead1", "2026-01-01T00:00:01+00:00"),
        _subcall("ev9", "blk9", "lead1", "2026-01-01T00:00:10+00:00",
                 "explorer", label="map the repo"),
        _subresult("blk9", "lead1", "2026-01-01T00:00:55+00:00"),
        _completed("task-lead", "2026-01-01T00:01:00+00:00"),
    ])
    assert len(rows[0].subagents) == 1
    sub = rows[0].subagents[0]
    assert sub.name == "explorer"
    assert sub.label == "map the repo"
    assert sub.wall_seconds == 45.0
    assert sub.errored is False


def test_subagent_tokens_and_cost_keyed_by_sub_agent_id():
    rows = build_summary([
        _started("task-lead", "implement", "2026-01-01T00:00:00+00:00"),
        _context("lead1", "2026-01-01T00:00:01+00:00"),
        _subcall("ev9", "blk9", "lead1", "2026-01-01T00:00:10+00:00", "coder"),
        _llm("sub:ev9", "2026-01-01T00:00:20+00:00", input=200, output=40),
        _qr("sub:ev9", "2026-01-01T00:00:30+00:00", 0.12),
        _subresult("blk9", "lead1", "2026-01-01T00:00:55+00:00"),
        _completed("task-lead", "2026-01-01T00:01:00+00:00"),
    ])
    sub = rows[0].subagents[0]
    assert sub.tokens.input == 200
    assert sub.tokens.output == 40
    assert sub.cost == pytest.approx(0.12)
    # Subagent usage must NOT leak into the parent step's own totals.
    assert rows[0].tokens.input == 0
    assert rows[0].cost == 0.0


def test_subagent_errored_from_tool_result_error_flag():
    rows = build_summary([
        _started("task-lead", "implement", "2026-01-01T00:00:00+00:00"),
        _context("lead1", "2026-01-01T00:00:01+00:00"),
        _subcall("ev9", "blk9", "lead1", "2026-01-01T00:00:10+00:00", "reviewer"),
        _subresult("blk9", "lead1", "2026-01-01T00:00:55+00:00", error=True),
        _completed("task-lead", "2026-01-01T00:01:00+00:00"),
    ])
    assert rows[0].subagents[0].errored is True


def test_parent_and_subagent_tokens_accrue_independently_while_sub_open():
    rows = build_summary([
        _started("task-lead", "implement", "2026-01-01T00:00:00+00:00"),
        _context("lead1", "2026-01-01T00:00:01+00:00"),
        _llm("lead1", "2026-01-01T00:00:05+00:00", input=70),
        _subcall("ev9", "blk9", "lead1", "2026-01-01T00:00:10+00:00", "coder"),
        _llm("sub:ev9", "2026-01-01T00:00:20+00:00", input=200),
        _llm("lead1", "2026-01-01T00:00:25+00:00", input=30),
        _subresult("blk9", "lead1", "2026-01-01T00:00:55+00:00"),
        _completed("task-lead", "2026-01-01T00:01:00+00:00"),
    ])
    assert rows[0].tokens.input == 100
    assert rows[0].subagents[0].tokens.input == 200


@pytest.mark.parametrize("n, expected", [
    (-5, "—"),
    (0, "—"),
    (5, "5"),
    (999, "999"),
    (1000, "1.0k"),
    (12345, "12.3k"),
    (999_949, "999.9k"),
    (999_950, "1.0M"),
    (999_999, "1.0M"),
    (1_200_000, "1.2M"),
])
def test_fmt_tokens(n, expected):
    assert _fmt_tokens(n) == expected


@pytest.mark.parametrize("c, expected", [
    (-1.0, "—"),
    (0.0, "—"),
    (0.004, "<$0.01"),
    (0.01, "$0.01"),
    (0.34, "$0.34"),
    (12.5, "$12.50"),
])
def test_fmt_cost(c, expected):
    assert _fmt_cost(c) == expected


def test_tok_total_sums_all_four_buckets():
    t = TokenTotals(input=1, output=2, cache_read=3, cache_creation=4)
    assert _tok_total(t) == 10


def test_dur_text_running_when_none_else_compact():
    assert _dur_text(None) == "running"
    assert _dur_text(90.0) == "1m 30s"


def test_sub_body_with_and_without_label():
    assert _sub_body(SubagentRow(name="explorer")) == "↳ explorer"
    assert _sub_body(SubagentRow(name="explorer", label="map repo")) \
        == "↳ explorer (map repo)"


def test_header_totals_subtracts_wait_and_rolls_up_steps_and_subs():
    rows = [
        StepRow(phase="implement", step="task-lead",
                start_ts="2026-01-01T00:00:00+00:00",
                end_ts="2026-01-01T00:02:00+00:00",
                wait_seconds=90.0,
                tokens=TokenTotals(input=1000), cost=0.20,
                subagents=[SubagentRow(name="coder",
                                       tokens=TokenTotals(input=500),
                                       cost=0.05)]),
    ]
    work, tokens, cost = header_totals(rows, 120.0)
    assert work == 30.0                 # 120s total - 90s wait
    assert tokens == 1500               # 1000 step + 500 subagent
    assert cost == pytest.approx(0.25)  # 0.20 step + 0.05 subagent


def test_header_totals_floors_work_at_zero():
    rows = [StepRow(phase="p", step="s",
                    start_ts="2026-01-01T00:00:00+00:00",
                    end_ts="2026-01-01T00:00:10+00:00", wait_seconds=999.0)]
    work, _, _ = header_totals(rows, 10.0)
    assert work == 0.0


def test_header_totals_none_total_gives_none_work():
    work, tokens, cost = header_totals([], None)
    assert work is None
    assert tokens == 0
    assert cost == 0.0


def test_step_row_shows_token_and_cost_columns():
    row = StepRow(phase="plan", step="plan-writer",
                  start_ts="2026-01-01T00:00:00+00:00",
                  end_ts="2026-01-01T00:01:30+00:00",
                  tokens=TokenTotals(input=12000, output=345), cost=0.34)
    out = render_summary([row], header=_HDR, color=False, width=80)
    line = next(ln for ln in out if "plan-writer" in ln)
    assert line == "  plan-writer  1m 30s  12.3k  $0.34"


def test_cost_column_dash_fallback_when_zero():
    row = StepRow(phase="plan", step="s",
                  start_ts="2026-01-01T00:00:00+00:00",
                  end_ts="2026-01-01T00:00:10+00:00",
                  tokens=TokenTotals(input=5), cost=0.0)
    out = render_summary([row], header=_HDR, color=False, width=80)
    line = next(ln for ln in out if "10s" in ln)
    assert line == "  s  10s  5  —"


def test_duration_renders_in_step_hue():
    rows = [_row("plan", "plan-writer", 90)]
    out = render_summary(rows, header=_HDR, color=True, width=80)
    line = next(ln for ln in out if "plan-writer" in ln)
    assert "\x1b[34m1m 30s\x1b[0m" in line   # first hue: step-blue on the duration


def test_errored_step_duration_follows_error_color():
    rows = [_row("implement", "task-lead", 10, errored=True)]
    out = render_summary(rows, header=_HDR, color=True, width=80)
    line = next(ln for ln in out if "task-lead" in ln)
    assert "\x1b[1m\x1b[31m10s\x1b[0m" in line


def test_header_total_is_work_time_excluding_wait():
    rows = [StepRow(phase="implement", step="verify",
                    start_ts="2026-01-01T00:00:00+00:00",
                    end_ts="2026-01-01T00:02:00+00:00", wait_seconds=90.0)]
    hdr = SummaryHeader(workflow="f", change_id="c", status="merged",
                        total_seconds=120.0)
    out = render_summary(rows, header=hdr, color=False, width=80)
    assert "30s" in out[0]     # 120s total − 90s wait = 30s work
    assert "2m 0s" not in out[0]


def test_header_rolls_up_step_tokens_and_cost():
    rows = [
        StepRow(phase="plan", step="a",
                start_ts="2026-01-01T00:00:00+00:00",
                end_ts="2026-01-01T00:00:30+00:00",
                tokens=TokenTotals(input=1000), cost=0.20),
        StepRow(phase="plan", step="b",
                start_ts="2026-01-01T00:00:30+00:00",
                end_ts="2026-01-01T00:01:00+00:00",
                tokens=TokenTotals(input=1500), cost=0.30),
    ]
    hdr = SummaryHeader(workflow="feature", change_id="abc", status="merged",
                        total_seconds=60.0)
    out = render_summary(rows, header=hdr, color=False, width=80)
    assert "2.5k" in out[0]    # 1000 + 1500 tokens rolled up
    assert "$0.50" in out[0]   # 0.20 + 0.30 cost rolled up


def test_subagent_row_indented_with_own_figures():
    step = StepRow(phase="implement", step="task-lead",
                   start_ts="2026-01-01T00:00:00+00:00",
                   end_ts="2026-01-01T00:01:00+00:00",
                   tokens=TokenTotals(input=100), cost=0.10,
                   subagents=[SubagentRow(
                       name="explorer",
                       start_ts="2026-01-01T00:00:05+00:00",
                       end_ts="2026-01-01T00:00:50+00:00",
                       tokens=TokenTotals(input=200), cost=0.05)])
    out = render_summary([step], header=_HDR, color=False, width=80)
    step_line = next(ln for ln in out if "task-lead" in ln)
    sub_line = next(ln for ln in out if "explorer" in ln)
    assert step_line == "  task-lead     1m 0s  100  $0.10"
    assert sub_line == "    ↳ explorer    45s  200  $0.05"


def test_subagent_label_shown_in_parens():
    step = StepRow(phase="implement", step="task-lead",
                   start_ts="2026-01-01T00:00:00+00:00",
                   end_ts="2026-01-01T00:01:00+00:00",
                   subagents=[SubagentRow(
                       name="explorer", label="map repo",
                       start_ts="2026-01-01T00:00:05+00:00",
                       end_ts="2026-01-01T00:00:50+00:00")])
    out = render_summary([step], header=_HDR, color=False, width=80)
    sub_line = next(ln for ln in out if "explorer" in ln)
    assert "↳ explorer (map repo)" in sub_line


def test_subagent_duration_follows_subagent_color():
    step = StepRow(phase="implement", step="task-lead",
                   start_ts="2026-01-01T00:00:00+00:00",
                   end_ts="2026-01-01T00:01:00+00:00",
                   subagents=[SubagentRow(
                       name="explorer",
                       start_ts="2026-01-01T00:00:05+00:00",
                       end_ts="2026-01-01T00:00:50+00:00")])
    out = render_summary([step], header=_HDR, color=True, width=80)
    sub_line = next(ln for ln in out if "explorer" in ln)
    assert "\x1b[35m  45s\x1b[0m" in sub_line   # magenta subagent duration


def test_errored_subagent_duration_follows_error_color():
    step = StepRow(phase="implement", step="task-lead",
                   start_ts="2026-01-01T00:00:00+00:00",
                   end_ts="2026-01-01T00:01:00+00:00",
                   subagents=[SubagentRow(
                       name="coder", errored=True,
                       start_ts="2026-01-01T00:00:05+00:00",
                       end_ts="2026-01-01T00:00:50+00:00")])
    out = render_summary([step], header=_HDR, color=True, width=80)
    sub_line = next(ln for ln in out if "coder" in ln)
    assert "\x1b[1m\x1b[31m  45s\x1b[0m" in sub_line


def test_header_rollup_includes_subagent_tokens_and_cost():
    step = StepRow(phase="implement", step="task-lead",
                   start_ts="2026-01-01T00:00:00+00:00",
                   end_ts="2026-01-01T00:00:30+00:00",
                   tokens=TokenTotals(input=1000), cost=0.20,
                   subagents=[SubagentRow(
                       name="coder",
                       start_ts="2026-01-01T00:00:05+00:00",
                       end_ts="2026-01-01T00:00:25+00:00",
                       tokens=TokenTotals(input=1500), cost=0.30)])
    hdr = SummaryHeader(workflow="f", change_id="c", status="merged",
                        total_seconds=30.0)
    out = render_summary([step], header=hdr, color=False, width=80)
    assert "2.5k" in out[0]    # 1000 step + 1500 subagent
    assert "$0.50" in out[0]   # 0.20 step + 0.30 subagent
