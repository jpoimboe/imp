import json
from pathlib import Path

from imp.engine.state import EventLog, State


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
    log = EventLog(tmp_path / "events.jsonl")
    log.append("phase_started", {"phase": "onboard"})
    log.append("step_started", {"step": "intent-clarifier", "model": "claude-sonnet-4-6"})
    events = list(log.read())
    assert [e["type"] for e in events] == ["phase_started", "step_started"]
    assert events[0]["payload"]["phase"] == "onboard"
    assert events[0]["timestamp"]  # ISO-8601


def test_event_log_append_only(tmp_path: Path) -> None:
    log = EventLog(tmp_path / "events.jsonl")
    log.append("a", {})
    log.append("b", {})
    log.append("c", {})
    raw_lines = (tmp_path / "events.jsonl").read_text().splitlines()
    assert len(raw_lines) == 3


def test_event_log_filter_by_step(tmp_path: Path) -> None:
    log = EventLog(tmp_path / "events.jsonl")
    log.append("phase_started", {"phase": "p"})
    log.append("step_started", {"step": "s1"})
    log.append("agent_message", {"step": "s1", "text": "hi"})
    log.append("step_completed", {"step": "s1", "action": "continue"})
    log.append("step_started", {"step": "s2"})
    log.append("agent_message", {"step": "s2", "text": "hello"})

    s1_events = list(log.events_for_current_step("s1"))
    # Slice from last 'step_started' for s1 to end.
    assert any(e["payload"].get("text") == "hi" for e in s1_events)
    assert not any(e["payload"].get("text") == "hello" for e in s1_events)
