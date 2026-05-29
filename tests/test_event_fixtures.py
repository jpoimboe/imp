from __future__ import annotations

from tests._event_fixtures import SAMPLE, ev


def test_ev_builds_canonical_shape() -> None:
    e = ev("7", "tool_call", agent_id="step:design/spec", id="b1", name="bash",
           input={"command": "ls"})
    assert e == {
        "id": "7", "agent_id": "step:design/spec", "type": "tool_call",
        "payload": {"id": "b1", "name": "bash", "input": {"command": "ls"}},
    }


def test_sample_is_a_complete_phase() -> None:
    types = [e["type"] for e in SAMPLE]
    assert types[0] == "change_created"
    assert "phase_started" in types and "phase_completed" in types
    assert "step_started" in types and "step_completed" in types
