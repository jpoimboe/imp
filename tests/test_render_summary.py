from __future__ import annotations

import pytest

from imp.cli import palette as pal
from imp.cli.accounting import RowUsage
from imp.cli.narrative import Exclusions, elapsed_seconds
from imp.cli.render_summary import (
    StepRow,
    SubagentRow,
    SummaryHeader,
    _dur_text,
    _fmt_tokens,
    _sub_body,
    build_summary,
    cost_cell,
    exclusion_spans,
    legend_lines,
    open_span_label,
    render_summary,
    summary_total,
    summary_totals,
)
from tests._event_fixtures import titled_subagent_events


def _started(step: str, phase: str, ts: str) -> dict:
    return {"type": "step_started", "timestamp": ts,
            "payload": {"step": step, "phase": phase}}


def _completed(step: str, ts: str) -> dict:
    return {"type": "step_completed", "timestamp": ts,
            "payload": {"step": step, "action": {"verb": "continue"}}}


def _await(ts: str) -> dict:
    return {"type": "awaiting_user_input", "timestamp": ts, "payload": {}}


def _reply(ts: str, subtype: str = "reply") -> dict:
    return {"type": "user_message", "timestamp": ts,
            "payload": {"role": "user", "content": [], "subtype": subtype}}


def _paused(ts: str) -> dict:
    return {"type": "workflow_paused", "timestamp": ts,
            "payload": {"reason": "ctrl_c"}}


def _resumed(ts: str) -> dict:
    return {"type": "workflow_resumed", "timestamp": ts, "payload": {}}


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


def test_two_human_turns_deduct_both_waits_and_not_the_work_after_the_reply():
    rows = build_summary([
        _started("threshold", "spec", "2026-01-01T00:00:00+00:00"),
        _await("2026-01-01T00:01:00+00:00"),
        _reply("2026-01-01T00:02:00+00:00"),
        _await("2026-01-01T00:03:00+00:00"),
        _reply("2026-01-01T00:05:00+00:00"),
        _completed("threshold", "2026-01-01T00:10:00+00:00")])
    assert rows[0].wait_seconds == 180.0          # both turns, 60s + 120s
    assert rows[0].work_seconds == 420.0          # the single marker gave 180s


def test_a_step_containing_a_pause_excludes_the_gap():
    # AC 16(b): neither event was handled at all, so the gap was charged as work.
    rows = build_summary([
        _started("verify", "implement", "2026-01-01T00:00:00+00:00"),
        _paused("2026-01-01T00:02:00+00:00"),
        _resumed("2026-01-01T00:09:00+00:00"),
        _completed("verify", "2026-01-01T00:10:00+00:00")])
    assert rows[0].wait_seconds == 420.0
    assert rows[0].work_seconds == 180.0


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


def _row(phase, step, secs, *, wait=0.0, errored=False, open_=False,
         label=None, phase_label=None):
    start = "2026-01-01T00:00:00+00:00"
    end = None if open_ else f"2026-01-01T00:{secs // 60:02d}:{secs % 60:02d}+00:00"
    return StepRow(phase=phase, step=step, start_ts=start, end_ts=end,
                   wait_seconds=wait, errored=errored,
                   label=label, phase_label=phase_label)


_HDR = SummaryHeader(title="feature", quest_id="abc",
                     status="completed", total_seconds=2342.0)


def test_plain_output_has_no_sgr_and_expected_lines():
    rows = [_row("plan", "plan-writer", 90)]
    lines = render_summary(rows, header=_HDR, color=False, width=80)
    assert lines == [
        "abc  ·  feature  ·  completed",
        "                 time  in  out  cost",
        "plan phase",
        "  plan-writer  1m 30s   —    —     —",
        "",
        "  total        39m 2s   —    —     —",
    ]


def test_plain_multi_step_shows_column_padding():
    rows = [
        _row("plan", "plan-writer", 90),
        _row("plan", "plan-review", 120),
        _row("implement", "verify", 30),
    ]
    lines = render_summary(rows, header=_HDR, color=False, width=80)
    assert lines == [
        "abc  ·  feature  ·  completed",
        "                 time  in  out  cost",
        "plan phase",
        "  plan-writer  1m 30s   —    —     —",
        "  plan-review   2m 0s   —    —     —",
        "implement phase",
        "  verify          30s   —    —     —",
        "",
        "  total        39m 2s   —    —     —",
    ]


_OLD_ROTATION_HUES = ("magenta", "cyan", "green")


@pytest.mark.parametrize("theme", pal.THEMES)
def test_every_step_row_wears_the_step_hue_whatever_its_position(theme):
    rows = [
        _row("plan", "plan-writer", 10),
        _row("plan", "plan-review", 20),
        _row("plan", "verify", 30),
        _row("plan", "task-lead", 40),
        _row("plan", "plan-review", 50),
        _row("implement", "coder", 60),
        _row("implement", "reviewer", 70),
    ]
    pal.set_theme(theme)
    try:
        step = pal._ANSI["step"]
        others = [pal._ANSI[h] for h in _OLD_ROTATION_HUES]
        out = render_summary(rows, header=_HDR, color=True, width=80)
    finally:
        pal.set_theme("ansi")
    names = ("plan-writer", "plan-review", "verify", "task-lead", "coder",
             "reviewer")
    step_lines = [ln for ln in out if any(n in ln for n in names)]
    assert len(step_lines) == len(rows)
    for ln in step_lines:
        assert ln.startswith(f"  {step}")          # name in the step hue
        assert ln.count(step) == 2                 # ...and the duration
        assert not any(h in ln for h in others)    # no positional magenta/cyan/green


@pytest.mark.parametrize("theme", pal.THEMES)
def test_subagent_rows_stay_magenta_and_errored_steps_bold_red(theme):
    rows = [
        StepRow(phase="plan", step="plan-writer",
                start_ts="2026-01-01T00:00:00+00:00",
                end_ts="2026-01-01T00:01:00+00:00",
                subagents=[SubagentRow(name="explorer",
                                       start_ts="2026-01-01T00:00:05+00:00",
                                       end_ts="2026-01-01T00:00:50+00:00")]),
        _row("plan", "task-lead", 10, errored=True),
    ]
    pal.set_theme(theme)
    try:
        mag, dim = pal._ANSI["magenta"], pal._ANSI["dim"]
        bold, red = pal._ANSI["bold"], pal._ANSI["red"]
        out = render_summary(rows, header=_HDR, color=True, width=80)
    finally:
        pal.set_theme("ansi")
    sub = next(ln for ln in out if "↳ explorer" in ln)
    assert sub.startswith(f"    {mag}↳ explorer")
    assert f"{dim}   45s" in sub
    err = next(ln for ln in out if "task-lead" in ln)
    assert err.startswith(f"  {bold}{red}task-lead")
    assert err.count(f"{bold}{red}") == 2


def test_errored_step_renders_bold_red():
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
    hdr = SummaryHeader(title="feature", quest_id="abc",
                        status="running", total_seconds=None)
    rows = [_row("implement", "task-lead", 0, open_=True)]
    out = render_summary(rows, header=hdr, color=False, width=80)
    assert "  task-lead  running" in out[3]


def test_open_trailing_step_on_paused_change_reflects_status():
    hdr = SummaryHeader(title="feature", quest_id="abc",
                        status="paused", total_seconds=100.0)
    rows = [_row("implement", "task-lead", 0, open_=True)]
    out = render_summary(rows, header=hdr, color=False, width=80)
    assert "  task-lead  paused" in out[3]
    assert "running" not in out[3]


def test_header_id_is_coloured_and_completed_status_plain():
    out = render_summary([], header=_HDR, color=True, width=80)
    assert out[0].startswith(pal.ansi("abc", *pal.NODE_COLORS["quest_id"], enabled=True))
    assert "completed" in out[0]                       # completed status plain (no color)


def test_summary_total_is_first_to_last_timestamp():
    events = [{"timestamp": "2026-01-01T00:00:00+00:00"},
              {"timestamp": "2026-01-01T00:39:02+00:00"}]
    assert summary_total(events) == 2342.0
    assert summary_total([]) is None


