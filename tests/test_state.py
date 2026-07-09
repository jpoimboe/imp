import json
from pathlib import Path

from imp.engine.state import InMemoryEventLog, JsonlEventLog, State


def test_state_roundtrip_vars_and_loop_counts(tmp_path: Path) -> None:
    s = State(id="c1", step="spec-writer", step_attempt=0, status="new",
              vars={"review_done": False, "round": 0},
              loop_counts={"spec-reviewer": 2})
    s.save(tmp_path)
    back = State.load(tmp_path)
    assert back.step == "spec-writer"
    assert back.vars == {"review_done": False, "round": 0}
    assert back.loop_counts == {"spec-reviewer": 2}
    assert not hasattr(back, "phase")


def test_state_defaults(tmp_path: Path) -> None:
    s = State(id="c1", step=None, step_attempt=0, status="new")
    s.save(tmp_path)
    back = State.load(tmp_path)
    assert back.vars == {}
    assert back.loop_counts == {}


def test_state_round_trip(tmp_path: Path) -> None:
    s = State(
        id="c5",
        step="intent",
        step_attempt=1,
        status="new",
    )
    s.save(tmp_path)
    loaded = State.load(tmp_path)
    assert loaded == s


def test_state_atomic_overwrite(tmp_path: Path) -> None:
    s = State(
        id="c1", step=None, step_attempt=0,
        status="new",
    )
    s.save(tmp_path)
    s2 = State(
        id="c1", step="s", step_attempt=2,
        status="new",
    )
    s2.save(tmp_path)
    raw = json.loads((tmp_path / "state.json").read_text())
    assert raw["step"] == "s"
    assert raw["step_attempt"] == 2


def test_state_no_partial_writes(tmp_path: Path) -> None:
    s = State(
        id="c1", step=None, step_attempt=0,
        status="new",
    )
    s.save(tmp_path)
    # No leftover tmp file
    siblings = list(tmp_path.iterdir())
    assert {p.name for p in siblings} == {"state.json"}


def test_event_log_append_and_read(tmp_path: Path) -> None:
    log = JsonlEventLog(tmp_path / "events.jsonl")
    log.append(agent_id=None, type="phase_started", payload={"phase": "onboard"})
    log.append(agent_id=None, type="step_started",
               payload={"step": "intent-clarifier", "model": "claude-sonnet-4-6"})
    events = list(log.read())
    assert [e["type"] for e in events] == ["phase_started", "step_started"]
    assert events[0]["payload"]["phase"] == "onboard"
    assert events[0]["timestamp"]  # ISO-8601
    assert "id" in events[0] and "agent_id" in events[0]


def test_event_log_append_only(tmp_path: Path) -> None:
    log = JsonlEventLog(tmp_path / "events.jsonl")
    log.append(agent_id=None, type="a", payload={})
    log.append(agent_id=None, type="b", payload={})
    log.append(agent_id=None, type="c", payload={})
    raw_lines = (tmp_path / "events.jsonl").read_text().splitlines()
    assert len(raw_lines) == 3


def test_event_log_filter_by_step(tmp_path: Path) -> None:
    log = JsonlEventLog(tmp_path / "events.jsonl")
    log.append(agent_id=None, type="phase_started", payload={"phase": "p"})
    log.append(agent_id=None, type="step_started", payload={"step": "s1"})
    log.append(agent_id=None, type="agent_output", payload={"step": "s1", "text": "hi"})
    log.append(agent_id=None, type="step_completed", payload={"step": "s1", "action": "continue"})
    log.append(agent_id=None, type="step_started", payload={"step": "s2"})
    log.append(agent_id=None, type="agent_output", payload={"step": "s2", "text": "hello"})

    s1_events = list(log.events_for_current_step("s1"))
    # Slice from last 'step_started' for s1 to end.
    assert any(e["payload"].get("text") == "hi" for e in s1_events)
    assert not any(e["payload"].get("text") == "hello" for e in s1_events)


