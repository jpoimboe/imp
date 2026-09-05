from __future__ import annotations

import pytest

from imp.cli.accounting import RowUsage
from imp.cli.narrative import elapsed_seconds
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
    open_span_label,
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


def test_audit_error_does_not_mark_errored():
    rows = build_summary([
        _started("task-lead", "implement", "2026-01-01T00:00:00+00:00"),
        {"type": "error", "timestamp": "2026-01-01T00:00:05+00:00",
         "payload": {"audit": True, "category": "terminated", "kind": "budget_exhausted"}},
    ])
    assert rows[-1].errored is False


def _row(phase, step, secs, *, wait=0.0, errored=False, open_=False):
    start = "2026-01-01T00:00:00+00:00"
    end = None if open_ else f"2026-01-01T00:{secs // 60:02d}:{secs % 60:02d}+00:00"
    return StepRow(phase=phase, step=step, start_ts=start, end_ts=end,
                   wait_seconds=wait, errored=errored)


_HDR = SummaryHeader(workflow="feature", quest_id="abc",
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
    assert "4m 18s" in verify     # 588s wall - 330s wait = 258s work
    assert "9m 48s" not in verify  # NOT the raw 588s wall clock


def test_open_trailing_step_renders_running():
    hdr = SummaryHeader(workflow="feature", quest_id="abc",
                        status="running", total_seconds=None)
    rows = [_row("implement", "task-lead", 0, open_=True)]
    out = render_summary(rows, header=hdr, color=False, width=80)
    assert "  task-lead  running" in out[-1]


def test_open_trailing_step_on_paused_change_reflects_status():
    hdr = SummaryHeader(workflow="feature", quest_id="abc",
                        status="paused", total_seconds=100.0)
    rows = [_row("implement", "task-lead", 0, open_=True)]
    out = render_summary(rows, header=hdr, color=False, width=80)
    assert "  task-lead  paused" in out[-1]
    assert "running" not in out[-1]


def test_header_status_color_and_workflow_magenta():
    out = render_summary([], header=_HDR, color=True, width=80)
    assert "\x1b[1m\x1b[35mfeature\x1b[0m" in out[0]   # workflow bold magenta
    assert "merged" in out[0]                          # merged status plain (no color)


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
    assert row.usage == RowUsage()
    assert row.subagents == []


def test_subagent_row_wall_seconds_and_defaults():
    sub = SubagentRow(name="explorer",
                      start_ts="2026-01-01T00:00:00+00:00",
                      end_ts="2026-01-01T00:00:45+00:00")
    assert sub.wall_seconds == 45.0
    assert sub.label is None
    assert sub.errored is False
    assert sub.usage == RowUsage()


def _context(agent_id: str, ts: str) -> dict:
    return {"type": "step_context", "agent_id": agent_id, "timestamp": ts,
            "payload": {}}


def _llm(agent_id: str, ts: str, *, input=0, output=0,
         cache_read=0, cache_creation=0, provider="anthropic",
         model="claude-sonnet-4-6") -> dict:
    return {"type": "agent_message", "agent_id": agent_id, "timestamp": ts,
            "payload": {"provider": provider, "model": model,
                        "usage": {"input_tokens": input,
                                  "output_tokens": output,
                                  "cache_read_input_tokens": cache_read,
                                  "cache_creation_input_tokens": cache_creation}}}


def _codex(agent_id: str, ts: str, *, model="gpt-5.6-sol",
           auth_mode=None) -> dict:
    """A codex agent_message carries `usage={}` on the wire
    (`codex_runner.py:1057`, `:1283`) — the real figures arrive on the
    usage-patch row that follows."""
    payload = {"provider": "codex", "model": model, "usage": {}}
    if auth_mode is not None:
        payload["auth_mode"] = auth_mode
    return {"type": "agent_message", "agent_id": agent_id, "timestamp": ts,
            "payload": payload}


def _cc(agent_id: str, ts: str, *, auth_mode=None) -> dict:
    """A claude_code agent_message: ignored by the accounting path entirely —
    its usage is the unreliable start-of-message snapshot."""
    return {"type": "agent_message", "agent_id": agent_id, "timestamp": ts,
            "payload": {"provider": "claude_code", "model": "claude-sonnet-4-6",
                        "usage": {"input_tokens": 9999, "output_tokens": 8888},
                        **({"auth_mode": auth_mode} if auth_mode else {})}}


def _qr(agent_id: str, ts: str, cost: float, *, session: str = "s1",
        model_usage: dict | None = None) -> dict:
    return {"type": "query_result", "agent_id": agent_id, "timestamp": ts,
            "payload": {"session_id": session, "total_cost_usd": cost,
                        "usage": {},
                        "model_usage": model_usage if model_usage is not None
                        else {}}}


def _patch(agent_id: str, *, input=0, output=0, cache_read=0,
           cache_creation=0, context_tokens=None) -> dict:
    payload = {"usage": {"input_tokens": input, "output_tokens": output,
                         "cache_read_input_tokens": cache_read,
                         "cache_creation_input_tokens": cache_creation}}
    if context_tokens is not None:
        payload["context_tokens"] = context_tokens
    return {"type": "agent_message_usage_patch", "agent_id": agent_id,
            "payload": payload}


def _subcall(event_id: str, block_id: str, agent_id: str, ts: str,
             name: str, label: str | None = None, *,
             stamped: bool = True) -> dict:
    payload: dict = {"name": "sub_agent", "id": block_id, "label": label,
                     "input": {"subagent_name": name}}
    if stamped:
        payload["id_ns"] = "block"
    return {"type": "tool_call", "id": event_id, "agent_id": agent_id,
            "timestamp": ts, "payload": payload}


def _subresult(block_id: str, agent_id: str, ts: str, error: bool = False, *,
               stamped: bool = True) -> dict:
    payload: dict = {"id": block_id, "error": error}
    if stamped:
        payload["id_ns"] = "block"
    return {"type": "tool_result", "agent_id": agent_id, "timestamp": ts,
            "payload": payload}


def test_step_own_tokens_summed_from_matching_agent_id():
    rows = build_summary([
        _started("plan-writer", "plan", "2026-01-01T00:00:00+00:00"),
        _llm("a1", "2026-01-01T00:00:05+00:00",
             input=100, output=20, cache_read=5, cache_creation=3),
        _llm("a1", "2026-01-01T00:00:10+00:00", input=10, output=2),
        _completed("plan-writer", "2026-01-01T00:00:20+00:00"),
    ])
    assert rows[0].agent_id == "a1"
    usage = rows[0].usage
    assert (usage.fresh_input, usage.output) == (110, 22)
    assert (usage.cached_input, usage.cache_write) == (5, 3)
    # sonnet, per million: 110*$3 + 5*$0.30 + 3*$3.75 + 22*$15 = $0.00067275
    assert usage.dollars == pytest.approx(0.00067275)
    assert (usage.priced, usage.unpriced, usage.notional) == (2, 0, 0)


def test_cumulative_query_results_in_one_session_take_the_last():
    # The over-report this quest exists to end: these two rows are cumulative
    # within one SDK session, so the step cost is 0.40 — not 0.34 + 0.40.
    rows = build_summary([
        _started("plan-writer", "plan", "2026-01-01T00:00:00+00:00"),
        _cc("a1", "2026-01-01T00:00:01+00:00"),
        _qr("a1", "2026-01-01T00:00:05+00:00", 0.34, session="s1"),
        _qr("a1", "2026-01-01T00:00:10+00:00", 0.40, session="s1"),
        _completed("plan-writer", "2026-01-01T00:00:20+00:00"),
    ])
    assert rows[0].usage.dollars == pytest.approx(0.40)


def test_query_results_from_two_sessions_add():
    rows = build_summary([
        _started("plan-writer", "plan", "2026-01-01T00:00:00+00:00"),
        _cc("a1", "2026-01-01T00:00:01+00:00"),
        _qr("a1", "2026-01-01T00:00:05+00:00", 0.34, session="s1"),
        _qr("a1", "2026-01-01T00:00:10+00:00", 0.40, session="s1"),
        _qr("a1", "2026-01-01T00:00:15+00:00", 0.06, session="s2"),
        _completed("plan-writer", "2026-01-01T00:00:20+00:00"),
    ])
    assert rows[0].usage.dollars == pytest.approx(0.46)


def test_the_hava67_shape_totals_28_11_not_45_01():
    # AC 3, at the measured figures: five cumulative rows across two sessions.
    # Summing every row naively gives $45.01 (what imp reports today); taking
    # the last of each session gives the true $28.11.
    series = [("s1", 3.00), ("s1", 9.50), ("s1", 16.90),
              ("s2", 4.40), ("s2", 11.21)]
    events = [_started("coder", "build", "2026-01-01T00:00:00+00:00"),
              _cc("a1", "2026-01-01T00:00:01+00:00")]
    for i, (session, cost) in enumerate(series):
        events.append(_qr("a1", f"2026-01-01T00:0{i}:30+00:00", cost,
                          session=session))
    events.append(_completed("coder", "2026-01-01T00:09:00+00:00"))
    # Both figures derive from `series`, so the contrast cannot go stale.
    assert sum(cost for _session, cost in series) == pytest.approx(45.01)
    rows = build_summary(events)
    assert rows[0].usage.dollars == pytest.approx(28.11)


def test_usage_from_other_agent_id_is_not_attributed():
    rows = build_summary([
        _started("plan-writer", "plan", "2026-01-01T00:00:00+00:00"),
        _context("a1", "2026-01-01T00:00:01+00:00"),
        _llm("other", "2026-01-01T00:00:05+00:00", input=999),
        _qr("other", "2026-01-01T00:00:06+00:00", 5.0),
        _completed("plan-writer", "2026-01-01T00:00:20+00:00"),
    ])
    assert rows[0].usage == RowUsage()


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
    assert rows[0].usage.fresh_input == 80


def test_sub_prefixed_agent_does_not_establish_step_mapping_or_tokens():
    rows = build_summary([
        _started("task-lead", "implement", "2026-01-01T00:00:00+00:00"),
        _llm("sub:ev1", "2026-01-01T00:00:05+00:00", input=999),
        _llm("a1", "2026-01-01T00:00:10+00:00", input=10),
        _completed("task-lead", "2026-01-01T00:00:20+00:00"),
    ])
    assert rows[0].agent_id == "a1"
    assert rows[0].usage.fresh_input == 10


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
        _cc("sub:ev9", "2026-01-01T00:00:20+00:00"),
        _qr("sub:ev9", "2026-01-01T00:00:30+00:00", 0.12, model_usage={
            "claude-sonnet-4-6": {"inputTokens": 200, "outputTokens": 40,
                                  "cacheReadInputTokens": 0,
                                  "cacheCreationInputTokens": 0}}),
        _subresult("blk9", "lead1", "2026-01-01T00:00:55+00:00"),
        _completed("task-lead", "2026-01-01T00:01:00+00:00"),
    ])
    sub = rows[0].subagents[0]
    assert (sub.usage.fresh_input, sub.usage.output) == (200, 40)
    assert sub.usage.dollars == pytest.approx(0.12)
    # The subagent's usage is its own; the parent's OWN figures stay empty.
    assert rows[0].usage == RowUsage()


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
    assert rows[0].usage.fresh_input == 100
    assert rows[0].subagents[0].usage.fresh_input == 200


