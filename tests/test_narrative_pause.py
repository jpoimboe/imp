# tests/test_narrative_pause.py
from __future__ import annotations

from imp.cli.narrative import NarrativeBuilder, PauseNode, PhaseNode, StepNode


def _pause_nodes(events: list[dict]) -> list[PauseNode]:
    # Public API: feed via `nb.add(event)`; read the tree via `nb.tree` → `.root`,
    # then walk `.children` recursively (PauseNode is a leaf → children=[]).
    nb = NarrativeBuilder()
    for e in events:
        nb.add(e)
    found: list[PauseNode] = []
    def _walk(node: object) -> None:
        if isinstance(node, PauseNode):
            found.append(node)
        for child in getattr(node, "children", []):
            _walk(child)
    _walk(nb.tree.root)
    return found


def test_interrupt_reason_preferred_over_reason() -> None:
    pauses = _pause_nodes([
        {"type": "workflow_paused",
         "payload": {"reason": "interrupt", "interrupt_reason": "esc"}},
    ])
    assert len(pauses) == 1
    assert pauses[0].reason == "esc"


def test_reason_used_when_no_interrupt_reason() -> None:
    pauses = _pause_nodes([
        {"type": "workflow_paused", "payload": {"reason": "max_loops"}},
    ])
    assert pauses[0].reason == "max_loops"


def _created() -> dict:
    return {"id": "c", "agent_id": None, "type": "change_created",
            "payload": {"id": "abc123", "title": "x"}}


def _started(eid: str, phase: str, step: str = "s") -> dict:
    return {"id": eid, "agent_id": None, "type": "step_started",
            "payload": {"step": step, "kind": "agent", "phase": phase}}


def _completed(eid: str, step: str = "s") -> dict:
    return {"id": eid, "agent_id": None, "type": "step_completed",
            "payload": {"step": step, "kind": "agent",
                        "action": {"verb": "continue"}, "var_mutations": {}}}


def _paused(eid: str = "z", reason: str = "max_loops") -> dict:
    return {"id": eid, "agent_id": None, "type": "workflow_paused",
            "payload": {"interrupt_reason": reason}}


def _parent_of_pause(root: object) -> object:
    def walk(node: object, parent: object) -> object:
        if isinstance(node, PauseNode):
            return parent
        for child in getattr(node, "children", []):
            found = walk(child, node)
            if found is not None:
                return found
        return None
    return walk(root, None)


def _build(events: list[dict]) -> object:
    nb = NarrativeBuilder()
    for e in events:
        nb.add(e)
    return nb.tree.root


def test_pause_after_step_completed_nests_in_phase_above_next_phase() -> None:
    root = _build([
        _created(),
        _started("s1", phase="design"),
        _completed("s1"),
        _paused(),
        _started("s2", phase="implement"),
    ])
    phases = [c for c in root.children if isinstance(c, PhaseNode)]
    assert len(phases) == 2                                  # design, then implement
    parent = _parent_of_pause(root)
    assert parent is phases[0]                               # nested in the first phase
    assert root.children.index(phases[0]) < root.children.index(phases[1])


def test_pause_during_open_step_nests_inside_that_step() -> None:
    root = _build([
        _created(),
        _started("s1", phase="design"),   # step left open
        _paused(eid="z", reason="interrupt"),
    ])
    parent = _parent_of_pause(root)
    assert isinstance(parent, StepNode)


def test_pause_finalizes_open_folds_without_clearing_membership() -> None:
    nb = NarrativeBuilder()
    for e in [
        _created(),
        _started("s1", phase="design"),
        {"id": "t1", "agent_id": "a", "type": "tool_call",
         "payload": {"id": "t1", "name": "sub_agent",
                     "input": {"subagent_name": "reviewer"}}},
        _paused(eid="z", reason="interrupt"),
    ]:
        nb.add(e)
    fold = nb._open_sub_by_event["t1"]
    assert fold.running is False
    assert fold.terminal_state == "interrupted"
    # Narrower than the resume loop: membership entries are NOT cleared.
    assert "t1" in nb._open_sub_by_event
    assert "sub:t1" in nb._sub_by_agent_id


def test_awaiting_user_pause_appends_no_pause_node() -> None:
    pauses = _pause_nodes([
        {"type": "workflow_paused", "payload": {"reason": "awaiting_user"}},
    ])
    assert pauses == []


def test_awaiting_user_pause_still_finalizes_open_folds_and_sets_paused() -> None:
    nb = NarrativeBuilder()
    for e in [
        _created(),
        _started("s1", phase="design"),
        {"id": "t1", "agent_id": "a", "type": "tool_call",
         "payload": {"id": "t1", "name": "sub_agent",
                     "input": {"subagent_name": "reviewer"}}},
        {"id": "z", "agent_id": None, "type": "workflow_paused",
         "payload": {"reason": "awaiting_user"}},
    ]:
        nb.add(e)
    # Root still marked paused, open folds finalized exactly as for any pause...
    assert nb.tree.root.status == "paused"
    fold = nb._open_sub_by_event["t1"]
    assert fold.running is False
    assert fold.terminal_state == "interrupted"
    # ...but NO PauseNode is appended (the redundant your-turn line is suppressed).
    def _walk(node: object) -> bool:
        if isinstance(node, PauseNode):
            return True
        return any(_walk(c) for c in getattr(node, "children", []))
    assert _walk(nb.tree.root) is False