def test_state_round_trip_with_current_step(tmp_path: Path) -> None:
    from imp.engine.state import CurrentStep, State
    s = State(
        id="c5", step="coder", step_attempt=0,
        status="new",
        current_step=CurrentStep(transcript_dir=tmp_path / "steps/coder"),
    )
    s.save(tmp_path)
    loaded = State.load(tmp_path)
    assert loaded.current_step is not None
    assert loaded.current_step.transcript_dir == tmp_path / "steps/coder"


def test_state_round_trip_without_current_step(tmp_path: Path) -> None:
    from imp.engine.state import State
    s = State(
        id="c5", step=None, step_attempt=0,
        status="completed",
    )
    s.save(tmp_path)
    loaded = State.load(tmp_path)
    assert loaded.current_step is None


def test_state_round_trip_with_retry_feedback(tmp_path: Path) -> None:
    s = State(
        id="c7",
        step="coder",
        step_attempt=1,
        status="new",
        retry_feedback={"target_step": "coder", "text": "fix the lint error"},
    )
    s.save(tmp_path)
    loaded = State.load(tmp_path)
    assert loaded == s
    assert loaded.retry_feedback == {"target_step": "coder", "text": "fix the lint error"}


def test_state_round_trip_retry_feedback_defaults_none(tmp_path: Path) -> None:
    s = State(
        id="c1", step=None, step_attempt=0,
        status="new",
    )
    s.save(tmp_path)
    raw = json.loads((tmp_path / "state.json").read_text())
    assert raw["retry_feedback"] is None
    assert State.load(tmp_path).retry_feedback is None


def test_state_resume_pending_roundtrip(tmp_path: Path) -> None:
    s = State(id="c1", step="coder", step_attempt=0, status="new",
              resume_pending="coder")
    s.save(tmp_path)
    back = State.load(tmp_path)
    assert back.resume_pending == "coder"


def test_state_resume_pending_defaults_none() -> None:
    s = State(id="c1", step=None, step_attempt=0, status="new")
    assert s.resume_pending is None


def test_state_load_forward_compatible_without_resume_pending(tmp_path: Path) -> None:
    # An older state.json (written before this field existed) still loads.
    (tmp_path / "state.json").write_text(json.dumps({
        "id": "c1", "step": "coder", "step_attempt": 0, "status": "new",
        "vars": {}, "loop_counts": {}, "current_step": None, "retry_feedback": None,
    }))
    back = State.load(tmp_path)
    assert back.resume_pending is None


def test_state_resolved_tiers_and_steps_roundtrip(tmp_path):
    from imp.engine.state import State
    from imp.providers.config import ProviderModel
    s = State(
        id="c1", step="a", step_attempt=0, status="new",
        resolved_tiers={
            "low":    ProviderModel(provider="anthropic", model="haiku"),
            "medium": ProviderModel(provider="anthropic", model="sonnet"),
            "high":   ProviderModel(provider="anthropic", model="opus"),
        },
        resolved_steps={
            "code-reviewer": ProviderModel(provider="openai", model="gpt-5.5"),
        },
    )
    s.save(tmp_path)
    loaded = State.load(tmp_path)
    assert loaded.resolved_tiers is not None
    assert loaded.resolved_tiers["medium"] == ProviderModel("anthropic", "sonnet")
    assert loaded.resolved_steps is not None
    assert loaded.resolved_steps["code-reviewer"].model == "gpt-5.5"


def test_state_resolved_tiers_none_preserved(tmp_path):
    """A fresh State persists resolved_tiers=null and loads back as None, so the
    executor's compute-if-absent (`is None`) check still fires after a resume."""
    from imp.engine.state import State
    s = State(id="c1", step="a", step_attempt=0, status="new")
    assert s.resolved_tiers is None
    s.save(tmp_path)
    assert State.load(tmp_path).resolved_tiers is None


