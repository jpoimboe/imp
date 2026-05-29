"""Shared deterministic event-sequence fixtures for narrative / render / live
tests. The canonical `ev()` builder and the SAMPLE sequence were extracted from
the test_tui_*.py files (design "Testing" — fixture extraction ordered first)
before those files were deleted."""
from __future__ import annotations

from typing import Any


def ev(eid: str, etype: str, agent_id: str | None = None, **payload: Any) -> dict[str, Any]:
    """An events.jsonl-shaped event dict: {id, agent_id, type, payload}."""
    return {"id": eid, "agent_id": agent_id, "type": etype, "payload": payload}


# A minimal change -> phase -> step -> thinking -> complete sequence.
SAMPLE: list[dict[str, Any]] = [
    ev("1", "change_created", change_id=1, title="x"),
    ev("2", "phase_started", phase="design"),
    ev("3", "step_started", step="spec", model="m", provider="anthropic",
       interactive=False),
    ev("4", "thinking", agent_id="step:design/spec", content="hmm"),
    ev("5", "step_completed", step="spec", action="continue"),
    ev("6", "phase_completed", phase="design"),
]