def test_codex_tokens_and_cost_come_from_the_usage_patch_row():
    # AC 1: the agent_message carries usage={}; the real figures arrive on the
    # patch row build_summary used to ignore entirely.
    rows = build_summary([
        _started("coder", "build", "2026-01-01T00:00:00+00:00"),
        _codex("a1", "2026-01-01T00:00:01+00:00", auth_mode="chatgpt"),
        _patch("a1", input=1000, output=200, cache_read=4000,
               cache_creation=500, context_tokens=5000),
        _completed("coder", "2026-01-01T00:01:00+00:00"),
    ])
    usage = rows[0].usage
    assert (usage.fresh_input, usage.cached_input, usage.cache_write,
            usage.output) == (1000, 4000, 500, 200)
    # gpt-5.6-sol, per million: 1000*$4 + 4000*$0.40 + 500*$5 + 200*$20
    assert usage.dollars == pytest.approx(0.0121)
    assert (usage.priced, usage.notional) == (1, 1)   # chatgpt auth -> notional


def test_a_usage_patch_is_priced_against_the_preceding_message_model():
    # AC 24: the run's configured model on step_started is gpt-5, the turn ran
    # on gpt-5.6-sol. 1000 fresh + 1000 out is $0.024 on sol, $0.01125 on gpt-5.
    rows = build_summary([
        {"type": "step_started", "timestamp": "2026-01-01T00:00:00+00:00",
         "payload": {"step": "coder", "phase": "build", "model": "gpt-5"}},
        _codex("a1", "2026-01-01T00:00:01+00:00", auth_mode="apikey"),
        _patch("a1", input=1000, output=1000, context_tokens=2000),
        _completed("coder", "2026-01-01T00:01:00+00:00"),
    ])
    assert rows[0].usage.dollars == pytest.approx(0.024)
    assert rows[0].usage.notional == 0          # apikey -> metered
    assert rows[0].usage.priced == 1            # the patch row, once


