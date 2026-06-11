# tests/test_narrative_pause.py
from __future__ import annotations

from imp.cli.narrative import NarrativeBuilder, PauseNode


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
