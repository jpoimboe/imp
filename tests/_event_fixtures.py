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


def interrupted_task_lead_events() -> list[dict[str, Any]]:
    """task 1/2 prose, a ctrl-c pause, a continuation resume, then a push to task 2/2."""
    return [
        ev("1", "quest_created", quest_id=1, title="t"),
        task_lead_started("2"),
        agent_text("3", "first segment"),
        ev("4", "workflow_paused", reason="interrupt", interrupt_reason="ctrl-c"),
        ev("5", "workflow_resumed"),
        task_lead_started("6", resumed=True, restarted=False),
        agent_text("7", "second segment"),
        ev("8", "step_label_updated", agent_id=TASK_LEAD_AID, label="task 2/2"),
        agent_text("9", "third segment"),
        ev("10", "step_completed", step="task-lead", kind="agent",
           action={"verb": "continue"}, var_mutations={}),
    ]


SPEC_MARKER = "✓ spec written · imp show abc123 spec"


def express_spec_marker_events() -> list[dict[str, Any]]:
    """Short path: hidden threshold -> hidden scope-code-check -> hidden spec-written
    (show_output) printing the spec marker -> planning phase."""
    return [
        ev("1", "quest_created", quest_id=1, title="x"),
        ev("2", "step_started", step="threshold", kind="agent", phase="threshold",
           model="m", provider="anthropic", label="chatting",
           phase_label="threshold phase", show_label=False, phase_show_label=False),
        ev("3", "agent_message", agent_id="step:threshold/threshold",
           provider="anthropic", model="m", stop_reason="end_turn", usage={},
           raw={"content": [{"type": "text", "text": "Spec drafted."}]}),
        ev("4", "step_completed", step="threshold", kind="agent",
           action={"verb": "goto", "target": "scope-code-check", "resume": False},
           var_mutations={}),
        ev("5", "step_started", step="scope-code-check", kind="script",
           phase="threshold", phase_label="threshold phase", show_label=False,
           phase_show_label=False),
        ev("6", "step_completed", step="scope-code-check", kind="script",
           action={"verb": "goto", "target": "spec-written", "resume": False},
           var_mutations={}),
        ev("7", "step_started", step="spec-written", kind="script", phase="threshold",
           phase_label="threshold phase", show_label=False, phase_show_label=False,
           show_output=True),
        ev("8", "script_output", step="spec-written", stdout=SPEC_MARKER + "\n"),
        ev("9", "step_completed", step="spec-written", kind="script",
           action={"verb": "goto", "target": "plan-writer", "resume": False},
           var_mutations={}),
        ev("10", "step_started", step="plan-writer", kind="agent", phase="plan",
           model="m", provider="anthropic", label="writing plan",
           phase_label="planning phase · plan 1/1"),
    ]


def assert_spec_marker_scrollback(lines: list[str]) -> None:
    """The marker is a `• ` line, the hidden step has no header/breadcrumb, and the
    planning-phase rule follows the marker after one blank separator."""
    assert "• " + SPEC_MARKER in lines, lines
    i = lines.index("• " + SPEC_MARKER)
    assert len(lines) > i + 2, lines
    assert not any("spec-written" in ln for ln in lines), lines
    assert lines[i + 1] == "", lines
    assert lines[i + 2] == "── planning phase · plan 1/1 ───", lines


INTERNAL_TOOLS = ("complete_step", "rename_quest", "create_and_enter_worktree", "save_and_exit")


def tool_burst_events(*calls: tuple[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """One open agent step holding a single burst of finished (name, input) calls."""
    events = [ev("s1", "step_started", step="s", kind="agent", phase="p",
                 model="m", provider="anthropic")]
    for i, (name, args) in enumerate(calls):
        events.append(ev(f"c{i}", "tool_call", agent_id="step:p/s", id=f"b{i}",
                         name=name, input=args))
        events.append(ev(f"r{i}", "tool_result", agent_id="step:p/s", id=f"b{i}",
                         content="ok", error=False))
    return events