def test_a_codex_turn_is_surcharged_on_context_tokens_not_the_patch_sum():
    # AC 7(b)/(c): the patch usage sums to 410k, over the 272k breakpoint, but
    # the turn's own occupancy reading is 100k — and an absent reading is never
    # surcharged either. Unsurcharged on gpt-5.6-sol, per million:
    #   200000*$4 + 200000*$0.40 + 10000*$20 = $1.08; surcharged = $2.06.
    def _cost(**patch_kw):
        rows = build_summary([
            _started("coder", "build", "2026-01-01T00:00:00+00:00"),
            _codex("a1", "2026-01-01T00:00:01+00:00"),
            _patch("a1", input=200_000, cache_read=200_000, output=10_000,
                   **patch_kw),
            _completed("coder", "2026-01-01T00:01:00+00:00"),
        ])
        return rows[0].usage.dollars

    assert _cost(context_tokens=100_000) == pytest.approx(1.08)
    assert _cost() == pytest.approx(1.08)                  # no reading at all
    assert _cost(context_tokens=300_000) == pytest.approx(2.06)


def test_codex_subagent_figures_routed_via_usage_patch():
    # _credit's sub-branch crossed with the last_msg lookup inside the patch
    # branch for a sub:-prefixed agent id — the path the step-level tests skip.
    rows = build_summary([
        _started("task-lead", "implement", "2026-01-01T00:00:00+00:00"),
        _context("a1", "2026-01-01T00:00:01+00:00"),
        _subcall("ev9", "blk9", "a1", "2026-01-01T00:00:10+00:00", "coder"),
        _codex("sub:ev9", "2026-01-01T00:00:15+00:00"),
        _patch("sub:ev9", input=500, output=200, context_tokens=1000),
        _subresult("blk9", "a1", "2026-01-01T00:00:55+00:00"),
        _completed("task-lead", "2026-01-01T00:01:00+00:00"),
    ])
    sub = rows[0].subagents[0]
    assert (sub.usage.fresh_input, sub.usage.output) == (500, 200)
    # gpt-5.6-sol, per million: 500*$4 + 200*$20 = $0.006
    assert sub.usage.dollars == pytest.approx(0.006)
    assert rows[0].usage == RowUsage()


