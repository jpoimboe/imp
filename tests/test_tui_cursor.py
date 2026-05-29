# tests/test_tui_cursor.py
from __future__ import annotations

from imp.cli import narrative as nv
from imp.cli.tui import (FoldState, fold_header_indices, next_fold_line,
                         prev_fold_line, render_tree_to_lines, remap_cursor_to_node)


def _ev(eid, etype, agent_id=None, **payload):
    return {"id": eid, "agent_id": agent_id, "type": etype, "payload": payload}


SAMPLE = [
    _ev("1", "change_created", change_id=1, title="t"),
    _ev("2", "phase_started", phase="design"),
    _ev("3", "step_started", step="spec", model="m", provider="anthropic"),
    _ev("4", "thinking", agent_id="step:design/spec", content="abc"),
    _ev("5", "tool_call", agent_id="step:design/spec", id="b1", name="bash", input={}),
    _ev("6", "tool_result", agent_id="step:design/spec", id="b1", content="ok", error=False),
    _ev("7", "step_completed", step="spec", action="continue"),
]


def test_fold_header_navigation_cycles_through_folds() -> None:
    tree = nv.build_tree(SAMPLE)
    lines = render_tree_to_lines(tree, FoldState(), width=80, color=False)
    headers = fold_header_indices(lines)
    assert len(headers) == 2                       # thinking + tools
    assert next_fold_line(lines, cursor=0) == headers[0]
    assert next_fold_line(lines, cursor=headers[0]) == headers[1]
    assert prev_fold_line(lines, cursor=headers[1]) == headers[0]
    # wrap-around: next past last → last (clamped) ; design choice: no wrap
    assert next_fold_line(lines, cursor=headers[1]) == headers[1]


def test_cursor_remaps_to_same_node_after_fold_toggle() -> None:
    tree = nv.build_tree(SAMPLE)
    fs = FoldState()
    lines = render_tree_to_lines(tree, fs, width=80, color=False)
    # Put the cursor on the tools fold header.
    tools_idx = [i for i, ln in enumerate(lines)
                 if ln.is_fold_header and isinstance(ln.node, nv.ToolBurstFold)][0]
    node_at_cursor = lines[tools_idx].node
    # Expand the thinking fold ABOVE it — line numbers shift down.
    think = lines[[i for i, ln in enumerate(lines)
                   if ln.is_fold_header and isinstance(ln.node, nv.ThinkingFold)][0]].node
    fs.toggle(think)
    new_lines = render_tree_to_lines(tree, fs, width=80, color=False)
    new_cursor = remap_cursor_to_node(new_lines, node_at_cursor, fallback=tools_idx)
    assert new_lines[new_cursor].node is node_at_cursor
    assert new_cursor > tools_idx                  # shifted down by the expansion
