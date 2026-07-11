# tests/test_narrative_block_age.py
from __future__ import annotations

from imp.cli import narrative as nv
from tests._event_fixtures import ev


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
    # 10s wall span - (4 - 1) within-block backoff = 7s honest.
    assert nv.activity_elapsed(act, "2026-01-01T00:00:10+00:00") == 7.0


def test_activity_elapsed_never_negative() -> None:
    src = nv.NarrativeBuilder()
    src._backoff_wait_accum = 100.0
    act = nv.Activity(verb="thinking", start_ts="2026-01-01T00:00:00+00:00",
                      backoff_source=src, backoff_base=0.0)
    assert nv.activity_elapsed(act, "2026-01-01T00:00:05+00:00") == 0.0


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


def test_subagent_tool_result_close_sets_stretch_start() -> None:
    # The COMMON production close path (the test above covers only resume-close):
    # the subagent's `task` tool_result — same block id as its dispatch — closes
    # the last running fold via _h_tool_result, so the parent regains the block
    # and a fresh stretch starts at the result's timestamp.
    b = _stepped("2026-01-01T00:00:03+00:00")
    b.add(ev("t1", "tool_call", agent_id="step:p/s", id="blk-t1", name="sub_agent",
             input={"subagent_name": "coder"}, timestamp="2026-01-01T00:00:04+00:00"))
    assert b.running_subagents()                          # fold open
    b.add(ev("tr1", "tool_result", agent_id="step:p/s", id="blk-t1", content="ok",
             timestamp="2026-01-01T00:00:44+00:00"))
    assert b.running_subagents() == []                    # canonical close fired
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


def _sub(*subs, start_ts="2026-01-01T00:00:00+00:00") -> nv.NarrativeBuilder:
    b = _stepped(start_ts)
    for eid, name in subs:
        b.add(ev(eid, "tool_call", agent_id="step:p/s", id=f"blk-{eid}",
                 name="sub_agent", input={"subagent_name": name},
                 timestamp=start_ts))
    return b


def test_single_subagent_activity_carries_fold_start_and_nested_source() -> None:
    b = _sub(("t1", "coder"))
    act = b.current_activity()
    fold = b.running_subagents()[0]
    assert act.start_ts == fold.start_ts
    assert act.backoff_source is fold.nested
    assert act.backoff_base == 0.0
    # And the derived elapsed matches the ↳-row formula exactly (AC1 basis).
    now = "2026-01-01T00:01:44+00:00"
    assert nv.activity_elapsed(act, now) == (
        nv.elapsed_seconds(fold.start_ts, now)
        - fold.nested.backoff_wait_seconds(now))


def test_running_n_activity_measures_from_earliest_fold() -> None:
    b = _stepped("2026-01-01T00:00:00+00:00")
    b.add(ev("t1", "tool_call", agent_id="step:p/s", id="blk-t1", name="sub_agent",
             input={"subagent_name": "coder"}, timestamp="2026-01-01T00:00:02+00:00"))
    b.add(ev("t2", "tool_call", agent_id="step:p/s", id="blk-t2", name="sub_agent",
             input={"subagent_name": "explorer"}, timestamp="2026-01-01T00:00:05+00:00"))
    act = b.current_activity()
    assert act.verb == "running 2 subagents"
    assert act.start_ts == "2026-01-01T00:00:02+00:00"   # earliest still-running fold
    earliest = min(b.running_subagents(), key=lambda fld: fld.start_ts or "9999")
    assert act.backoff_source is earliest.nested


def test_surfaced_waiting_subagent_countdown_anchors_to_nested_wait_entry() -> None:
    # Parent is NOT waiting (its own _backoff_wait_start is None); only the
    # subagent is. The countdown baseline must be the subagent's wait entry, not
    # a frozen full total from a None parent field (AC5b).
    b = _sub(("t1", "code-reviewer"))
    b.add(ev("r1", "agent_retry", agent_id="sub:t1", category="auth",
             attempt=1, backoff_seconds=30.0, timestamp="2026-01-01T00:00:05+00:00"))
    act = b.current_activity()
    assert b._backoff_wait_start is None                 # parent not waiting
    assert act.countdown_total == 30.0
    assert act.start_ts == "2026-01-01T00:00:05+00:00"   # the subagent's wait entry
    # 10s after wait entry -> 30 - 5 = 25s remaining is derivable from start_ts.
    now = "2026-01-01T00:00:10+00:00"
    assert 30.0 - nv.activity_elapsed(act, now) == 25.0


def test_parent_reset_wait_countdown_anchors_to_own_wait_entry() -> None:
    b = _stepped("2026-01-01T00:00:00+00:00")
    b.add(ev("3", "agent_retry", category="rate_limit", attempt=1,
             backoff_seconds=2940.0, hinted=True, timestamp="2026-01-01T00:00:08+00:00"))
    act = b.current_activity()
    assert act.countdown_total == 2940.0
    assert act.start_ts == "2026-01-01T00:00:08+00:00"   # self._backoff_wait_start


def test_parent_stretch_activity_uses_stretch_start_not_prose() -> None:
    b = _stepped("2026-01-01T00:00:03+00:00")
    b.add(ev("5", "llm_response", agent_id="step:p/s", provider="anthropic",
             stop_reason="end_turn", usage={},
             raw={"content": [{"type": "text", "text": "narration"}]},
             timestamp="2026-01-01T00:00:30+00:00"))
    act = b.current_activity()
    assert act.start_ts == "2026-01-01T00:00:03+00:00"   # stretch start, not the prose ts
    assert act.backoff_source is b


def test_test_lane_states_use_queue_and_run_timestamps() -> None:
    b = nv.NarrativeBuilder()
    b.add(ev("1", "change_created", change_id=1, title="x"))
    b.add(ev("2", "step_started", step="integration-tests", kind="script",
             phase="integration", label="running tests",
             timestamp="2026-01-01T00:00:00+00:00"))
    b.add(ev("3", "waiting_for_test_lane", step="integration-tests",
             timestamp="2026-01-01T00:00:09+00:00"))
    waiting = b.current_activity()
    assert waiting.start_ts == "2026-01-01T00:00:09+00:00"   # queue entry
    assert waiting.backoff_source is None
    b.add(ev("4", "test_lane_acquired", step="integration-tests",
             timestamp="2026-01-01T00:00:15+00:00"))
    running = b.current_activity()
    assert running.start_ts == "2026-01-01T00:00:15+00:00"   # slot acquisition
    assert running.backoff_source is None