def test_exclusion_spans_pair_every_human_turn_with_its_own_reply():
    # Two gates in one step: the old single marker kept only the last one and
    # ran it to step_completed, charging the post-reply work as wait.
    excl = exclusion_spans([
        _started("threshold", "spec", "2026-01-01T00:00:00+00:00"),
        _await("2026-01-01T00:01:00+00:00"),
        _reply("2026-01-01T00:02:00+00:00"),
        _await("2026-01-01T00:03:00+00:00"),
        _reply("2026-01-01T00:05:00+00:00"),
        _completed("threshold", "2026-01-01T00:10:00+00:00"),
    ])
    assert excl.spans == (
        ("2026-01-01T00:01:00+00:00", "2026-01-01T00:02:00+00:00"),
        ("2026-01-01T00:03:00+00:00", "2026-01-01T00:05:00+00:00"))
    assert excl.total == 180.0                    # 60s + 120s, not 420s


def test_an_unanswered_gate_closes_at_the_step_boundary():
    excl = exclusion_spans([
        _started("verify", "implement", "2026-01-01T00:00:00+00:00"),
        _await("2026-01-01T00:00:30+00:00"),
        _completed("verify", "2026-01-01T00:02:00+00:00")])
    assert excl.total == 90.0


def test_a_reply_with_no_open_gate_excludes_nothing():
    # The opening intent lands before any step; an interject lands mid-step with
    # no gate open. Neither is a human wait.
    excl = exclusion_spans([
        _reply("2026-01-01T00:00:00+00:00", "intent"),
        _started("verify", "implement", "2026-01-01T00:01:00+00:00"),
        _reply("2026-01-01T00:02:00+00:00", "interject"),
        _completed("verify", "2026-01-01T00:03:00+00:00")])
    assert excl.spans == ()
    assert excl.total == 0.0


def test_a_re_armed_gate_keeps_the_first_ask():
    excl = exclusion_spans([
        _started("verify", "implement", "2026-01-01T00:00:00+00:00"),
        _await("2026-01-01T00:01:00+00:00"),
        _await("2026-01-01T00:01:30+00:00"),
        _reply("2026-01-01T00:03:00+00:00"),
        _completed("verify", "2026-01-01T00:04:00+00:00")])
    assert excl.total == 120.0                    # from the first ask, not 90s


def test_a_pause_pairs_with_its_resume():
    excl = exclusion_spans([
        _started("verify", "implement", "2026-01-01T00:00:00+00:00"),
        _paused("2026-01-01T00:02:00+00:00"),
        _resumed("2026-01-01T00:09:00+00:00"),
        _completed("verify", "2026-01-01T00:10:00+00:00")])
    assert excl.total == 420.0


def test_a_pause_inside_a_human_wait_is_excluded_once():
    # AC 16(c). The pause closes first, so the spans arrive out of start order:
    # summing them gives 480s, the union gives the 300s wait that contains it.
    excl = exclusion_spans([
        _started("verify", "implement", "2026-01-01T00:00:00+00:00"),
        _await("2026-01-01T00:01:00+00:00"),
        _paused("2026-01-01T00:02:00+00:00"),
        _resumed("2026-01-01T00:05:00+00:00"),
        _reply("2026-01-01T00:06:00+00:00"),
        _completed("verify", "2026-01-01T00:10:00+00:00")])
    assert excl.total == 300.0


def test_an_interval_the_log_never_closes_excludes_nothing():
    excl = exclusion_spans([
        _started("verify", "implement", "2026-01-01T00:00:00+00:00"),
        _await("2026-01-01T00:01:00+00:00")])
    assert excl.spans == ()
    assert excl.total == 0.0


def test_overlap_clips_to_the_queried_span():
    excl = Exclusions((("2026-01-01T00:00:30+00:00",
                        "2026-01-01T00:02:30+00:00"),))
    assert excl.overlap("2026-01-01T00:00:00+00:00",
                        "2026-01-01T00:01:30+00:00") == 60.0
    assert excl.overlap("2026-01-01T00:00:00+00:00", None) == 0.0


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


