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
        {"type": "quest_created", "agent_id": None, "payload": {"title": "t"},
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
    ev("1", "quest_created", quest_id=1, title="x"),
    ev("2", "step_started", step="spec", kind="agent", phase="design",
       model="m", provider="anthropic"),
    ev("3", "thinking", agent_id="step:design/spec", content="hmm"),
    ev("4", "step_completed", step="spec", kind="agent",
       action={"verb": "continue"}, var_mutations={}),
]


TASK_LEAD_AID = "step:code/task-lead"


def task_lead_started(eid: str, **over: Any) -> dict[str, Any]:
    """A position_label task-lead step_started at `task 1/2`; `over` overrides payload keys."""
    payload: dict[str, Any] = {
        "step": "task-lead", "kind": "agent", "phase": "code", "model": "m",
        "provider": "anthropic", "label": "task 1/2",
        "phase_label": "implementation phase · plan 1/1", "position_label": True}
    return ev(eid, "step_started", **{**payload, **over})


def agent_text(eid: str, text: str, agent_id: str = TASK_LEAD_AID) -> dict[str, Any]:
    """A turn-ending agent_message carrying one prose block."""
    return ev(eid, "agent_message", agent_id=agent_id, provider="anthropic", model="m",
              stop_reason="end_turn", usage={},
              raw={"content": [{"type": "text", "text": text}]})
