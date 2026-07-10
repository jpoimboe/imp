"""Shared deterministic event-sequence fixtures for narrative / render / live
tests. The canonical `ev()` builder and the SAMPLE sequence were extracted from
the test_tui_*.py files (design "Testing" — fixture extraction ordered first)
before those files were deleted.

Schema (unified step model): there are NO phase_started / phase_completed /
gate_evaluated events. Phase is DERIVED from step_started.payload.phase, and the
resolved route lives in step_completed.payload.action (a dict {verb, target?}).
Applied set: results live in the sibling step_completed.payload.var_mutations."""
from __future__ import annotations

import io
from typing import Any


class _TTY(io.StringIO):
    def isatty(self) -> bool:
        return True


def _two_phase_events():
    return [
        {"type": "change_created", "agent_id": None, "payload": {"title": "t"},
         "id": "e0", "timestamp": "2026-06-03T00:00:00+00:00"},
        {"type": "step_started", "agent_id": None,
         "payload": {"step": "draft", "kind": "branch", "phase": "spec"},
         "id": "e1", "timestamp": "2026-06-03T00:00:00+00:00"},
        {"type": "step_completed", "agent_id": None,
         "payload": {"step": "draft", "kind": "branch", "action": {"verb": "continue"}},
         "id": "e2", "timestamp": "2026-06-03T00:00:05+00:00"},
        {"type": "step_started", "agent_id": None,
         "payload": {"step": "refine", "kind": "branch", "phase": "plan"},
         "id": "e3", "timestamp": "2026-06-03T00:00:05+00:00"},
        {"type": "step_completed", "agent_id": None,
         "payload": {"step": "refine", "kind": "branch", "action": {"verb": "continue"}},
         "id": "e4", "timestamp": "2026-06-03T00:00:09+00:00"},
    ]


def ev(eid: str, etype: str, agent_id: str | None = None, **payload: Any) -> dict[str, Any]:
    """An events.jsonl-shaped event dict: {id, agent_id, type, payload}.

    If a `timestamp` keyword is provided it is hoisted to the top-level field
    (where the real events.jsonl places it) and removed from the payload dict.
    """
    ts = payload.pop("timestamp", None)
    event: dict[str, Any] = {"id": eid, "agent_id": agent_id, "type": etype, "payload": payload}
    if ts is not None:
        event["timestamp"] = ts
    return event


# A minimal change -> step (phase derived) -> thinking -> complete sequence.
SAMPLE: list[dict[str, Any]] = [
    ev("1", "change_created", change_id=1, title="x"),
    ev("2", "step_started", step="spec", kind="agent", phase="design",
       model="m", provider="anthropic"),
    ev("3", "thinking", agent_id="step:design/spec", content="hmm"),
    ev("4", "step_completed", step="spec", kind="agent",
       action={"verb": "continue"}, var_mutations={}),
]
