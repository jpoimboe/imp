# tests/test_narrative_block_age.py
from __future__ import annotations

from imp.cli import narrative as nv


def test_activity_elapsed_zero_when_start_ts_missing() -> None:
    act = nv.Activity(verb="thinking")            # no start_ts
    assert nv.activity_elapsed(act, "2026-01-01T00:00:10+00:00") == 0.0


def test_activity_elapsed_counts_from_start_ts() -> None:
    act = nv.Activity(verb="thinking", start_ts="2026-01-01T00:00:00+00:00")
    assert nv.activity_elapsed(act, "2026-01-01T00:00:07+00:00") == 7.0


def test_activity_elapsed_excludes_source_backoff_beyond_base() -> None:
    # A builder that has accrued 4s of completed backoff; the block began when
    # 1s had already accrued, so only 3s of backoff falls inside the block.
    src = nv.NarrativeBuilder()
    src._backoff_wait_accum = 4.0
    act = nv.Activity(verb="thinking", start_ts="2026-01-01T00:00:00+00:00",
                      backoff_source=src, backoff_base=1.0)
    # 10s wall span − (4 − 1) within-block backoff = 7s honest.
    assert nv.activity_elapsed(act, "2026-01-01T00:00:10+00:00") == 7.0


def test_activity_elapsed_never_negative() -> None:
    src = nv.NarrativeBuilder()
    src._backoff_wait_accum = 100.0
    act = nv.Activity(verb="thinking", start_ts="2026-01-01T00:00:00+00:00",
                      backoff_source=src, backoff_base=0.0)
    assert nv.activity_elapsed(act, "2026-01-01T00:00:05+00:00") == 0.0


from tests._event_fixtures import ev


def _stepped(ts="2026-01-01T00:00:00+00:00") -> nv.NarrativeBuilder:
    b = nv.NarrativeBuilder()
    b.add(ev("1", "change_created", change_id=1, title="x"))
    b.add(ev("2", "step_started", step="s", kind="agent", phase="p",
             model="m", provider="anthropic", timestamp=ts))
    return b


def test_step_started_sets_stretch_start() -> None:
    b = _stepped("2026-01-01T00:00:03+00:00")
    assert b._stretch_start_ts == "2026-01-01T00:00:03+00:00"


def test_prose_thinking_and_tool_do_not_advance_stretch_start() -> None:
    b = _stepped("2026-01-01T00:00:03+00:00")
    b.add(ev("3", "thinking", agent_id="step:p/s", content="hmm",
             timestamp="2026-01-01T00:00:05+00:00"))
    b.add(ev("4", "tool_call", agent_id="step:p/s", id="c1", name="read_file",
             input={"p": "x"}, timestamp="2026-01-01T00:00:06+00:00"))
    b.add(ev("5", "llm_response", agent_id="step:p/s", provider="anthropic",
             stop_reason="end_turn", usage={},
             raw={"content": [{"type": "text", "text": "narration"}]},
             timestamp="2026-01-01T00:00:07+00:00"))
    # The stretch start is still the step_started timestamp — one continuous block.
    assert b._stretch_start_ts == "2026-01-01T00:00:03+00:00"


def test_step_label_update_does_not_advance_stretch_start() -> None:
    # The `task N/M` counter is a live-label update, not a new step; it must not
    # reset the block-age timer (AC4).
    b = _stepped("2026-01-01T00:00:03+00:00")
    b.add(ev("lbl", "step_label_updated", agent_id="step:p/s", label="task 2/3",
             timestamp="2026-01-01T00:00:18+00:00"))
    assert b._stretch_start_ts == "2026-01-01T00:00:03+00:00"


def test_user_message_sets_stretch_start() -> None:
    b = _stepped("2026-01-01T00:00:03+00:00")
    b.add(ev("6", "user_message", content=[{"type": "text", "text": "go"}],
             timestamp="2026-01-01T00:00:20+00:00"))
    assert b._stretch_start_ts == "2026-01-01T00:00:20+00:00"


def test_subagent_return_sets_stretch_start_when_last_fold_closes() -> None:
    b = _stepped("2026-01-01T00:00:03+00:00")
    b.add(ev("t1", "tool_call", agent_id="step:p/s", id="blk-t1", name="sub_agent",
             input={"subagent_name": "coder"}, timestamp="2026-01-01T00:00:04+00:00"))
    # Parent's next turn-end llm_response returns control -> resume-close fires and
    # the last running fold closes, so the parent regains the block at THIS ts.
    b.add(ev("p1", "llm_response", agent_id="step:p/s", provider="anthropic",
             stop_reason="end_turn", usage={},
             raw={"content": [{"type": "text", "text": "back"}]},
             timestamp="2026-01-01T00:00:44+00:00"))
    assert b.running_subagents() == []
    assert b._stretch_start_ts == "2026-01-01T00:00:44+00:00"


def test_waiting_for_test_lane_captures_wait_start_ts() -> None:
    b = nv.NarrativeBuilder()
    b.add(ev("1", "change_created", change_id=1, title="x"))
    b.add(ev("2", "step_started", step="integration-tests", kind="script",
             phase="integration", label="running tests",
             timestamp="2026-01-01T00:00:00+00:00"))
    b.add(ev("3", "waiting_for_test_lane", step="integration-tests",
             timestamp="2026-01-01T00:00:09+00:00"))
    assert b._step_stack[-1].lane_wait_start_ts == "2026-01-01T00:00:09+00:00"


def test_merge_lane_waiting_captures_wait_start_ts_once() -> None:
    b = nv.NarrativeBuilder()
    b.add(ev("1", "change_created", change_id=1, title="x"))
    b.add(ev("2", "step_started", step="merge", kind="merge", phase="convergence",
             timestamp="2026-01-01T00:00:00+00:00"))
    b.add(ev("3", "lane_waiting", ahead=2, timestamp="2026-01-01T00:00:12+00:00"))
    assert b._step_stack[-1].lane_wait_start_ts == "2026-01-01T00:00:12+00:00"
    # A re-emitted lane_waiting (updated `ahead` count) must NOT reset the clock.
    b.add(ev("4", "lane_waiting", ahead=1, timestamp="2026-01-01T00:00:20+00:00"))
    assert b._step_stack[-1].lane_wait_start_ts == "2026-01-01T00:00:12+00:00"
