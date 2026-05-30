from __future__ import annotations

from tests._event_fixtures import SAMPLE, ev


def test_ev_builds_canonical_shape() -> None:
    e = ev("7", "tool_call", agent_id="step:design/spec", id="b1", name="bash",
           input={"command": "ls"})
    assert e == {
        "id": "7", "agent_id": "step:design/spec", "type": "tool_call",
        "payload": {"id": "b1", "name": "bash", "input": {"command": "ls"}},
    }


def test_sample_is_a_complete_step() -> None:
    types = [e["type"] for e in SAMPLE]
    assert types[0] == "change_created"
    # No phase_started/phase_completed events — phase is derived from
    # step_started.payload.phase.
    assert "phase_started" not in types and "phase_completed" not in types
    assert "step_started" in types and "step_completed" in types
    started = next(e for e in SAMPLE if e["type"] == "step_started")
    assert started["payload"]["phase"] == "design"
    assert started["payload"]["kind"] == "agent"
    completed = next(e for e in SAMPLE if e["type"] == "step_completed")
    assert completed["payload"]["action"] == {"verb": "continue"}
    assert completed["payload"]["var_mutations"] == {}