def test_event_log_read_yields_malformed_sentinel_for_bad_lines(
    tmp_path: Path,
) -> None:
    """A line that isn't valid JSON yields a {'_malformed': True, 'raw': ...}
    sentinel instead of crashing the iterator. Surrounding good lines still
    parse normally."""
    path = tmp_path / "events.jsonl"
    log = JsonlEventLog(path)
    log.append(agent_id=None, type="good_one", payload={"x": 1})
    # Hand-inject a garbage line between two good ones.
    with path.open("a") as f:
        f.write("this is not json\n")
    log.append(agent_id=None, type="good_two", payload={"y": 2})

    events = list(log.read())
    assert len(events) == 3
    assert events[0]["type"] == "good_one"
    assert events[1] == {"_malformed": True, "raw": "this is not json"}
    assert events[2]["type"] == "good_two"


def test_state_roundtrips_abandoned_status(tmp_path: Path) -> None:
    from imp.engine.state import State

    state_dir = tmp_path / "state"
    State(id="s", step=None, step_attempt=0, status="abandoned").save(state_dir)
    loaded = State.load(state_dir)
    assert loaded.status == "abandoned"


def test_jsonl_tail_returns_last_n_records_in_order(tmp_path: Path) -> None:
    log = JsonlEventLog(tmp_path / "events.jsonl")
    for i in range(5):
        log.append(agent_id=None, type=f"e{i}", payload={"i": i})
    assert [e["type"] for e in log.tail(2)] == ["e3", "e4"]


def test_jsonl_tail_fewer_than_n_when_shorter(tmp_path: Path) -> None:
    log = JsonlEventLog(tmp_path / "events.jsonl")
    log.append(agent_id=None, type="only", payload={})
    assert [e["type"] for e in log.tail(3)] == ["only"]


def test_jsonl_tail_empty_when_no_events(tmp_path: Path) -> None:
    log = JsonlEventLog(tmp_path / "events.jsonl")
    assert log.tail(2) == []


def test_inmemory_tail_returns_last_n_records_in_order(tmp_path: Path) -> None:
    log = InMemoryEventLog()
    for i in range(4):
        log.append(agent_id=None, type=f"e{i}", payload={})
    assert [e["type"] for e in log.tail(2)] == ["e2", "e3"]
    assert [e["type"] for e in log.tail(5)] == ["e0", "e1", "e2", "e3"]
    assert InMemoryEventLog().tail(2) == []


def test_jsonl_tail_zero_returns_empty(tmp_path: Path) -> None:
    log = JsonlEventLog(tmp_path / "events.jsonl")
    log.append(agent_id=None, type="e0", payload={})
    assert log.tail(0) == []


def test_inmemory_tail_zero_returns_empty() -> None:
    log = InMemoryEventLog()
    log.append(agent_id=None, type="e0", payload={})
    assert log.tail(0) == []