def _codex(agent_id: str, ts: str, *, model="gpt-6.1-sol",
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
    """A claude agent_message: ignored by the accounting path entirely —
    its usage is the unreliable start-of-message snapshot."""
    return {"type": "agent_message", "agent_id": agent_id, "timestamp": ts,
            "payload": {"provider": "claude", "model": "claude-sonnet-4-6",
                        "usage": {"input_tokens": 9999, "output_tokens": 8888},
                        **({"auth_mode": auth_mode} if auth_mode else {})}}


def _qr(agent_id: str, ts: str, cost: float | None = None, *,
        session: str = "s1", model_usage: dict | None = None) -> dict:
    """`total_cost_usd` is `float | None` on the wire; passing no cost omits the
    key entirely — the shape that falls back to the price table."""
    payload: dict = {"session_id": session, "usage": {},
                     "model_usage": model_usage if model_usage is not None
                     else {}}
    if cost is not None:
        payload["total_cost_usd"] = cost
    return {"type": "query_result", "agent_id": agent_id, "timestamp": ts,
            "payload": payload}


# 5,000*$3 + 200,000*$0.30 + 8,000*$15 per million = $0.195 on claude-sonnet-4-6.
_FALLBACK_USAGE = {"claude-sonnet-4-6": {"inputTokens": 5000,
                                         "outputTokens": 8000,
                                         "cacheReadInputTokens": 200_000,
                                         "cacheCreationInputTokens": 0}}


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
    # sonnet, per million: 110*$3 + 5*$0.30 + 3*$6.00 + 22*$15 = $0.0006795
    assert usage.dollars == pytest.approx(0.0006795)
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
    # gpt-6.1-sol, per million: 1000*$2 + 4000*$0.10 + 500*$2.50 + 200*$10
    assert usage.dollars == pytest.approx(0.00565)
    assert (usage.priced, usage.notional) == (1, 1)   # chatgpt auth -> notional


def test_a_usage_patch_is_priced_against_the_preceding_message_model():
    # AC 24: the run's configured model on step_started is gpt-5, the turn ran
    # on gpt-6.1-sol. 1000 fresh + 1000 out is $0.012 on sol, $0.01125 on gpt-5.
    rows = build_summary([
        {"type": "step_started", "timestamp": "2026-01-01T00:00:00+00:00",
         "payload": {"step": "coder", "phase": "build", "model": "gpt-5"}},
        _codex("a1", "2026-01-01T00:00:01+00:00", auth_mode="apikey"),
        _patch("a1", input=1000, output=1000, context_tokens=2000),
        _completed("coder", "2026-01-01T00:01:00+00:00"),
    ])
    assert rows[0].usage.dollars == pytest.approx(0.012)
    assert rows[0].usage.notional == 0          # apikey -> metered
    assert rows[0].usage.priced == 1            # the patch row, once


def test_a_codex_turn_is_surcharged_on_context_tokens_not_the_patch_sum():
    # AC 7(b)/(c): the patch usage sums to 410k, over the 272k breakpoint, but
    # the turn's own occupancy reading is 100k — and an absent reading is never
    # surcharged either. Unsurcharged on gpt-6.1-sol, per million:
    #   200000*$2 + 200000*$0.10 + 10000*$10 = $0.52; surcharged = $0.99.
    def _cost(**patch_kw):
        rows = build_summary([
            _started("coder", "build", "2026-01-01T00:00:00+00:00"),
            _codex("a1", "2026-01-01T00:00:01+00:00"),
            _patch("a1", input=200_000, cache_read=200_000, output=10_000,
                   **patch_kw),
            _completed("coder", "2026-01-01T00:01:00+00:00"),
        ])
        return rows[0].usage.dollars

    assert _cost(context_tokens=100_000) == pytest.approx(0.52)
    assert _cost() == pytest.approx(0.52)                  # no reading at all
    assert _cost(context_tokens=300_000) == pytest.approx(0.99)


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
    # gpt-6.1-sol, per million: 500*$2 + 200*$10 = $0.003
    assert sub.usage.dollars == pytest.approx(0.003)
    assert rows[0].usage == RowUsage()


def test_claude_tokens_come_solely_from_model_usage():
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


def test_claude_cost_is_the_vendor_figure_not_the_price_table():
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
    assert rows[0].usage.unpriced == 0       # a vendor figure is priced by definition


def _fallback_events(model_usage: dict, *, auth_mode: str | None = None,
                     with_query_result: bool = True) -> list[dict]:
    """A claude step whose query_result carries no `total_cost_usd`."""
    events = [
        _started("reviewer", "build", "2026-01-01T00:00:00+00:00"),
        _cc("a1", "2026-01-01T00:00:01+00:00", auth_mode=auth_mode),
    ]
    if with_query_result:
        events.append(_qr("a1", "2026-01-01T00:00:02+00:00",
                          model_usage=model_usage))
    events.append(_completed("reviewer", "2026-01-01T00:01:00+00:00"))
    return events


def test_a_claude_row_with_no_vendor_cost_is_priced_from_the_table():
    # AC 1: the SDK declares total_cost_usd as `float | None`; with none on the
    # wire the row is priced per model instead of being credited $0.00.
    rows = build_summary(_fallback_events(_FALLBACK_USAGE))
    assert rows[0].usage.dollars == pytest.approx(0.195)
    assert cost_cell(rows[0].totals) == "$0.20*"
    without = build_summary(
        _fallback_events(_FALLBACK_USAGE, with_query_result=False))
    assert (summary_totals(rows, 60.0)[1].dollars
            - summary_totals(without, 60.0)[1].dollars) == pytest.approx(0.195)


def test_a_metered_claude_row_priced_from_the_table_renders_a_tilde():
    # AC 2: apikey auth is metered, so the same computed figure drops the
    # asterisk and takes the tilde.
    rows = build_summary(
        _fallback_events(_FALLBACK_USAGE, auth_mode="apikey"))
    assert cost_cell(rows[0].totals) == "~$0.20"


def test_a_vendor_zero_renders_as_the_vendors_own_zero():
    # AC 4: $0.00 from the vendor IS a vendor figure, so the table is never
    # consulted and the cell keeps its sub-cent form rather than showing $0.20.
    rows = build_summary([
        _started("reviewer", "build", "2026-01-01T00:00:00+00:00"),
        _cc("a1", "2026-01-01T00:00:01+00:00"),
        _qr("a1", "2026-01-01T00:00:02+00:00", 0.0,
            model_usage=_FALLBACK_USAGE),
        _completed("reviewer", "2026-01-01T00:01:00+00:00"),
    ])
    assert rows[0].usage.dollars == 0.0
    assert cost_cell(rows[0].totals) == "<$0.01*"


def test_a_vendor_less_row_floors_when_one_model_is_absent_from_the_table():
    # AC 5: the priced model's dollars alone, prefixed ≥, and the legend states
    # the one cause a ≥ has ever had.
    rows = build_summary(_fallback_events({
        **_FALLBACK_USAGE,
        "claude-mythic-9": {"inputTokens": 2000, "outputTokens": 1000}}))
    assert cost_cell(rows[0].totals) == "≥$0.20*"
    hdr = SummaryHeader(title="f", quest_id="c", status="completed",
                        total_seconds=60.0)
    out = render_summary(rows, header=hdr, color=False, width=80)
    assert out[-1] == \
        "  ≥ excludes steps run on a model absent from the price table"


def test_a_vendor_less_row_of_unknown_models_renders_n_a_and_floors_the_total():
    # AC 6: every id absent from the table — the tokens count, the dollars
    # cannot, and the total says so rather than shrinking.
    rows = build_summary([
        _started("reviewer", "build", "2026-01-01T00:00:00+00:00"),
        _cc("a1", "2026-01-01T00:00:01+00:00"),
        _qr("a1", "2026-01-01T00:00:02+00:00", model_usage={
            "claude-mythic-9": {"inputTokens": 2000, "outputTokens": 1000}}),
        _completed("reviewer", "2026-01-01T00:00:30+00:00"),
        _started("coder", "build", "2026-01-01T00:00:30+00:00"),
        _cc("a2", "2026-01-01T00:00:31+00:00"),
        _qr("a2", "2026-01-01T00:00:32+00:00", session="s2",
            model_usage=_FALLBACK_USAGE),
        _completed("coder", "2026-01-01T00:01:00+00:00"),
    ])
    assert cost_cell(rows[0].totals) == "n/a"
    assert cost_cell(summary_totals(rows, 60.0)[1]) == "≥$0.20*"


def test_a_vendor_less_row_sums_three_priced_models_without_a_floor():
    # AC 7: per million — sonnet $0.018, haiku $0.006, opus $0.030 = $0.054.
    # The priced counter trips three times and the cell is unprefixed anyway.
    rows = build_summary(_fallback_events({
        "claude-sonnet-4-6": {"inputTokens": 1000, "outputTokens": 1000},
        "claude-haiku-4-5-20251001": {"inputTokens": 1000, "outputTokens": 1000},
        "claude-opus-5": {"inputTokens": 1000, "outputTokens": 1000}}))
    assert rows[0].usage.dollars == pytest.approx(0.054)
    assert rows[0].usage.priced == 3
    assert cost_cell(rows[0].totals) == "$0.05*"


def test_a_vendor_less_row_with_no_billable_usage_renders_dashes():
    # AC 8: no dollars and nothing to bill — the step did no model work at all.
    rows = build_summary(_fallback_events({
        "claude-sonnet-4-6": {"inputTokens": 0, "outputTokens": 0,
                              "cacheReadInputTokens": 0,
                              "cacheCreationInputTokens": 0,
                              "webSearchRequests": 0}}))
    assert cost_cell(rows[0].totals) == "—"
    hdr = SummaryHeader(title="f", quest_id="c", status="completed",
                        total_seconds=60.0)
    out = render_summary(rows, header=hdr, color=False, width=80)
    assert out[3].split() == ["reviewer", "1m", "0s", "—", "—", "—"]


def test_web_search_requests_are_priced_when_the_vendor_cost_is_absent():
    # AC 9(a): $10 per 1,000 requests — 12 requests are $0.12 on top of the
    # tokens' $0.195.
    searched = build_summary(_fallback_events({
        "claude-sonnet-4-6": {**_FALLBACK_USAGE["claude-sonnet-4-6"],
                              "webSearchRequests": 12}}))
    plain = build_summary(_fallback_events(_FALLBACK_USAGE))
    assert (searched[0].usage.dollars
            - plain[0].usage.dollars) == pytest.approx(0.12)


def test_a_search_only_row_with_no_vendor_cost_is_billable_work():
    # AC 9(b): a flat charge does not need a token to exist, so this row is
    # work — not the "did no model work" dash.
    rows = build_summary(_fallback_events({
        "claude-sonnet-4-6": {"inputTokens": 0, "outputTokens": 0,
                              "cacheReadInputTokens": 0,
                              "cacheCreationInputTokens": 0,
                              "webSearchRequests": 5}}))
    assert rows[0].usage.dollars == pytest.approx(0.05)
    assert cost_cell(rows[0].totals) == "$0.05*"


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
                            ("openai", "gpt-6.1-sol")):
        rows = build_summary([
            _started("coder", "build", "2026-01-01T00:00:00+00:00"),
            _llm("a1", "2026-01-01T00:00:01+00:00", provider=provider,
                 model=model, input=100, output=10),
            _completed("coder", "2026-01-01T00:01:00+00:00"),
        ])
        assert rows[0].usage.notional == 0, provider
        assert rows[0].usage.priced == 1, provider


def test_a_claude_row_with_no_auth_mode_is_notional():
    # AC 16(c): an old log records no auth mode, and unknown renders notional.
    rows = build_summary([
        _started("coder", "build", "2026-01-01T00:00:00+00:00"),
        _cc("a1", "2026-01-01T00:00:01+00:00"),
        _qr("a1", "2026-01-01T00:00:02+00:00", 0.42),
        _completed("coder", "2026-01-01T00:01:00+00:00"),
    ])
    assert rows[0].usage.notional == 1


