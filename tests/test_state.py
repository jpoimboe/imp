import json
from pathlib import Path

from imp.engine.state import JsonlEventLog, State


def test_state_round_trip(tmp_path: Path) -> None:
    s = State(
        change_id=5,
        phase="onboard",
        step="intent",
        step_attempt=1,
        phase_attempt=0,
        status="running",
    )
    s.save(tmp_path)
    loaded = State.load(tmp_path)
    assert loaded == s


def test_state_atomic_overwrite(tmp_path: Path) -> None:
    s = State(
        change_id=1, phase="p1", step=None, step_attempt=0, phase_attempt=0,
        status="running",
    )
    s.save(tmp_path)
    s2 = State(
        change_id=1, phase="p2", step="s", step_attempt=2, phase_attempt=1,
        status="running",
    )
    s2.save(tmp_path)
    raw = json.loads((tmp_path / "state.json").read_text())
    assert raw["phase"] == "p2"
    assert raw["step_attempt"] == 2


def test_state_no_partial_writes(tmp_path: Path) -> None:
    s = State(
        change_id=1, phase="p", step=None, step_attempt=0, phase_attempt=0,
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
    log.append(agent_id=None, type="agent_message", payload={"step": "s1", "text": "hi"})
    log.append(agent_id=None, type="step_completed", payload={"step": "s1", "action": "continue"})
    log.append(agent_id=None, type="step_started", payload={"step": "s2"})
    log.append(agent_id=None, type="agent_message", payload={"step": "s2", "text": "hello"})

    s1_events = list(log.events_for_current_step("s1"))
    # Slice from last 'step_started' for s1 to end.
    assert any(e["payload"].get("text") == "hi" for e in s1_events)
    assert not any(e["payload"].get("text") == "hello" for e in s1_events)


def test_state_round_trip_with_current_step(tmp_path: Path) -> None:
    from imp.engine.state import CurrentStep, State
    s = State(
        change_id=5, phase="implement", step="coder", step_attempt=0, phase_attempt=0,
        status="running",
        current_step=CurrentStep(transcript_dir=tmp_path / "steps/implement/coder"),
    )
    s.save(tmp_path)
    loaded = State.load(tmp_path)
    assert loaded.current_step is not None
    assert loaded.current_step.transcript_dir == tmp_path / "steps/implement/coder"


def test_state_round_trip_without_current_step(tmp_path: Path) -> None:
    from imp.engine.state import State
    s = State(
        change_id=5, phase=None, step=None, step_attempt=0, phase_attempt=0,
        status="completed",
    )
    s.save(tmp_path)
    loaded = State.load(tmp_path)
    assert loaded.current_step is None


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
