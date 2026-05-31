import json
from pathlib import Path

from imp.engine.state import JsonlEventLog, State


def test_state_roundtrip_vars_and_loop_counts(tmp_path: Path) -> None:
    s = State(slug="c1", step="spec-writer", step_attempt=0, status="running",
              vars={"review_done": False, "round": 0},
              loop_counts={"spec-reviewer": 2})
    s.save(tmp_path)
    back = State.load(tmp_path)
    assert back.step == "spec-writer"
    assert back.vars == {"review_done": False, "round": 0}
    assert back.loop_counts == {"spec-reviewer": 2}
    assert not hasattr(back, "phase")


def test_state_defaults(tmp_path: Path) -> None:
    s = State(slug="c1", step=None, step_attempt=0, status="running")
    s.save(tmp_path)
    back = State.load(tmp_path)
    assert back.vars == {}
    assert back.loop_counts == {}


def test_state_round_trip(tmp_path: Path) -> None:
    s = State(
        slug="c5",
        step="intent",
        step_attempt=1,
        status="running",
    )
    s.save(tmp_path)
    loaded = State.load(tmp_path)
    assert loaded == s


def test_state_atomic_overwrite(tmp_path: Path) -> None:
    s = State(
        slug="c1", step=None, step_attempt=0,
        status="running",
    )
    s.save(tmp_path)
    s2 = State(
        slug="c1", step="s", step_attempt=2,
        status="running",
    )
    s2.save(tmp_path)
    raw = json.loads((tmp_path / "state.json").read_text())
    assert raw["step"] == "s"
    assert raw["step_attempt"] == 2


def test_state_no_partial_writes(tmp_path: Path) -> None:
    s = State(
        slug="c1", step=None, step_attempt=0,
        status="running",
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
        slug="c5", step="coder", step_attempt=0,
        status="running",
        current_step=CurrentStep(transcript_dir=tmp_path / "steps/coder"),
    )
    s.save(tmp_path)
    loaded = State.load(tmp_path)
    assert loaded.current_step is not None
    assert loaded.current_step.transcript_dir == tmp_path / "steps/coder"


def test_state_round_trip_without_current_step(tmp_path: Path) -> None:
    from imp.engine.state import State
    s = State(
        slug="c5", step=None, step_attempt=0,
        status="completed",
    )
    s.save(tmp_path)
    loaded = State.load(tmp_path)
    assert loaded.current_step is None


def test_state_round_trip_with_retry_feedback(tmp_path: Path) -> None:
    s = State(
        slug="c7",
        step="coder",
        step_attempt=1,
        status="running",
        retry_feedback={"target_step": "coder", "text": "fix the lint error"},
    )
    s.save(tmp_path)
    loaded = State.load(tmp_path)
    assert loaded == s
    assert loaded.retry_feedback == {"target_step": "coder", "text": "fix the lint error"}


def test_state_round_trip_retry_feedback_defaults_none(tmp_path: Path) -> None:
    s = State(
        slug="c1", step=None, step_attempt=0,
        status="running",
    )
    s.save(tmp_path)
    raw = json.loads((tmp_path / "state.json").read_text())
    assert raw["retry_feedback"] is None
    assert State.load(tmp_path).retry_feedback is None


def test_state_resume_pending_roundtrip(tmp_path: Path) -> None:
    s = State(slug="c1", step="coder", step_attempt=0, status="running",
              resume_pending="coder")
    s.save(tmp_path)
    back = State.load(tmp_path)
    assert back.resume_pending == "coder"


def test_state_resume_pending_defaults_none() -> None:
    s = State(slug="c1", step=None, step_attempt=0, status="running")
    assert s.resume_pending is None


def test_state_load_forward_compatible_without_resume_pending(tmp_path: Path) -> None:
    # An older state.json (written before this field existed) still loads.
    (tmp_path / "state.json").write_text(json.dumps({
        "slug": "c1", "step": "coder", "step_attempt": 0, "status": "running",
        "vars": {}, "loop_counts": {}, "current_step": None, "retry_feedback": None,
    }))
    back = State.load(tmp_path)
    assert back.resume_pending is None


def test_state_resolved_roles_roundtrip(tmp_path):
    from imp.engine.state import State
    from imp.providers.config import ProviderModel
    s = State(
        slug="c1", step="a", step_attempt=0, status="running",
        resolved_roles={
            "code": ProviderModel(provider="anthropic", model="haiku"),
            "code_review": ProviderModel(provider="openai", model="gpt-5.2"),
        },
    )
    s.save(tmp_path)
    loaded = State.load(tmp_path)
    assert loaded.resolved_roles is not None
    assert loaded.resolved_roles["code"] == ProviderModel("anthropic", "haiku")
    assert loaded.resolved_roles["code_review"].model == "gpt-5.2"


def test_state_resolved_roles_none_preserved(tmp_path):
    """A fresh State persists resolved_roles=null and loads back as None, so the
    executor's compute-if-absent (`is None`) check still fires after a resume."""
    from imp.engine.state import State
    s = State(slug="c1", step="a", step_attempt=0, status="running")
    assert s.resolved_roles is None
    s.save(tmp_path)
    assert State.load(tmp_path).resolved_roles is None


def test_state_round_trips_model_tiers(tmp_path) -> None:
    from imp.engine.state import State
    s = State(slug="c1", step="x", step_attempt=0, status="running",
              model_tiers=("fast", "powerful"))
    s.save(tmp_path)
    loaded = State.load(tmp_path)
    assert loaded.model_tiers == ("fast", "powerful")


def test_state_model_tiers_defaults_none(tmp_path) -> None:
    from imp.engine.state import State
    s = State(slug="c1", step="x", step_attempt=0, status="running")
    s.save(tmp_path)
    loaded = State.load(tmp_path)
    assert loaded.model_tiers is None


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