def test_claude_code_tokens_come_solely_from_model_usage():
    # AC 2: the agent_message says 9999/8888 and must not contribute; the
    # model_usage rollup says 110+1100+200 in and 55 out, haiku included.
    rows = build_summary([
        _started("coder", "build", "2026-01-01T00:00:00+00:00"),
        _cc("a1", "2026-01-01T00:00:01+00:00"),
        _qr("a1", "2026-01-01T00:00:02+00:00", 0.42, model_usage={
            "claude-sonnet-4-6": {"inputTokens": 100, "outputTokens": 50,
                                  "cacheReadInputTokens": 1000,
                                  "cacheCreationInputTokens": 200},
            "claude-haiku-4-5-20251001": {"inputTokens": 10, "outputTokens": 5,
                                          "cacheReadInputTokens": 100,
                                          "cacheCreationInputTokens": 0}}),
        _completed("coder", "2026-01-01T00:01:00+00:00"),
    ])
    usage = rows[0].usage
    assert (usage.input_total, usage.output) == (1410, 55)
    assert usage.fresh_input == 110          # not 9999, and no partial sum of it


def test_claude_code_cost_is_the_vendor_figure_not_the_price_table():
    # AC 5: the table would compute $0.00018 for these tokens; the vendor says
    # $0.99, and the vendor figure is what the row carries.
    rows = build_summary([
        _started("coder", "build", "2026-01-01T00:00:00+00:00"),
        _cc("a1", "2026-01-01T00:00:01+00:00"),
        _qr("a1", "2026-01-01T00:00:02+00:00", 0.99, model_usage={
            "claude-sonnet-4-6": {"inputTokens": 10, "outputTokens": 10,
                                  "cacheReadInputTokens": 0,
                                  "cacheCreationInputTokens": 0,
                                  "costUSD": 0.99}}),
        _completed("coder", "2026-01-01T00:01:00+00:00"),
    ])
    assert rows[0].usage.dollars == pytest.approx(0.99)
    assert rows[0].usage.unpriced == 0       # claude_code never falls to n/a


def test_a_model_absent_from_the_price_table_counts_as_unpriced():
    rows = build_summary([
        _started("coder", "build", "2026-01-01T00:00:00+00:00"),
        _llm("a1", "2026-01-01T00:00:01+00:00", provider="openai",
             model="gpt-9-imaginary", input=1000, output=100),
        _completed("coder", "2026-01-01T00:01:00+00:00"),
    ])
    usage = rows[0].usage
    assert usage.fresh_input == 1000         # the tokens are still real
    assert (usage.priced, usage.unpriced) == (0, 1)
    assert usage.dollars == 0.0


def test_anthropic_and_openai_rows_are_metered_without_an_auth_mode():
    # AC 16(a): neither provider has a subscription mode, so neither carries an
    # auth_mode field and both read as metered from provider identity alone.
    for provider, model in (("anthropic", "claude-sonnet-4-6"),
                            ("openai", "gpt-5.6-sol")):
        rows = build_summary([
            _started("coder", "build", "2026-01-01T00:00:00+00:00"),
            _llm("a1", "2026-01-01T00:00:01+00:00", provider=provider,
                 model=model, input=100, output=10),
            _completed("coder", "2026-01-01T00:01:00+00:00"),
        ])
        assert rows[0].usage.notional == 0, provider
        assert rows[0].usage.priced == 1, provider


def test_a_claude_code_row_with_no_auth_mode_is_notional():
    # AC 16(c): an old log records no auth mode, and unknown renders notional.
    rows = build_summary([
        _started("coder", "build", "2026-01-01T00:00:00+00:00"),
        _cc("a1", "2026-01-01T00:00:01+00:00"),
        _qr("a1", "2026-01-01T00:00:02+00:00", 0.42),
        _completed("coder", "2026-01-01T00:01:00+00:00"),
    ])
    assert rows[0].usage.notional == 1


def test_a_claude_code_row_with_apikey_auth_is_metered():
    # AC 16(b): auth_mode rides on the agent_message; the query_result inherits
    # it from the preceding message for that agent.
    rows = build_summary([
        _started("coder", "build", "2026-01-01T00:00:00+00:00"),
        _cc("a1", "2026-01-01T00:00:01+00:00", auth_mode="apikey"),
        _qr("a1", "2026-01-01T00:00:02+00:00", 0.42),
        _completed("coder", "2026-01-01T00:01:00+00:00"),
    ])
    assert rows[0].usage.notional == 0


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