def test_a_claude_row_with_apikey_auth_is_metered():
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


@pytest.mark.parametrize("usage, expected", [
    # No model work at all: the counters are the signal, not the dollar figure.
    (RowUsage(fresh_input=5), "—"),
    (RowUsage(), "—"),
    # Metered — API-key auth or a structurally key-only provider.
    (RowUsage(dollars=6.43, priced=1), "~$6.43"),
    (RowUsage(dollars=0.004, priced=1), "~<$0.01"),
    # Notional — subscription, or an old log with no recoverable auth mode.
    (RowUsage(dollars=6.43, priced=1, notional=1), "$6.43*"),
    (RowUsage(dollars=0.004, priced=1, notional=1), "<$0.01*"),
    # Model work imp could not price at all.
    (RowUsage(fresh_input=900, output=90, unpriced=1), "n/a"),
    # A floor: something was priced, something else was not.
    (RowUsage(dollars=6.43, priced=1, unpriced=1), "≥~$6.43"),
    (RowUsage(dollars=6.43, priced=2, unpriced=1, notional=1), "≥$6.43*"),
])
def test_cost_cell_states(usage, expected):
    assert cost_cell(usage) == expected


def test_legend_lines_follow_the_cells_actually_rendered():
    assert legend_lines(["—", "n/a"]) == []
    assert legend_lines(["~$1.00"]) == [
        "~ estimated at API list price"]
    # Mixed auth: both lines, tilde first (spec §7 / AC 18).
    assert legend_lines(["~$1.00", "$2.00*", "≥$3.00*"]) == [
        "~ estimated at API list price",
        "* theoretical API list price; actual subscription usage is much lower",
        "≥ excludes steps run on a model absent from the price table"]



def test_dur_text_running_when_none_else_compact():
    assert _dur_text(None) == "running"
    assert _dur_text(90.0) == "1m 30s"


def test_sub_body_with_and_without_label():
    assert _sub_body(SubagentRow(name="explorer")) == "↳ explorer"
    assert _sub_body(SubagentRow(name="explorer", label="map repo")) \
        == "↳ explorer (map repo)"


def test_summary_totals_subtracts_the_exclusion_union_and_rolls_up_steps_and_subs():
    rows = [
        StepRow(phase="implement", step="task-lead",
                start_ts="2026-01-01T00:00:00+00:00",
                end_ts="2026-01-01T00:02:00+00:00",
                usage=RowUsage(fresh_input=1000, output=40, dollars=0.20,
                               priced=1),
                subagents=[SubagentRow(
                    name="coder",
                    usage=RowUsage(fresh_input=500, output=10, dollars=0.05,
                                   priced=1))]),
    ]
    work, total = summary_totals(rows, 120.0, Exclusions(
        (("2026-01-01T00:00:15+00:00", "2026-01-01T00:01:45+00:00"),)))
    assert work == 30.0                          # 120s total - 90s excluded
    assert total.input_total == 1500             # 1000 step + 500 subagent
    assert total.output == 50                    # 40 step + 10 subagent
    assert total.dollars == pytest.approx(0.25)  # 0.20 step + 0.05 subagent
    assert (total.priced, total.notional) == (2, 0)


def test_summary_totals_floors_work_at_zero():
    rows = [StepRow(phase="p", step="s",
                    start_ts="2026-01-01T00:00:00+00:00",
                    end_ts="2026-01-01T00:00:10+00:00")]
    work, _total = summary_totals(rows, 10.0, Exclusions(
        (("2026-01-01T00:00:00+00:00", "2026-01-01T00:00:30+00:00"),)))
    assert work == 0.0


def test_summary_totals_deducts_a_gap_no_step_row_covers():
    # AC 16(d): the run was parked between the two steps, so the pause falls
    # outside both rows' spans and only the run-level union sees it.
    rows = [
        StepRow(phase="spec", step="spec-writer",
                start_ts="2026-01-01T00:00:00+00:00",
                end_ts="2026-01-01T00:01:00+00:00"),
        StepRow(phase="plan", step="plan-writer",
                start_ts="2026-01-01T00:06:30+00:00",
                end_ts="2026-01-01T00:08:00+00:00"),
    ]
    work, _total = summary_totals(rows, 480.0, Exclusions(
        (("2026-01-01T00:01:30+00:00", "2026-01-01T00:06:30+00:00"),)))
    assert work == 180.0                         # 8m wall - 5m parked


def test_summary_totals_none_total_gives_none_work():
    work, total = summary_totals([], None)
    assert work is None
    assert total == RowUsage()


def test_step_row_shows_token_and_cost_columns():
    row = StepRow(phase="plan", step="plan-writer",
                  start_ts="2026-01-01T00:00:00+00:00",
                  end_ts="2026-01-01T00:01:30+00:00",
                  usage=RowUsage(fresh_input=12000, output=345, dollars=0.34,
                                 priced=1))
    out = render_summary([row], header=_HDR, color=False, width=80)
    line = next(ln for ln in out if "plan-writer" in ln)
    assert line == "  plan-writer  1m 30s  12.0k  345  ~$0.34"


def test_cost_column_is_a_dash_when_the_step_did_no_model_work():
    # No priced and no unpriced contribution: the row ran no model at all.
    row = StepRow(phase="plan", step="s",
                  start_ts="2026-01-01T00:00:00+00:00",
                  end_ts="2026-01-01T00:00:10+00:00",
                  usage=RowUsage(fresh_input=5))
    out = render_summary([row], header=_HDR, color=False, width=80)
    line = next(ln for ln in out if "10s" in ln)
    assert line == "  s         10s   5    —     —"


def test_duration_renders_in_step_hue():
    rows = [_row("plan", "plan-writer", 90)]
    out = render_summary(rows, header=_HDR, color=True, width=80)
    line = next(ln for ln in out if "plan-writer" in ln)
    assert "\x1b[94m1m 30s\x1b[0m" in line   # step hue (ansi bright blue) on the duration


def test_ansi_summary_phase_and_step_rows_are_bright_blue():
    rows = [_row("plan", "plan-writer", 90), _row("plan", "plan-review", 30)]
    pal.set_theme("ansi")
    out = render_summary(rows, header=_HDR, color=True, width=80)
    assert "\x1b[1m\x1b[94mplan phase\x1b[0m" in out
    review = next(ln for ln in out if "plan-review" in ln)
    assert review.startswith("  \x1b[94mplan-review")
    assert "\x1b[94m   30s\x1b[0m" in review
    assert not any("\x1b[34m" in ln for ln in out)


def test_token_and_cost_columns_carry_no_color():
    row = StepRow(phase="plan", step="plan-writer",
                  start_ts="2026-01-01T00:00:00+00:00",
                  end_ts="2026-01-01T00:01:30+00:00",
                  usage=RowUsage(fresh_input=12000, output=345, dollars=0.34,
                                 priced=1))
    out = render_summary([row], header=_HDR, color=True, width=80)
    line = next(ln for ln in out if "plan-writer" in ln)
    # Duration is hued for contrast, but the token/cost columns stay default
    # foreground: the line ends with the plain "12.0k  345  ~$0.34" and no SGR reset.
    assert line.endswith("  12.0k  345  ~$0.34")
    assert "\x1b[94m1m 30s\x1b[0m" in line


def test_errored_step_duration_follows_error_color():
    rows = [_row("implement", "task-lead", 10, errored=True)]
    out = render_summary(rows, header=_HDR, color=True, width=80)
    line = next(ln for ln in out if "task-lead" in ln)
    assert "\x1b[1m\x1b[31m   10s\x1b[0m" in line


