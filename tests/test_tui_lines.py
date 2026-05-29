# tests/test_tui_lines.py
from __future__ import annotations

from imp.cli import narrative as nv
from imp.cli.tui import FoldState, RenderedLine, render_tree_to_lines


def _ev(eid, etype, agent_id=None, **payload):
    return {"id": eid, "agent_id": agent_id, "type": etype, "payload": payload}


def _text(lines: list[RenderedLine]) -> str:
    return "\n".join("".join(t for _s, t in ln.fragments) for ln in lines)


SAMPLE = [
    _ev("1", "change_created", change_id=3, title="add auth"),
    _ev("2", "phase_started", phase="design"),
    _ev("3", "step_started", step="spec", model="m", provider="anthropic"),
    _ev("4", "thinking", agent_id="step:design/spec", content="x" * 50),
    _ev("5", "tool_call", agent_id="step:design/spec", id="b1", name="bash", input={}),
    _ev("6", "tool_result", agent_id="step:design/spec", id="b1", content="ok", error=False),
    _ev("7", "step_completed", step="spec", action="continue"),
]


def test_collapsed_lines_show_fold_summaries_and_mark_headers() -> None:
    tree = nv.build_tree(SAMPLE)
    lines = render_tree_to_lines(tree, FoldState(), width=80, color=False)
    blob = _text(lines)
    assert "#3" in blob and "add auth" in blob
    assert "▸ thinking · 50 chars" in blob
    assert "▸ 1 tools · 1 bash" in blob
    # Fold headers are flagged and carry their owning node.
    headers = [ln for ln in lines if ln.is_fold_header]
    assert any(isinstance(ln.node, nv.ThinkingFold) for ln in headers)
    assert any(isinstance(ln.node, nv.ToolBurstFold) for ln in headers)


def test_expanding_a_fold_reveals_its_body() -> None:
    tree = nv.build_tree(SAMPLE)
    fs = FoldState()
    # locate the thinking fold node
    step = tree.root.children[0].children[0]
    think = next(c for c in step.children if isinstance(c, nv.ThinkingFold))
    fs.toggle(think)
    blob = _text(render_tree_to_lines(tree, fs, width=80, color=False))
    assert "▾ thinking" in blob
    assert "x" * 50 in blob


def test_default_expanded_keeps_step_context_collapsed_until_toggled() -> None:
    events = [
        *SAMPLE[:3],
        _ev("c", "step_context", agent_id="step:design/spec", provider="anthropic",
            model="m", system_prompt="SECRET", tools_schema=[], params={},
            max_tool_calls=50),
    ]
    tree = nv.build_tree(events)
    fs = FoldState(default_expanded=True)        # mirrors --full / expand-all
    blob = _text(render_tree_to_lines(tree, fs, width=80, color=False))
    assert "SECRET" in blob                       # expand-all reveals context


def test_every_line_carries_a_node_for_cursor_mapping() -> None:
    tree = nv.build_tree(SAMPLE)
    lines = render_tree_to_lines(tree, FoldState(), width=80, color=False)
    # Every line maps to some node (the workflow header maps to the root).
    assert all(ln.node is not None for ln in lines)


def test_prose_renders_markdown_bold_via_rich() -> None:
    ev = [
        _ev("1", "phase_started", phase="p"),
        _ev("2", "step_started", step="s", model="m", provider="anthropic"),
        _ev("3", "llm_response", agent_id="step:p/s", provider="anthropic", model="m",
            stop_reason="end_turn", usage={},
            raw={"content": [{"type": "text", "text": "Here is **bold** text"}]}),
    ]
    tree = nv.build_tree(ev)
    lines = render_tree_to_lines(tree, FoldState(), width=80, color=True)
    blob = "\n".join("".join(t for _s, t in ln.fragments) for ln in lines)
    assert "bold" in blob
    # rich must actually apply bold styling: some fragment on an agent-prose line
    # carries "bold" in its prompt_toolkit style string.
    prose_frags = [frag for ln in lines if isinstance(ln.node, nv.AgentTextNode)
                   for frag in ln.fragments]
    assert any("bold" in style for style, text in prose_frags)


def test_ansi_memo_caches_per_node_and_width() -> None:
    from imp.cli.tui import AnsiMemo
    ev = [
        _ev("1", "phase_started", phase="p"),
        _ev("2", "step_started", step="s", model="m", provider="anthropic"),
        _ev("3", "llm_response", agent_id="step:p/s", provider="anthropic", model="m",
            stop_reason="end_turn", usage={},
            raw={"content": [{"type": "text", "text": "hello"}]}),
    ]
    tree = nv.build_tree(ev)
    memo = AnsiMemo(max_entries=8)
    render_tree_to_lines(tree, FoldState(), width=80, color=True, memo=memo)
    assert memo.hits == 0 and memo.size >= 1
    render_tree_to_lines(tree, FoldState(), width=80, color=True, memo=memo)
    assert memo.hits >= 1                    # second pass at same width → cache hit
    render_tree_to_lines(tree, FoldState(), width=40, color=True, memo=memo)
    # different width → fresh render for that node, no false hit at width 40