def test_tok_total_sums_the_input_side_and_output():
    usage = RowUsage(fresh_input=1, cached_input=3, cache_write=4, output=2)
    assert _tok_total(usage) == 10


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
                usage=RowUsage(fresh_input=1000, dollars=0.20, priced=1),
                subagents=[SubagentRow(
                    name="coder",
                    usage=RowUsage(fresh_input=500, dollars=0.05, priced=1))]),
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
                  usage=RowUsage(fresh_input=12000, output=345, dollars=0.34,
                                 priced=1))
    out = render_summary([row], header=_HDR, color=False, width=80)
    line = next(ln for ln in out if "plan-writer" in ln)
    assert line == "  plan-writer  1m 30s  12.3k  $0.34"


def test_cost_column_dash_fallback_when_zero():
    row = StepRow(phase="plan", step="s",
                  start_ts="2026-01-01T00:00:00+00:00",
                  end_ts="2026-01-01T00:00:10+00:00",
                  usage=RowUsage(fresh_input=5, priced=1))
    out = render_summary([row], header=_HDR, color=False, width=80)
    line = next(ln for ln in out if "10s" in ln)
    assert line == "  s  10s  5  —"


def test_duration_renders_in_step_hue():
    rows = [_row("plan", "plan-writer", 90)]
    out = render_summary(rows, header=_HDR, color=True, width=80)
    line = next(ln for ln in out if "plan-writer" in ln)
    assert "\x1b[34m1m 30s\x1b[0m" in line   # first hue: step-blue on the duration


def test_token_and_cost_columns_carry_no_color():
    row = StepRow(phase="plan", step="plan-writer",
                  start_ts="2026-01-01T00:00:00+00:00",
                  end_ts="2026-01-01T00:01:30+00:00",
                  usage=RowUsage(fresh_input=12000, output=345, dollars=0.34,
                                 priced=1))
    out = render_summary([row], header=_HDR, color=True, width=80)
    line = next(ln for ln in out if "plan-writer" in ln)
    # Duration is hued for contrast, but the token/cost columns stay default
    # foreground: the line ends with the plain "12.3k  $0.34" and no SGR reset.
    assert line.endswith("  12.3k  $0.34")
    assert "\x1b[34m1m 30s\x1b[0m" in line


def test_errored_step_duration_follows_error_color():
    rows = [_row("implement", "task-lead", 10, errored=True)]
    out = render_summary(rows, header=_HDR, color=True, width=80)
    line = next(ln for ln in out if "task-lead" in ln)
    assert "\x1b[1m\x1b[31m10s\x1b[0m" in line


def test_header_total_is_work_time_excluding_wait():
    rows = [StepRow(phase="implement", step="verify",
                    start_ts="2026-01-01T00:00:00+00:00",
                    end_ts="2026-01-01T00:02:00+00:00", wait_seconds=90.0)]
    hdr = SummaryHeader(workflow="f", quest_id="c", status="merged",
                        total_seconds=120.0)
    out = render_summary(rows, header=hdr, color=False, width=80)
    assert "30s" in out[0]     # 120s total - 90s wait = 30s work
    assert "2m 0s" not in out[0]


def test_header_rolls_up_step_tokens_and_cost():
    rows = [
        StepRow(phase="plan", step="a",
                start_ts="2026-01-01T00:00:00+00:00",
                end_ts="2026-01-01T00:00:30+00:00",
                usage=RowUsage(fresh_input=1000, dollars=0.20, priced=1)),
        StepRow(phase="plan", step="b",
                start_ts="2026-01-01T00:00:30+00:00",
                end_ts="2026-01-01T00:01:00+00:00",
                usage=RowUsage(fresh_input=1500, dollars=0.30, priced=1)),
    ]
    hdr = SummaryHeader(workflow="feature", quest_id="abc", status="merged",
                        total_seconds=60.0)
    out = render_summary(rows, header=hdr, color=False, width=80)
    assert "2.5k" in out[0]    # 1000 + 1500 tokens rolled up
    assert "$0.50" in out[0]   # 0.20 + 0.30 cost rolled up


def test_subagent_row_indented_with_own_figures():
    step = StepRow(phase="implement", step="task-lead",
                   start_ts="2026-01-01T00:00:00+00:00",
                   end_ts="2026-01-01T00:01:00+00:00",
                   usage=RowUsage(fresh_input=100, dollars=0.10, priced=1),
                   subagents=[SubagentRow(
                       name="explorer",
                       start_ts="2026-01-01T00:00:05+00:00",
                       end_ts="2026-01-01T00:00:50+00:00",
                       usage=RowUsage(fresh_input=200, dollars=0.05,
                                      priced=1))])
    out = render_summary([step], header=_HDR, color=False, width=80)
    step_line = next(ln for ln in out if "task-lead" in ln)
    sub_line = next(ln for ln in out if "explorer" in ln)
    assert step_line == "  task-lead     1m 0s  300  $0.15"
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
                   usage=RowUsage(fresh_input=1000, dollars=0.20, priced=1),
                   subagents=[SubagentRow(
                       name="coder",
                       start_ts="2026-01-01T00:00:05+00:00",
                       end_ts="2026-01-01T00:00:25+00:00",
                       usage=RowUsage(fresh_input=1500, dollars=0.30,
                                      priced=1))])
    hdr = SummaryHeader(workflow="f", quest_id="c", status="merged",
                        total_seconds=30.0)
    out = render_summary([step], header=hdr, color=False, width=80)
    assert "2.5k" in out[0]    # 1000 step + 1500 subagent
    assert "$0.50" in out[0]   # 0.20 step + 0.30 subagent