def test_jsonl_tail_empty_file_returns_empty(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    path.touch()  # exists but zero bytes
    log = JsonlEventLog(path)
    assert log.tail(2) == []


def test_state_roundtrip_outcome_history(tmp_path: Path) -> None:
    s = State(id="c1", step="writer", step_attempt=0, status="new",
              outcome_history={"writer": [{"lap": 1, "outcome": "wrote plan-1"}]})
    s.save(tmp_path)
    back = State.load(tmp_path)
    assert back.outcome_history == {"writer": [{"lap": 1, "outcome": "wrote plan-1"}]}


def test_state_outcome_history_defaults_empty(tmp_path: Path) -> None:
    s = State(id="c1", step=None, step_attempt=0, status="new")
    assert s.outcome_history == {}
    s.save(tmp_path)
    assert State.load(tmp_path).outcome_history == {}


def test_state_load_forward_compatible_without_outcome_history(tmp_path: Path) -> None:
    (tmp_path / "state.json").write_text(json.dumps({
        "id": "c1", "step": "writer", "step_attempt": 0, "status": "new",
        "vars": {}, "loop_counts": {}, "current_step": None,
        "retry_feedback": None,
    }))
    assert State.load(tmp_path).outcome_history == {}


def test_state_step_label_roundtrips(tmp_path: Path) -> None:
    s = State(id="c1", step="write_spec", step_attempt=0, status="new",
              step_label="writing spec")
    s.save(tmp_path)
    back = State.load(tmp_path)
    assert back.step_label == "writing spec"


def test_state_step_label_defaults_none() -> None:
    s = State(id="c1", step=None, step_attempt=0, status="new")
    assert s.step_label is None


def test_state_load_forward_compatible_without_step_label(tmp_path: Path) -> None:
    # A state.json written before this field existed must load with step_label=None.
    (tmp_path / "state.json").write_text(json.dumps({
        "id": "c1", "step": "coder", "step_attempt": 0, "status": "new",
        "vars": {}, "loop_counts": {}, "current_step": None, "retry_feedback": None,
    }))
    assert State.load(tmp_path).step_label is None


def test_state_round_trips_hold_mark(tmp_path) -> None:
    from imp.engine.state import State
    sd = tmp_path / "state"
    s = State(id="feat", step="touch-base", step_attempt=0, status="new")
    s.held_lane = "main"
    s.held_acquire_step = "wait-for-head"
    s.save(sd)
    loaded = State.load(sd)
    assert loaded.held_lane == "main"
    assert loaded.held_acquire_step == "wait-for-head"


def test_state_hold_mark_defaults_none(tmp_path: Path) -> None:
    from imp.engine.state import State
    (tmp_path / "state.json").write_text(json.dumps({
        "id": "feat", "step": "s", "step_attempt": 0, "status": "new",
        "vars": {}, "loop_counts": {}, "current_step": None,
        "retry_feedback": None,
    }))
    loaded = State.load(tmp_path)
    assert loaded.held_lane is None
    assert loaded.held_acquire_step is None


def test_saved_is_a_status_value() -> None:
    from typing import get_args

    from imp.engine.state import Status
    assert "saved" in get_args(Status)


def test_reincarnation_counter_bumps_independently_per_session() -> None:
    s = State(id="c1", step="impl", step_attempt=0, status="new")
    assert s.reincarnation_count("step:implement/task-lead") == 0
    assert s.bump_reincarnation("step:implement/task-lead") == 1
    assert s.bump_reincarnation("step:implement/task-lead") == 2
    # A subagent session under the same step carries its own counter.
    assert s.bump_reincarnation("sub:coder/1") == 1
    assert s.reincarnation_count("step:implement/task-lead") == 2
    assert s.reincarnation_count("sub:coder/1") == 1


def test_reincarnations_round_trip(tmp_path: Path) -> None:
    s = State(id="c1", step="impl", step_attempt=0, status="new")
    s.bump_reincarnation("step:implement/task-lead")
    s.save(tmp_path)
    back = State.load(tmp_path)
    assert back.reincarnations == {"step:implement/task-lead": 1}
    assert back.reincarnation_count("step:implement/task-lead") == 1


def test_reincarnations_defaults_empty(tmp_path: Path) -> None:
    s = State(id="c1", step=None, step_attempt=0, status="new")
    s.save(tmp_path)
    back = State.load(tmp_path)
    assert back.reincarnations == {}


def test_max_reincarnations_round_trips(tmp_path: Path) -> None:
    s = State(id="c1", step=None, step_attempt=0, status="new",
              max_reincarnations=5)
    s.save(tmp_path)
    assert State.load(tmp_path).max_reincarnations == 5


def test_max_reincarnations_defaults_to_two(tmp_path: Path) -> None:
    s = State(id="c1", step=None, step_attempt=0, status="new")
    s.save(tmp_path)
    assert State.load(tmp_path).max_reincarnations == 2


def test_state_resume_target_defaults_to_none_and_round_trips(tmp_path: Path) -> None:
    s = State(id="c1", step="plan", step_attempt=0, status="paused")
    s.save(tmp_path)

    # Key genuinely absent from on-disk JSON (forward-compat path) ⇒ loads as None.
    raw = json.loads((tmp_path / "state.json").read_text())
    del raw["resume_target"]
    (tmp_path / "state.json").write_text(json.dumps(raw))
    assert State.load(tmp_path).resume_target is None

    # An explicitly stamped "prompt" survives a save() -> load() cycle and is
    # written into the on-disk payload.
    s.resume_target = "prompt"
    s.save(tmp_path)
    assert State.load(tmp_path).resume_target == "prompt"
    raw = json.loads((tmp_path / "state.json").read_text())
    assert raw["resume_target"] == "prompt"


def test_state_save_cancellation_cleans_temp_file(tmp_path: Path, monkeypatch) -> None:
    # A cancellation-shaped BaseException mid-save (anyio/asyncio cancel,
    # KeyboardInterrupt) must not leak a .state-* temp file beside state.json
    # — same cleanup net as edit_file's _atomic_write_bytes.
    import imp.engine.state as state_mod

    class FakeCancelled(BaseException):
        pass

    def boom(src: object, dst: object) -> None:
        raise FakeCancelled

    monkeypatch.setattr(state_mod.os, "replace", boom)
    s = State(id="c1", step="x", step_attempt=0, status="new",
              vars={}, loop_counts={})
    try:
        s.save(tmp_path)
        raise AssertionError("save should have propagated FakeCancelled")
    except FakeCancelled:
        pass
    leftovers = [f.name for f in tmp_path.iterdir() if f.name.startswith(".state-")]
    assert leftovers == []


def test_clean_tree_bounce_counter_bumps_and_reads_per_step() -> None:
    s = State(id="c1", step="task-lead", step_attempt=0, status="new")
    assert s.clean_tree_bounce_count("step:implement/task-lead") == 0
    assert s.bump_clean_tree_bounce("step:implement/task-lead") == 1
    assert s.bump_clean_tree_bounce("step:implement/task-lead") == 2
    # A different step keeps its own count.
    assert s.bump_clean_tree_bounce("step:converge/deferred-sweep") == 1
    assert s.clean_tree_bounce_count("step:implement/task-lead") == 2


def test_clean_tree_bounces_round_trip(tmp_path: Path) -> None:
    s = State(id="c1", step="task-lead", step_attempt=0, status="new")
    s.bump_clean_tree_bounce("step:implement/task-lead")
    s.save(tmp_path)
    back = State.load(tmp_path)
    assert back.clean_tree_bounces == {"step:implement/task-lead": 1}
    assert back.clean_tree_bounce_count("step:implement/task-lead") == 1


def test_max_clean_tree_bounces_defaults_to_three(tmp_path: Path) -> None:
    s = State(id="c1", step=None, step_attempt=0, status="new")
    assert s.max_clean_tree_bounces == 3
    s.save(tmp_path)
    assert State.load(tmp_path).max_clean_tree_bounces == 3


def test_state_roundtrips_has_started(tmp_path: Path) -> None:
    default = State(id="c", step=None, step_attempt=0, status="new")
    assert default.has_started is False          # field defaults to not-yet-run
    default.save(tmp_path)
    assert State.load(tmp_path).has_started is False

    started = State(id="c", step="chat", step_attempt=0, status="new",
                    has_started=True)
    started.save(tmp_path)
    raw = json.loads((tmp_path / "state.json").read_text())
    assert raw["has_started"] is True            # persisted to the JSON payload
    assert State.load(tmp_path).has_started is True

    (tmp_path / "state.json").write_text(
        '{"id":"c","step":null,"step_attempt":0,"status":"new"}')
    assert State.load(tmp_path).has_started is False  # absent key -> default