def test_header_carries_identity_only_and_the_total_row_carries_the_numbers():
    rows = [StepRow(phase="implement", step="verify",
                    start_ts="2026-01-01T00:00:00+00:00",
                    end_ts="2026-01-01T00:02:00+00:00", wait_seconds=90.0,
                    usage=RowUsage(fresh_input=1000, output=40, dollars=0.20,
                                   priced=1))]
    hdr = SummaryHeader(title="f", quest_id="c", status="completed",
                        total_seconds=120.0,
                        exclusions=Exclusions(
                            (("2026-01-01T00:00:15+00:00",
                              "2026-01-01T00:01:45+00:00"),)))
    out = render_summary(rows, header=hdr, color=False, width=80)
    assert out[0] == "c  ·  f  ·  completed"     # AC 19: identity only
    assert "$" not in out[0]
    assert "1.0k" not in out[0]
    total = next(ln for ln in out if ln.startswith("  total"))
    assert total == "  total    30s  1.0k   40  ~$0.20"  # 120s - 90s wait


def test_total_row_is_the_sum_of_the_step_rows_including_subagents():
    rows = [
        StepRow(phase="plan", step="a",
                start_ts="2026-01-01T00:00:00+00:00",
                end_ts="2026-01-01T00:00:30+00:00",
                usage=RowUsage(fresh_input=1000, output=40, dollars=0.20,
                               priced=1)),
        StepRow(phase="plan", step="b",
                start_ts="2026-01-01T00:00:30+00:00",
                end_ts="2026-01-01T00:01:00+00:00",
                usage=RowUsage(fresh_input=1000, output=10, dollars=0.10,
                               priced=1),
                subagents=[SubagentRow(
                    name="coder",
                    start_ts="2026-01-01T00:00:35+00:00",
                    end_ts="2026-01-01T00:00:55+00:00",
                    usage=RowUsage(fresh_input=500, output=10, dollars=0.20,
                                   priced=1))]),
    ]
    hdr = SummaryHeader(title="f", quest_id="c", status="completed",
                        total_seconds=60.0)
    out = render_summary(rows, header=hdr, color=False, width=80)
    # AC 19/20: 1000 + (1000 + 500) in, 40 + (10 + 10) out, 0.20 + 0.30 cost.
    assert next(ln for ln in out if ln.startswith("  total")) \
        == "  total      1m 0s  2.5k   60  ~$0.50"
    # Step b's own row already folds its subagent in; the subagent still shows
    # its own figures on the indented line.
    assert next(ln for ln in out if ln.startswith("  b ")) \
        == "  b            30s  1.5k   20  ~$0.30"
    assert next(ln for ln in out if "coder" in ln) \
        == "    ↳ coder    20s   500   10  ~$0.20"


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
    assert step_line == "  task-lead      1m 0s  300    —  ~$0.15"
    assert sub_line == "    ↳ explorer     45s  200    —  ~$0.05"


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


def test_subagent_duration_recedes_while_its_name_keeps_magenta():
    step = StepRow(phase="implement", step="task-lead",
                   start_ts="2026-01-01T00:00:00+00:00",
                   end_ts="2026-01-01T00:01:00+00:00",
                   subagents=[SubagentRow(
                       name="explorer",
                       start_ts="2026-01-01T00:00:05+00:00",
                       end_ts="2026-01-01T00:00:50+00:00")])
    out = render_summary([step], header=_HDR, color=True, width=80)
    sub_line = next(ln for ln in out if "explorer" in ln)
    assert "\x1b[35m↳ explorer\x1b[0m" in sub_line   # magenta subagent name
    assert "\x1b[2m   45s\x1b[0m" in sub_line        # dim subagent duration
    assert "\x1b[35m   45s\x1b[0m" not in sub_line


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
    assert "\x1b[1m\x1b[31m   45s\x1b[0m" in sub_line


def test_header_merge_blocked_renders_two_words_bold():
    hdr = SummaryHeader(title="feature", quest_id="abc",
                        status="merge_blocked", total_seconds=100.0)
    out = render_summary([], header=hdr, color=True, width=80)
    assert "\x1b[1mmerge blocked\x1b[0m" in out[0]   # bold weight, two-word text
    assert "merge_blocked" not in out[0]