def test_header_merge_blocked_renders_two_words_bold():
    hdr = SummaryHeader(workflow="feature", quest_id="abc",
                        status="merge_blocked", total_seconds=100.0)
    out = render_summary([], header=hdr, color=True, width=80)
    assert "\x1b[1mmerge blocked\x1b[0m" in out[0]   # bold weight, two-word text
    assert "merge_blocked" not in out[0]


def test_header_waiting_renders_bold():
    hdr = SummaryHeader(workflow="feature", quest_id="abc",
                        status="waiting", total_seconds=100.0)
    out = render_summary([], header=hdr, color=True, width=80)
    assert "\x1b[1mwaiting\x1b[0m" in out[0]   # bold, like running


def test_plain_tool_result_cannot_close_a_stamped_subagent():
    prefix = [
        _started("task-lead", "implement", "2026-01-01T00:00:00+00:00"),
        _context("lead1", "2026-01-01T00:00:01+00:00"),
        _subcall("ev9", "item_5", "lead1", "2026-01-01T00:00:10+00:00", "coder"),
        # An ordinary, unrelated tool result that happens to reuse block id item_5
        # (codex block ids are short per-turn sequence numbers).
        _subresult("item_5", "lead1", "2026-01-01T00:00:30+00:00", stamped=False),
    ]
    ignored = build_summary(prefix)[0].subagents[0]
    assert ignored.end_ts is None
    assert ignored.wall_seconds is None

    # ...and its own stamped result still closes it afterwards.
    closed = build_summary([
        *prefix,
        _subresult("item_5", "lead1", "2026-01-01T00:00:50+00:00"),
    ])[0].subagents[0]
    assert closed.end_ts == "2026-01-01T00:00:50+00:00"
    assert closed.wall_seconds == 40.0


def test_duplicate_stamped_result_does_not_move_end_ts():
    rows = build_summary([
        _started("task-lead", "implement", "2026-01-01T00:00:00+00:00"),
        _context("lead1", "2026-01-01T00:00:01+00:00"),
        _subcall("ev9", "item_5", "lead1", "2026-01-01T00:00:10+00:00", "coder"),
        _subresult("item_5", "lead1", "2026-01-01T00:00:55+00:00"),
        _subresult("item_5", "lead1", "2026-01-01T00:02:00+00:00"),
        _completed("task-lead", "2026-01-01T00:03:00+00:00"),
    ])
    sub = rows[0].subagents[0]
    assert sub.end_ts == "2026-01-01T00:00:55+00:00"
    assert sub.wall_seconds == 45.0


def test_legacy_unstamped_pair_still_closes():
    rows = build_summary([
        _started("task-lead", "implement", "2026-01-01T00:00:00+00:00"),
        _context("lead1", "2026-01-01T00:00:01+00:00"),
        _subcall("ev9", "blk9", "lead1", "2026-01-01T00:00:10+00:00", "explorer",
                 stamped=False),
        _subresult("blk9", "lead1", "2026-01-01T00:00:55+00:00", stamped=False),
        _completed("task-lead", "2026-01-01T00:01:00+00:00"),
    ])
    assert rows[0].subagents[0].wall_seconds == 45.0


def test_open_subagent_is_contained_at_its_step_end():
    rows = build_summary([
        _started("task-lead", "implement", "2026-01-01T00:00:00+00:00"),
        _context("lead1", "2026-01-01T00:00:01+00:00"),
        _subcall("ev9", "item_3", "lead1", "2026-01-01T00:00:10+00:00", "coder"),
        _completed("task-lead", "2026-01-01T00:01:00+00:00"),
    ])
    sub = rows[0].subagents[0]
    assert sub.end_ts == "2026-01-01T00:01:00+00:00"
    assert sub.wall_seconds == 50.0
    assert sub.end_inferred is True


def test_observed_close_is_not_marked_inferred():
    rows = build_summary([
        _started("task-lead", "implement", "2026-01-01T00:00:00+00:00"),
        _context("lead1", "2026-01-01T00:00:01+00:00"),
        _subcall("ev9", "item_3", "lead1", "2026-01-01T00:00:10+00:00", "coder"),
        _subresult("item_3", "lead1", "2026-01-01T00:00:55+00:00"),
        _completed("task-lead", "2026-01-01T00:01:00+00:00"),
    ])
    sub = rows[0].subagents[0]
    assert sub.wall_seconds == 45.0
    assert sub.end_inferred is False


def test_second_subagent_reusing_an_open_block_id_is_still_contained():
    # Codex opens the fold at item.started with the raw SDK item id
    # (codex_runner.py:1085,1107-1110). An interrupt between started and completed
    # leaves the first dispatch resultless; on resume the step folds into the same
    # occurrence while item numbering restarts, so the next dispatch reuses item_3.
    rows = build_summary([
        _started("task-lead", "implement", "2026-01-01T00:00:00+00:00"),
        _context("lead1", "2026-01-01T00:00:01+00:00"),
        _subcall("ev1", "item_3", "lead1", "2026-01-01T00:00:10+00:00", "coder"),
        _subcall("ev2", "item_3", "lead1", "2026-01-01T00:00:40+00:00", "explorer"),
        _subresult("item_3", "lead1", "2026-01-01T00:01:00+00:00"),
        _completed("task-lead", "2026-01-01T00:02:00+00:00"),
    ])
    first, second = rows[0].subagents
    assert second.name == "explorer"
    assert second.end_ts == "2026-01-01T00:01:00+00:00"    # its own stamped result
    assert second.end_inferred is False
    # The evicted first row is unreachable through the pairing table, so it must be
    # closed off the step's own list or it stays open forever.
    assert first.end_ts == "2026-01-01T00:02:00+00:00"     # contained at step end
    assert first.end_inferred is True
    assert first.wall_seconds == 110.0


def test_repeated_block_id_in_a_later_step_cannot_reach_back():
    rows = build_summary([
        _started("task-lead", "implement", "2026-01-01T00:00:00+00:00"),
        _context("lead1", "2026-01-01T00:00:01+00:00"),
        _subcall("ev1", "item_3", "lead1", "2026-01-01T00:00:10+00:00", "coder"),
        _completed("task-lead", "2026-01-01T00:01:00+00:00"),
        _started("verify", "implement", "2026-01-01T00:01:05+00:00"),
        _subresult("item_3", "v1", "2026-01-01T00:02:00+00:00"),
        _completed("verify", "2026-01-01T00:02:30+00:00"),
    ])
    sub = rows[0].subagents[0]
    assert sub.end_ts == "2026-01-01T00:01:00+00:00"   # step A's end, not step B's result
    assert sub.wall_seconds == 50.0
    assert rows[1].subagents == []


def test_step_without_completion_closes_at_the_next_step_start():
    rows = build_summary([
        _started("task-lead", "implement", "2026-01-01T00:00:00+00:00"),
        _started("verify", "implement", "2026-01-01T00:05:00+00:00"),
        _completed("verify", "2026-01-01T00:06:00+00:00"),
    ])
    assert rows[0].end_ts == "2026-01-01T00:05:00+00:00"
    assert rows[0].wall_seconds == 300.0
    assert rows[0].end_inferred is True
    assert rows[1].end_inferred is False


def test_trailing_open_step_stays_open_and_uninferred():
    rows = build_summary([
        _started("task-lead", "implement", "2026-01-01T00:00:00+00:00"),
        _context("lead1", "2026-01-01T00:00:01+00:00"),
    ])
    assert rows[0].end_ts is None
    assert rows[0].end_inferred is False


def test_resume_reentry_is_not_an_inferred_close():
    rows = build_summary([
        _started("verify", "implement", "2026-01-01T00:00:00+00:00"),
        _await("2026-01-01T00:01:00+00:00"),
        _started("verify", "implement", "2026-01-01T00:06:00+00:00"),  # resume
        _completed("verify", "2026-01-01T00:06:30+00:00"),
    ])
    assert len(rows) == 1
    assert rows[0].end_inferred is False


def test_every_subagent_is_contained_on_a_codex_shaped_log():
    rows = build_summary([
        _started("task-lead", "implement", "2026-01-01T00:00:00+00:00"),
        _context("lead1", "2026-01-01T00:00:01+00:00"),
        _subcall("ev1", "item_3", "lead1", "2026-01-01T00:00:10+00:00", "coder"),
        _subresult("item_3", "lead1", "2026-01-01T00:02:00+00:00"),
        _subcall("ev2", "item_5", "lead1", "2026-01-01T00:02:10+00:00", "explorer"),
        # unrelated tool result reusing item_5 while the explorer is still open
        _subresult("item_5", "lead1", "2026-01-01T00:02:20+00:00", stamped=False),
        _completed("task-lead", "2026-01-01T00:05:00+00:00"),
        _started("code-review", "review", "2026-01-01T00:05:05+00:00"),
        _subcall("ev3", "item_3", "rev1", "2026-01-01T00:05:10+00:00", "reviewer"),
        # a stale id from the first step, stamped, arriving in this one
        _subresult("item_5", "rev1", "2026-01-01T00:06:00+00:00"),
        _started("verify", "implement", "2026-01-01T00:08:00+00:00"),  # review never completes
        _completed("verify", "2026-01-01T00:09:00+00:00"),
    ])
    assert [r.step for r in rows] == ["task-lead", "code-review", "verify"]
    for row in rows:
        assert row.end_ts is not None
        for sub in row.subagents:
            ran = elapsed_seconds(sub.start_ts, sub.end_ts)
            slack = elapsed_seconds(sub.end_ts, row.end_ts)
            assert ran is not None and ran >= 0
            assert slack is not None and slack >= 0
    assert rows[0].subagents[0].wall_seconds == 110.0            # its own stamped result
    assert rows[0].subagents[1].end_ts == "2026-01-01T00:05:00+00:00"  # contained at step end
    assert rows[1].subagents[0].end_ts == "2026-01-01T00:08:00+00:00"  # step closed at next start