def test_header_waiting_renders_bold():
    hdr = SummaryHeader(title="feature", quest_id="abc",
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
    ("completed", "interrupted"),
    ("abandoned", "interrupted"),
    ("running", "running"),
    ("waiting", "waiting"),
    ("paused", "paused"),
    ("new", "new"),
    ("error", "error"),
    ("archived", "archived"),          # parked, not finished: keeps its status word
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
    assert out[3:5] == [
        "  task-lead   ~5m 0s   —    —     —",
        "    ↳ coder  ~2m 14s   —    —     —",
    ]


def test_open_spans_in_a_finished_quest_read_interrupted():
    step = StepRow(phase="implement", step="task-lead",
                   start_ts="2026-01-01T00:00:00+00:00", end_ts=None,
                   subagents=[SubagentRow(name="coder",
                                          start_ts="2026-01-01T00:00:10+00:00")])
    out = render_summary([step], header=_HDR, color=False, width=80)   # _HDR is completed
    assert out[3:5] == [
        "  task-lead  interrupted   —    —     —",
        "    ↳ coder  interrupted   —    —     —",
    ]
    assert "completed" not in "\n".join(out[1:])   # the status word stays in the header


@pytest.mark.parametrize("status", ["abandoned"])
def test_other_finished_statuses_also_read_interrupted(status):
    hdr = SummaryHeader(title="feature", quest_id="abc", status=status,
                        total_seconds=100.0)
    out = render_summary([_row("implement", "task-lead", 0, open_=True)],
                         header=hdr, color=False, width=80)
    assert "  task-lead  interrupted" in out[3]
    assert status not in out[3]


def test_merge_blocked_open_step_reads_two_words():
    hdr = SummaryHeader(title="feature", quest_id="abc",
                        status="merge_blocked", total_seconds=100.0)
    out = render_summary([_row("implement", "task-lead", 0, open_=True)],
                         header=hdr, color=False, width=80)
    assert "  task-lead  merge blocked" in out[3]
    assert "merge_blocked" not in out[3]


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
    assert out[3:7] == [
        "  a                1m 0s   —    —     —",
        "    ↳ coder          45s   —    —     —",
        "  b                 ~45s   —    —     —",
        "  c          interrupted   —    —     —",
    ]
    # titles, every row and the totals line share one column grid
    assert len({len(ln) for ln in [out[1], *out[3:7], out[8]]}) == 1


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


def test_f2_a_subagents_usage_folds_into_the_quest_total():
    # Re-points test_narrative.py's F2 assertions (a subagent's tokens and its
    # query_result cost fold into the cumulative total) at the accounting path.
    rows = build_summary([
        _started("task-lead", "implement", "2026-01-01T00:00:00+00:00"),
        _cc("lead1", "2026-01-01T00:00:01+00:00"),
        _subcall("tk1", "blk1", "lead1", "2026-01-01T00:00:05+00:00", "reviewer"),
        _qr("lead1", "2026-01-01T00:00:06+00:00", 0.01, session="s1",
            model_usage={"claude-sonnet-4-6": {
                "inputTokens": 100, "outputTokens": 5,
                "cacheReadInputTokens": 0, "cacheCreationInputTokens": 0}}),
        _cc("sub:tk1", "2026-01-01T00:00:07+00:00"),
        _qr("sub:tk1", "2026-01-01T00:00:08+00:00", 0.02, session="s2",
            model_usage={"claude-sonnet-4-6": {
                "inputTokens": 900, "outputTokens": 7,
                "cacheReadInputTokens": 0, "cacheCreationInputTokens": 0}}),
        _subresult("blk1", "lead1", "2026-01-01T00:00:09+00:00"),
        _completed("task-lead", "2026-01-01T00:00:10+00:00"),
    ])
    _work, total = summary_totals(rows, 10.0)
    assert rows[0].totals.fresh_input == 1000   # 100 parent + 900 subagent
    assert rows[0].totals.output == 12
    assert total.input_total + total.output == 1012
    assert total.dollars == pytest.approx(0.03)  # 0.01 parent + 0.02 subagent


def _openai_msg(agent_id: str, ts: str, *, input: int, output: int) -> dict:
    """The nested ResponseUsage shape the raw openai path records."""
    return {"type": "agent_message", "agent_id": agent_id, "timestamp": ts,
            "payload": {"provider": "openai", "model": "gpt-6.1-sol",
                        "usage": {"input_tokens": input,
                                  "output_tokens": output,
                                  "input_tokens_details": {"cached_tokens": 0},
                                  "output_tokens_details": {
                                      "reasoning_tokens": 0}}}}


def _one_step(*events: dict) -> StepRow:
    rows = build_summary([
        _started("s", "p", "2026-01-01T00:00:00+00:00"),
        *events,
        _completed("s", "2026-01-01T00:00:20+00:00"),
    ])
    return rows[0]


def test_metered_marker_reads_anthropic_and_openai_from_provider_alone():
    # AC 16(a): neither payload carries auth_mode; both are key-only providers.
    anthropic = _one_step(_llm("a1", "2026-01-01T00:00:05+00:00",
                               input=100, output=10))
    assert anthropic.totals.notional == 0            # metered from provider
    # sonnet: 100*$3 + 10*$15 per million = $0.00045 -> sub-cent tilde form
    assert cost_cell(anthropic.totals) == "~<$0.01"

    openai = _one_step(_openai_msg("a1", "2026-01-01T00:00:05+00:00",
                                   input=100_000, output=10_000))
    # sol: 100k*$2 + 10k*$10 per million = $0.30, prompt 100k is under 272k
    assert cost_cell(openai.totals) == "~$0.30"


def test_an_openai_request_over_272k_is_surcharged_end_to_end():
    # AC 7(a): boundary is exclusive — 272k is not surcharged, 272k+1 is.
    # Unsurcharged: 272000*$2/1e6 + 10000*$10/1e6 = $0.544 + $0.100 = $0.644
    # Surcharged:   272001*$2*2/1e6 + 10000*$10*1.5/1e6 = $1.088004 + $0.150 = $1.238004
    at_boundary = _one_step(_openai_msg("a1", "2026-01-01T00:00:05+00:00",
                                        input=272_000, output=10_000))
    over_boundary = _one_step(_openai_msg("a1", "2026-01-01T00:00:05+00:00",
                                          input=272_001, output=10_000))
    assert at_boundary.totals.dollars == pytest.approx(0.644)
    assert over_boundary.totals.dollars == pytest.approx(1.238004)


def test_metered_marker_reads_auth_mode_on_codex_and_claude():
    apikey = _one_step(_codex("a1", "2026-01-01T00:00:05+00:00",
                              auth_mode="apikey"),
                       _patch("a1", input=100_000, output=10_000,
                              context_tokens=100_000))
    assert cost_cell(apikey.totals) == "~$0.30"      # AC 16(b)

    subscription = _one_step(_codex("a1", "2026-01-01T00:00:05+00:00"),
                             _patch("a1", input=100_000, output=10_000,
                                    context_tokens=100_000))
    assert cost_cell(subscription.totals) == "$0.30*"

    old_log = _one_step(
        _cc("a1", "2026-01-01T00:00:05+00:00"),
        _qr("a1", "2026-01-01T00:00:06+00:00", 6.43, model_usage={
            "claude-sonnet-4-6": {"inputTokens": 100, "outputTokens": 10,
                                  "cacheReadInputTokens": 0,
                                  "cacheCreationInputTokens": 0}}))
    assert cost_cell(old_log.totals) == "$6.43*"     # AC 16(c)


def test_an_unpriced_model_renders_n_a_and_floors_the_aggregate():
    # AC 15 / AC 17(b): the model is absent from the price table, so its tokens
    # count but its dollars cannot; the aggregate says so rather than shrinking.
    priced = StepRow(phase="p", step="a",
                     start_ts="2026-01-01T00:00:00+00:00",
                     end_ts="2026-01-01T00:00:30+00:00",
                     usage=RowUsage(fresh_input=100, output=10, dollars=2.00,
                                    priced=1))
    unpriced = _one_step(_llm("a1", "2026-01-01T00:00:05+00:00",
                              input=900, output=90, model="claude-mythic-9"))
    assert cost_cell(unpriced.totals) == "n/a"
    _work, total = summary_totals([priced, unpriced], 60.0)
    assert cost_cell(total) == "≥~$2.00"


def test_a_step_row_mixing_vendors_stars_while_the_leaf_keeps_its_form():
    # AC 17(c): a metered subagent under a notional parent. The step row
    # aggregates both and is not a metered figure; the subagent row is.
    step = StepRow(phase="build", step="coder",
                   start_ts="2026-01-01T00:00:00+00:00",
                   end_ts="2026-01-01T00:01:00+00:00",
                   usage=RowUsage(fresh_input=100, output=10, dollars=1.00,
                                  priced=1, notional=1),
                   subagents=[SubagentRow(
                       name="explorer",
                       start_ts="2026-01-01T00:00:05+00:00",
                       end_ts="2026-01-01T00:00:50+00:00",
                       usage=RowUsage(fresh_input=50, output=5, dollars=0.50,
                                      priced=1))])
    assert cost_cell(step.totals) == "$1.50*"
    assert cost_cell(step.subagents[0].usage) == "~$0.50"


def test_title_row_is_dim_and_absent_when_there_are_no_step_rows():
    # AC 21: the dim title row appears with the first step row, not before it.
    # The totals row still renders, so a range with no step rows keeps its time.
    assert render_summary([], header=_HDR, color=False, width=80) == [
        "abc  ·  feature  ·  completed",
        "",
        "  total  39m 2s   —    —     —",
    ]
    out = render_summary([_row("plan", "plan-writer", 90)], header=_HDR,
                         color=True, width=80)
    assert out[1] == "\x1b[2m                 time  in  out  cost\x1b[0m"


def test_a_range_with_no_step_rows_still_reports_the_runs_time():
    # `imp log -s <id> 1-3` on a window with no step_started still has a span:
    # main.py:2486 selects events before the summary branch reads them.
    hdr = SummaryHeader(title="default", quest_id="abc",
                        status="running", total_seconds=2342.0)
    assert render_summary([], header=hdr, color=False, width=80)[-1] \
        == "  total  39m 2s   —    —     —"
    # No span recorded at all: the duration reads open, not a fabricated zero.
    blank = SummaryHeader(title="default", quest_id="abc",
                          status="running", total_seconds=None)
    assert render_summary([], header=blank, color=False, width=80)[-1] \
        == "  total  running   —    —     —"


def test_the_reference_after_state_renders_verbatim():
    # The spec's §7 reference block for a subscription run, byte for byte.
    def _sub_row(name, label, secs, usage):
        return SubagentRow(
            name=name, label=label,
            start_ts="2026-01-01T00:00:00+00:00",
            end_ts=f"2026-01-01T00:{secs // 60:02d}:{secs % 60:02d}+00:00",
            usage=usage)

    def _step(phase, step, secs, usage, subs=()):
        return StepRow(
            phase=phase, step=step,
            start_ts="2026-01-01T00:00:00+00:00",
            end_ts=f"2026-01-01T00:{secs // 60:02d}:{secs % 60:02d}+00:00",
            usage=usage, subagents=list(subs))

    notional = dict(priced=1, notional=1)
    rows = [
        _step("scoping", "threshold", 720,
              RowUsage(fresh_input=163_000, output=3_000, dollars=0.27,
                       **notional)),
        _step("build", "coder", 2160,
              RowUsage(fresh_input=3_350_000, output=60_000, dollars=5.76,
                       **notional),
              [_sub_row("explorer", "exploring codebase", 420,
                        RowUsage(fresh_input=350_000, output=4_000,
                                 dollars=0.40, **notional))]),
        _step("integration", "integrate", 120, RowUsage()),
    ]
    hdr = SummaryHeader(title="", quest_id="zita45",
                        status="completed", total_seconds=3120.0)
    assert render_summary(rows, header=hdr, color=False, width=80) == [
        "zita45  ·  (untitled)  ·  completed",
        "                                       time      in    out    cost",
        "scoping phase",
        "  threshold                          12m 0s  163.0k   3.0k  $0.27*",
        "build phase",
        "  coder                              36m 0s    3.7M  64.0k  $6.16*",
        "    ↳ explorer (exploring codebase)   7m 0s  350.0k   4.0k  $0.40*",
        "integration phase",
        "  integrate                           2m 0s       —      —       —",
        "",
        "  total                              52m 0s    3.9M  67.0k  $6.43*",
        "  * theoretical API list price; actual subscription usage is much lower",
    ]


def test_a_mixed_auth_run_shows_both_legend_lines_tilde_first():
    # AC 18: a metered step and a notional step; the total stars (AC 17a).
    rows = [
        StepRow(phase="p", step="metered",
                start_ts="2026-01-01T00:00:00+00:00",
                end_ts="2026-01-01T00:00:30+00:00",
                usage=RowUsage(fresh_input=100, output=10, dollars=2.00,
                               priced=1)),
        StepRow(phase="p", step="subscription",
                start_ts="2026-01-01T00:00:30+00:00",
                end_ts="2026-01-01T00:01:00+00:00",
                usage=RowUsage(fresh_input=100, output=10, dollars=4.00,
                               priced=1, notional=1)),
    ]
    hdr = SummaryHeader(title="f", quest_id="c", status="completed",
                        total_seconds=60.0)
    out = render_summary(rows, header=hdr, color=False, width=80)
    assert out[3].endswith("~$2.00")
    assert out[4].endswith("$4.00*")
    assert out[-3].endswith("$6.00*")           # the total stars under a mix
    assert out[-2:] == [
        "  ~ estimated at API list price",
        "  * theoretical API list price; actual subscription usage is much lower",
    ]


def test_an_unpriced_step_floors_the_total_and_adds_the_third_legend_line():
    # AC 17(b): one step ran on a model absent from the price table.
    rows = [
        StepRow(phase="p", step="a", start_ts="2026-01-01T00:00:00+00:00",
                end_ts="2026-01-01T00:00:30+00:00",
                usage=RowUsage(fresh_input=100, output=10, dollars=2.00,
                               priced=1)),
        StepRow(phase="p", step="b", start_ts="2026-01-01T00:00:30+00:00",
                end_ts="2026-01-01T00:01:00+00:00",
                usage=RowUsage(fresh_input=900, output=90, unpriced=1)),
    ]
    hdr = SummaryHeader(title="f", quest_id="c", status="completed",
                        total_seconds=60.0)
    out = render_summary(rows, header=hdr, color=False, width=80)
    assert out[4].endswith("n/a")
    assert out[-3] == "  total  1m 0s  1.0k  100  ≥~$2.00"
    assert out[-2:] == [
        "  ~ estimated at API list price",
        "  ≥ excludes steps run on a model absent from the price table",
    ]


def _quota_reading(ts: str = "2026-01-01T00:00:30+00:00") -> dict:
    return {"type": "quota_reading", "timestamp": ts,
            "payload": {"provider": "claude", "plan": None,
                        "windows": [{"label": "5h", "used_pct": 44.0,
                                     "resets_at": 100},
                                    {"label": "7d", "used_pct": 7.0,
                                     "resets_at": 900}],
                        "raw": {}}}


def test_the_summary_has_no_quota_input_and_renders_no_quota_output():
    events = _fallback_events(_FALLBACK_USAGE)
    events.append(_quota_reading())
    lines = render_summary(build_summary(events), header=_HDR,
                           color=False, width=80)
    assert not any("quota" in ln or "5h" in ln or "7d" in ln for ln in lines)
    assert lines[-2].startswith("  total")
    assert lines[-1] == ("  * theoretical API list price; actual subscription"
                         " usage is much lower")



def test_build_summary_returns_the_step_rows_directly():
    rows = build_summary([
        _started("plan-writer", "plan", "2026-01-01T00:00:00+00:00"),
        _completed("plan-writer", "2026-01-01T00:01:30+00:00"),
    ])
    assert isinstance(rows, list)
    assert [(r.phase, r.step) for r in rows] == [("plan", "plan-writer")]
    assert rows[0].wall_seconds == 90.0


@pytest.mark.parametrize("color", [False, True], ids=["plain", "color"])
def test_suppressing_the_header_line_drops_exactly_that_line(color: bool) -> None:
    """The card states the id and the status itself, so it renders the table
    without the run header — one line fewer, every other line identical."""
    rows = [_row("plan", "plan-writer", 90)]
    full = render_summary(rows, header=_HDR, color=color, width=80)
    bare = render_summary(rows, header=_HDR, color=color, width=80,
                          header_line=False)
    assert bare == full[1:]
    assert len(bare) == len(full) - 1
    assert not any("feature" in ln or "completed" in ln for ln in bare)
    assert "cost" in bare[0]          # the dim title row is now first
    assert bare[-1].startswith("  total")


def _started_labeled(step: str, phase: str, ts: str, label: str | None,
                     phase_label: str | None = None) -> dict:
    event = _started(step, phase, ts)
    event["payload"]["label"] = label
    if phase_label is not None:
        event["payload"]["phase_label"] = phase_label
    return event


def _label_pushed(label: str, ts: str) -> dict:
    return {"type": "step_label_updated", "timestamp": ts, "agent_id": "lead",
            "payload": {"label": label}}


def test_row_reads_its_labels_from_the_started_payload():
    row = build_summary([
        _started_labeled("threshold", "threshold", "2026-01-01T00:00:00+00:00",
                         "chatting", "scoping phase"),
    ])[0]
    assert (row.step_text, row.phase_text) == ("chatting", "scoping phase")
    assert (row.step, row.phase) == ("threshold", "threshold")


def test_unlabeled_row_shows_its_raw_id_and_the_phase_fallback():
    row = build_summary([
        _started_labeled("threshold", "threshold", "2026-01-01T00:00:00+00:00", None),
    ])[0]
    assert (row.step_text, row.phase_text) == ("threshold", "threshold phase")


def test_label_push_moves_the_open_row_to_the_last_task_reached():
    rows = build_summary([
        _started_labeled("task-lead", "implement", "2026-01-01T00:00:00+00:00", "task 1/3"),
        _label_pushed("task 2/3", "2026-01-01T00:01:00+00:00"),
        _label_pushed("task 3/3", "2026-01-01T00:02:00+00:00"),
        _completed("task-lead", "2026-01-01T00:03:00+00:00"),
    ])
    assert [r.step_text for r in rows] == ["task 3/3"]


def test_label_push_with_no_open_row_changes_nothing():
    rows = build_summary([
        _label_pushed("before any step", "2026-01-01T00:00:00+00:00"),
        _started_labeled("task-lead", "implement", "2026-01-01T00:00:01+00:00", "task 1/1"),
        _completed("task-lead", "2026-01-01T00:01:00+00:00"),
        _label_pushed("after the step", "2026-01-01T00:01:01+00:00"),
    ])
    assert [r.step_text for r in rows] == ["task 1/1"]


def test_resume_reentry_keeps_the_rows_label():
    rows = build_summary([
        _started_labeled("task-lead", "implement", "2026-01-01T00:00:00+00:00", "task 1/2"),
        _label_pushed("task 2/2", "2026-01-01T00:01:00+00:00"),
        _started_labeled("task-lead", "implement", "2026-01-01T00:02:00+00:00", "task 1/2"),
    ])
    assert [r.step_text for r in rows] == ["task 2/2"]


def test_resume_reentry_fills_only_an_empty_step_label():
    rows = build_summary([
        _started_labeled("threshold", "threshold", "2026-01-01T00:00:00+00:00", None),
        _started_labeled("threshold", "threshold", "2026-01-01T00:01:00+00:00",
                         "chatting", "scoping phase"),
    ])
    assert [(r.step_text, r.phase_text) for r in rows] == [("chatting", "threshold phase")]


def test_row_key_ignores_the_label():
    ts = "2026-01-01T00:00:00+00:00"
    plain = build_summary([_started("task-lead", "implement", ts)])[0]
    labeled = build_summary([
        _started_labeled("task-lead", "implement", ts, "task 1/2", "implementation phase"),
        _label_pushed("task 2/2", "2026-01-01T00:01:00+00:00"),
    ])[0]
    assert labeled.key == plain.key == f"step:implement/task-lead@{ts}"


def test_numeric_columns_stay_aligned_with_the_longest_label():
    rows = [_row("spec", "spec-finalizer", 90, label="addressing spec review feedback"),
            _row("spec", "verify", 30)]
    assert render_summary(rows, header=_HDR, color=False, width=80) == [
        "abc  ·  feature  ·  completed",
        "                                     time  in  out  cost",
        "spec phase",
        "  addressing spec review feedback  1m 30s   —    —     —",
        "  verify                              30s   —    —     —",
        "",
        "  total                            39m 2s   —    —     —",
    ]


def test_rows_sharing_a_raw_phase_stay_one_group_under_the_first_heading():
    rows = [_row("plan", "plan-writer", 90, phase_label="planning phase · plan 1/3"),
            _row("plan", "plan-review", 30, phase_label="planning phase · plan 2/3")]
    lines = render_summary(rows, header=_HDR, color=False, width=80)
    assert "planning phase · plan 1/3" in lines
    assert not any("plan 2/3" in ln for ln in lines)


_START = "2026-01-01T00:00:00+00:00"


def _instant(step: str, *, usage: RowUsage | None = None,
             subagents: list[SubagentRow] | None = None) -> StepRow:
    return StepRow(phase="plan", step=step, start_ts=_START, end_ts=_START,
                   usage=usage or RowUsage(), subagents=subagents or [])


@pytest.mark.parametrize(("row", "idle"), [
    (_row("plan", "gate", 0), True),
    (_row("plan", "gate", 90, wait=89.5), True),
    (_row("plan", "gate", 1), False),
    (_row("plan", "gate", 0, errored=True), False),
    (_row("plan", "gate", 0, open_=True), False),
    (_instant("gate", usage=RowUsage(fresh_input=10, output=5, priced=1)), False),
    (_instant("gate", usage=RowUsage(fresh_input=10, output=5, unpriced=1)), False),
    (_instant("gate", subagents=[SubagentRow(name="explorer", start_ts=_START,
                                             end_ts=_START)]), False),
], ids=["instant", "under-a-second-of-work", "one-second", "errored", "open",
        "priced", "unpriced", "subagent"])
def test_step_row_idle_is_closed_instant_quiet_and_clean(row: StepRow, idle: bool) -> None:
    assert row.idle is idle


def test_hide_idle_drops_only_idle_rows_and_keeps_the_full_tables_layout() -> None:
    priced = _row("plan", "coder", 0)
    priced.usage = RowUsage(fresh_input=1000, output=100, dollars=0.01, priced=1)
    rows = [
        _row("plan", "plan-writer", 90),               # token-free, >=1s: shown
        _row("plan", "plan-finalized", 0),             # idle, and the widest name
        _row("plan", "plan-review", 0, errored=True),  # errored: shown
        priced,                                        # model work: shown
        _instant("dispatch", subagents=[SubagentRow(name="explorer", start_ts=_START,
                                                    end_ts=_START)]),
        _row("plan", "task-lead", 0, open_=True),      # open: shown
    ]
    full = render_summary(rows, header=_HDR, color=False, width=80)
    card = render_summary(rows, header=_HDR, color=False, width=80, hide_idle=True)
    assert any("plan-finalized" in ln for ln in full)
    assert card == [ln for ln in full if "plan-finalized" not in ln]


def test_hide_idle_drops_the_header_of_an_all_idle_phase() -> None:
    rows = [
        _row("spec", "spec-writer", 30),
        _row("spec", "spec-commit", 0),
        _row("spec", "spec-review", 40),
        _row("merge", "rebase", 0),
        _row("merge", "pre-merge", 0),
        _row("implement", "verify", 20),
    ]
    full = render_summary(rows, header=_HDR, color=True, width=80)
    card = render_summary(rows, header=_HDR, color=True, width=80, hide_idle=True)
    hidden = ("spec-commit", "rebase", "pre-merge", "merge phase")
    assert all(any(h in ln for ln in full) for h in hidden)
    assert card == [ln for ln in full if not any(h in ln for h in hidden)]


def _header(*, title: str = "", intent: str | None = None, status: str = "paused",
            color: bool = False) -> str:
    hdr = SummaryHeader(quest_id="kole14", title=title, status=status,
                        total_seconds=None, intent=intent)
    return render_summary([], header=hdr, color=color, width=80)[0]


_GOLD_ID = pal.ansi("kole14", *pal.NODE_COLORS["quest_id"], enabled=True)


def test_header_leads_with_id_then_title_then_status():
    assert _header(title="imp doctor") == "kole14  ·  imp doctor  ·  paused"
    assert _header(title="imp doctor", color=True) == f"{_GOLD_ID}  ·  imp doctor  ·  paused"


def test_header_never_truncates_a_title():
    long_title = "t" * 90
    assert _header(title=long_title) == f"kole14  ·  {long_title}  ·  paused"


def test_titleless_header_falls_back_to_the_opening_message_truncated_and_dim():
    opening = "fix  the\nsummary " + "x" * 80          # flattened: "fix the summary xxx…"
    flat = " ".join(opening.split())
    shown = flat[:69] + "…"
    assert len(shown) == 70
    assert _header(intent=opening) == f"kole14  ·  {shown}  ·  paused"
    dim = pal.ansi(shown, "dim", enabled=True)
    assert _header(intent=opening, color=True) == f"{_GOLD_ID}  ·  {dim}  ·  paused"


def test_short_opening_message_is_not_truncated():
    assert _header(intent="fix it") == "kole14  ·  fix it  ·  paused"


def test_header_with_no_title_or_intent_reads_untitled_and_dim():
    assert _header() == "kole14  ·  (untitled)  ·  paused"
    dim = pal.ansi("(untitled)", "dim", enabled=True)
    assert _header(color=True) == f"{_GOLD_ID}  ·  {dim}  ·  paused"


def test_header_status_keeps_display_word_and_bold():
    bold = pal.ansi("merge blocked", "bold", enabled=True)
    assert (_header(title="t", status="merge_blocked", color=True)
            == f"{_GOLD_ID}  ·  t  ·  {bold}")


def _titled_rows(title: str | None, label: str | None = None) -> list[StepRow]:
    return build_summary(titled_subagent_events(title, label=label))


def _titled_ledger_line(title: str | None, label: str | None = None) -> str:
    out = render_summary(_titled_rows(title, label), header=_HDR, color=False, width=80)
    return next(ln for ln in out if "explorer" in ln)


def test_build_summary_reads_the_dispatch_title():
    assert _titled_rows("map ledger time totals")[0].subagents[0].title \
        == "map ledger time totals"
    assert _titled_rows(None)[0].subagents[0].title is None


def test_sub_body_prefers_the_title_over_the_label():
    assert _sub_body(SubagentRow(name="explorer", label="exploring codebase",
                                 title="map ledger time totals")) \
        == "↳ explorer (map ledger time totals)"
    assert _sub_body(SubagentRow(name="explorer", label="exploring codebase",
                                 title=" \n ")) == "↳ explorer (exploring codebase)"


def test_ledger_row_shows_the_title_else_the_label():
    assert "↳ explorer (map ledger time totals)" in _titled_ledger_line(
        "map ledger time totals", label="exploring codebase")
    assert "↳ explorer (exploring codebase)" in _titled_ledger_line(
        None, label="exploring codebase")
    assert "↳ explorer (map the ledger)" in _titled_ledger_line("  map   the\nledger ")


def test_ledger_row_caret_escapes_the_title():
    line = _titled_ledger_line("map\x1b[31mred")
    assert "↳ explorer (map^[[31mred)" in line
    assert "\x1b" not in line


def test_ledger_row_clips_a_long_title_and_keeps_columns_aligned():
    out = render_summary(_titled_rows("x" * 60), header=_HDR, color=False, width=80)
    sub = next(ln for ln in out if "explorer" in ln)
    assert "↳ explorer (" + "x" * 40 + "…)" in sub
    assert "x" * 41 not in sub
    step = next(ln for ln in out if ln.strip().startswith("spec"))
    total = next(ln for ln in out if ln.strip().startswith("total"))
    assert step.index("—") == sub.index("—") == total.index("—")