def test_dur_text_marks_an_inferred_end_with_a_tilde():
    assert _dur_text(90.0, inferred=True) == "~1m 30s"
    assert _dur_text(90.0) == "1m 30s"


@pytest.mark.parametrize("status,expected", [
    ("merged", "interrupted"),
    ("done", "interrupted"),
    ("abandoned", "interrupted"),
    ("running", "running"),
    ("waiting", "waiting"),
    ("paused", "paused"),
    ("new", "new"),
    ("error", "error"),
    ("shelved", "shelved"),          # parked, not finished: keeps its status word
    ("merge_blocked", "merge blocked"),
])
def test_open_span_label_word_per_status(status, expected):
    assert open_span_label(status) == expected


def test_inferred_ends_render_with_a_tilde_on_step_and_subagent():
    step = StepRow(phase="implement", step="task-lead",
                   start_ts="2026-01-01T00:00:00+00:00",
                   end_ts="2026-01-01T00:05:00+00:00", end_inferred=True,
                   subagents=[SubagentRow(name="coder",
                                          start_ts="2026-01-01T00:00:10+00:00",
                                          end_ts="2026-01-01T00:02:24+00:00",
                                          end_inferred=True)])
    out = render_summary([step], header=_HDR, color=False, width=80)
    assert out[-2:] == [
        "  task-lead   ~5m 0s  —  —",
        "    ↳ coder  ~2m 14s  —  —",
    ]


def test_open_spans_in_a_finished_quest_read_interrupted():
    step = StepRow(phase="implement", step="task-lead",
                   start_ts="2026-01-01T00:00:00+00:00", end_ts=None,
                   subagents=[SubagentRow(name="coder",
                                          start_ts="2026-01-01T00:00:10+00:00")])
    out = render_summary([step], header=_HDR, color=False, width=80)   # _HDR is merged
    assert out[-2:] == [
        "  task-lead  interrupted  —  —",
        "    ↳ coder  interrupted  —  —",
    ]
    assert "merged" not in "\n".join(out[1:])   # the status word stays in the header


@pytest.mark.parametrize("status", ["done", "abandoned"])
def test_other_finished_statuses_also_read_interrupted(status):
    hdr = SummaryHeader(workflow="feature", quest_id="abc", status=status,
                        total_seconds=100.0)
    out = render_summary([_row("implement", "task-lead", 0, open_=True)],
                         header=hdr, color=False, width=80)
    assert "  task-lead  interrupted" in out[-1]
    assert status not in out[-1]


def test_merge_blocked_open_step_reads_two_words():
    hdr = SummaryHeader(workflow="feature", quest_id="abc",
                        status="merge_blocked", total_seconds=100.0)
    out = render_summary([_row("implement", "task-lead", 0, open_=True)],
                         header=hdr, color=False, width=80)
    assert "  task-lead  merge blocked" in out[-1]
    assert "merge_blocked" not in out[-1]


def test_alignment_across_tilde_plain_and_interrupted_cells():
    rows = [
        StepRow(phase="implement", step="a",
                start_ts="2026-01-01T00:00:00+00:00",
                end_ts="2026-01-01T00:01:00+00:00",
                subagents=[SubagentRow(name="coder",
                                       start_ts="2026-01-01T00:00:05+00:00",
                                       end_ts="2026-01-01T00:00:50+00:00")]),
        StepRow(phase="implement", step="b",
                start_ts="2026-01-01T00:01:00+00:00",
                end_ts="2026-01-01T00:01:45+00:00", end_inferred=True),
        StepRow(phase="implement", step="c",
                start_ts="2026-01-01T00:02:00+00:00", end_ts=None),
    ]
    out = render_summary(rows, header=_HDR, color=False, width=80)
    assert out[2:] == [
        "  a                1m 0s  —  —",
        "    ↳ coder          45s  —  —",
        "  b                 ~45s  —  —",
        "  c          interrupted  —  —",
    ]
    assert len({len(ln) for ln in out[2:]}) == 1   # one column grid for every row


def test_step_totals_include_every_subagent_while_own_usage_does_not():
    step = StepRow(phase="implement", step="task-lead", start_ts=None,
                   usage=RowUsage(fresh_input=100, output=10, dollars=0.10,
                                  priced=1),
                   subagents=[
                       SubagentRow(name="explorer",
                                   usage=RowUsage(fresh_input=200, output=20,
                                                  dollars=0.05, priced=1)),
                       SubagentRow(name="coder",
                                   usage=RowUsage(fresh_input=300, output=30,
                                                  dollars=0.15, priced=1,
                                                  notional=1))])
    assert step.usage.fresh_input == 100        # own is untouched
    assert step.totals.fresh_input == 600
    assert step.totals.output == 60
    assert step.totals.dollars == pytest.approx(0.30)
    assert step.totals.priced == 3
    assert step.totals.notional == 1            # the mix is visible to Part 5


def test_a_leaf_step_totals_are_its_own_usage():
    step = StepRow(phase="p", step="s", start_ts=None,
                   usage=RowUsage(fresh_input=7, dollars=0.01, priced=1))
    assert step.totals == step.usage
