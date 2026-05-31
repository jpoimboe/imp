# tests/test_render_text.py
from __future__ import annotations

import json
from pathlib import Path

from imp.cli import narrative as nv
from imp.cli import render_text as rt
from imp.cli.render_text import render_tree


class _FakeRendered:
    def __init__(self, texts): self.texts = texts


def _lines(events, *, full=False, width=80):
    tree = nv.build_tree(events)
    return list(render_tree(tree, full=full, color=False, width=width))


def _ev(eid, etype, agent_id=None, **payload):
    return {"id": eid, "agent_id": agent_id, "type": etype, "payload": payload}


def test_header_phase_step_outcome_render() -> None:
    out = "\n".join(_lines([
        _ev("1", "change_created", change_id=7, title="add auth"),
        _ev("3", "step_started", step="spec", kind="agent", phase="design",
            model="m", provider="anthropic"),
        _ev("4", "step_completed", step="spec", kind="agent",
            action={"verb": "continue"}, var_mutations={}),
    ]))
    assert "#7" in out and "add auth" in out
    assert "design" in out
    assert "spec" in out
    assert "continue" in out


def test_agent_text_rendered_in_full_not_truncated() -> None:
    long = "This is a long agent message. " * 10
    out = "\n".join(_lines([
        _ev("2", "step_started", step="s", kind="agent", phase="p",
            model="m", provider="anthropic"),
        _ev("3", "llm_response", agent_id="step:p/s", provider="anthropic", model="m",
            stop_reason="end_turn", usage={},
            raw={"content": [{"type": "text", "text": long}]}),
    ], width=40))
    # Every word survives (no elision); wrapping happened (multiple lines).
    assert "message." in out
    assert out.count("\n") >= 5


def test_collapsed_folds_show_one_line_summaries() -> None:
    out = "\n".join(_lines([
        _ev("2", "step_started", step="s", kind="agent", phase="p",
            model="m", provider="anthropic"),
        _ev("3", "thinking", agent_id="step:p/s", content="x" * 120),
        _ev("4", "tool_call", agent_id="step:p/s", id="b1", name="bash", input={}),
        _ev("5", "tool_result", agent_id="step:p/s", id="b1", content="ok", error=False),
        _ev("6", "tool_call", agent_id="step:p/s", id="b2", name="bash", input={}),
        _ev("7", "tool_result", agent_id="step:p/s", id="b2", content="ok", error=False),
        _ev("8", "tool_call", agent_id="step:p/s", id="b3", name="ls", input={}),
        _ev("9", "tool_result", agent_id="step:p/s", id="b3", content="ok", error=False),
    ]))
    assert "▸ thinking · 120 chars" in out
    # burst summary sorted by count desc
    assert "▸ 3 tools · 2 bash · 1 ls" in out
    # no expanded tool lines in collapsed mode
    assert "⏺" not in out


def test_collapsed_subagent_and_step_context_summaries() -> None:
    out = "\n".join(_lines([
        _ev("2", "step_started", step="s", kind="agent", phase="p",
            model="m", provider="anthropic"),
        _ev("3", "step_context", agent_id="step:p/s", provider="anthropic",
            model="claude", system_prompt="S", tools_schema=[{"name": "a"}, {"name": "b"}],
            params={}, max_tool_calls=50),
        _ev("tk1", "tool_call", agent_id="step:p/s", id="b1", name="task",
            input={"subagent_name": "reviewer", "task_description": "go"}),
        _ev("su1", "llm_response", agent_id="sub:tk1", provider="anthropic", model="m",
            stop_reason="end_turn", usage={}, raw={"content": []}),
        _ev("r1", "tool_result", agent_id="step:p/s", id="b1",
            content='{"status":"Success"}', error=False),
    ]))
    assert "▸ context · anthropic/claude · 2 tools" in out
    assert "▸ task → reviewer · 1 events" in out


def test_human_gate_and_error_lines() -> None:
    out = "\n".join(_lines([
        _ev("1", "step_started", step="merge", kind="human", phase="p"),
        _ev("2", "human_prompt_pending", step="merge", phase="p",
            ask="Approve?", options=[{"label": "yes"}]),
        _ev("3", "step_completed", step="merge", kind="human",
            action={"verb": "continue"}, var_mutations={}),
        _ev("4", "error", category="auth", message="bad key"),
    ]))
    assert "human gate" in out and "continue" in out
    assert "bad key" in out


def test_full_mode_expands_tools_thinking_and_subagent() -> None:
    out = "\n".join(_lines([
        _ev("2", "step_started", step="s", kind="agent", phase="p",
            model="m", provider="anthropic"),
        _ev("3", "thinking", agent_id="step:p/s", content="deep thoughts here"),
        _ev("4", "tool_call", agent_id="step:p/s", id="b1", name="bash",
            input={"command": "ls -la"}),
        _ev("5", "tool_result", agent_id="step:p/s", id="b1", content="total 0", error=False),
        _ev("tk1", "tool_call", agent_id="step:p/s", id="b2", name="task",
            input={"subagent_name": "reviewer", "task_description": "go"}),
        _ev("su1", "llm_response", agent_id="sub:tk1", provider="anthropic", model="m",
            stop_reason="end_turn", usage={},
            raw={"content": [{"type": "text", "text": "subagent verdict: approve"}]}),
        _ev("r1", "tool_result", agent_id="step:p/s", id="b2",
            content='{"status":"Success"}', error=False),
    ], full=True))
    assert "▾ thinking" in out and "deep thoughts here" in out
    assert "▾ tools" in out
    assert "⏺ bash" in out and "command=ls -la" in out and "total 0" in out
    assert "▾ task → reviewer" in out
    # nested subagent prose appears, indented under the fold
    assert "subagent verdict: approve" in out


def test_full_mode_expands_step_context_config_dump() -> None:
    out = "\n".join(_lines([
        _ev("2", "step_started", step="s", kind="agent", phase="p",
            model="m", provider="anthropic"),
        _ev("3", "step_context", agent_id="step:p/s", provider="anthropic", model="claude",
            system_prompt="SYSTEM PROMPT TEXT", tools_schema=[{"name": "bash"}],
            params={"max_tokens": 8192}, max_tool_calls=50),
    ], full=True))
    assert "SYSTEM PROMPT TEXT" in out
    assert "- bash" in out


def test_step_context_collapsed_even_in_default_mode() -> None:
    # design §5.1: step_context is folded by default in BOTH views.
    out = "\n".join(_lines([
        _ev("2", "step_started", step="s", kind="agent", phase="p",
            model="m", provider="anthropic"),
        _ev("3", "step_context", agent_id="step:p/s", provider="anthropic", model="claude",
            system_prompt="SYSTEM PROMPT TEXT", tools_schema=[], params={}, max_tool_calls=50),
    ], full=False))
    assert "SYSTEM PROMPT TEXT" not in out
    assert "▸ context" in out


FIXTURES = Path(__file__).parent / "fixtures"


def _sample_events():
    src = FIXTURES / "narrative_sample.jsonl"
    return [json.loads(ln) for ln in src.read_text().splitlines() if ln.strip()]


def test_snapshot_default(monkeypatch) -> None:
    tree = nv.build_tree(_sample_events())
    actual = "\n".join(render_tree(tree, full=False, color=False, width=80)) + "\n"
    golden = (FIXTURES / "narrative_default.txt").read_text()
    assert actual == golden, ("default-mode drift; to accept, rewrite "
                              "tests/fixtures/narrative_default.txt with the actual output")


def test_snapshot_full(monkeypatch) -> None:
    tree = nv.build_tree(_sample_events())
    actual = "\n".join(render_tree(tree, full=True, color=False, width=80)) + "\n"
    golden = (FIXTURES / "narrative_full.txt").read_text()
    assert actual == golden, ("full-mode drift; to accept, rewrite "
                              "tests/fixtures/narrative_full.txt with the actual output")


def test_header_line_matches_render_for_phase_and_step() -> None:
    phase = nv.PhaseNode(name="design")
    step = nv.StepNode(name="spec", kind="agent", interactive=True)
    human = nv.StepNode(name="approve", kind="human")
    assert rt.header_line(phase, depth=0, color=False) == "design"
    assert rt.header_line(step, depth=1, color=False) == "  spec (interactive)"
    # Every kind renders as the bare step name (no special-case label).
    assert rt.header_line(human, depth=2, color=False) == "    approve"


def test_agent_text_is_bold_when_color_enabled() -> None:
    node = nv.AgentTextNode(payload={})
    node._cache = _FakeRendered(["hello world"])
    out = list(rt._render(node, depth=0, full=False, color=True, width=80))
    assert out and out[0].startswith("\x1b[1m")   # bold prefix from NODE_COLORS["agent"]
    assert "\x1b[36m" not in out[0]               # NOT the removed bold-cyan ask_user block
    assert "hello world" in out[0]


def test_empty_thinking_is_suppressed_both_modes() -> None:
    for content in ("", "   ", "\n\t "):
        node = nv.ThinkingFold(content=content)
        assert list(rt._render(node, depth=1, full=False, color=False, width=80)) == []
        assert list(rt._render(node, depth=1, full=True, color=False, width=80)) == []


def test_nonempty_thinking_keeps_summary() -> None:
    node = nv.ThinkingFold(content="abcde")
    out = list(rt._render(node, depth=1, full=False, color=False, width=80))
    assert out == ["  ▸ thinking · 5 chars"]


def test_render_gate_recap_false_is_header_only() -> None:
    gate = nv.GateNode(kind="human", prompt="Approve?",
                       options=[{"label": "yes"}, {"label": "no"}],
                       action={"verb": "continue"})
    header_only = list(rt._render_gate(gate, depth=1, color=False, width=80, recap=False))
    assert header_only == ["  human gate → continue"]


def test_render_gate_recap_true_includes_prompt_and_options() -> None:
    gate = nv.GateNode(kind="human", prompt="Approve?",
                       options=[{"label": "yes"}, {"label": "no"}],
                       action={"verb": "continue"})
    full = list(rt._render_gate(gate, depth=1, color=False, width=80))
    assert full[0] == "  human gate → continue"
    assert any("Approve?" in ln for ln in full)
    assert any("yes" in ln for ln in full) and any("no" in ln for ln in full)


def test_render_gate_colors_resolved_action() -> None:
    gate = nv.GateNode(kind="human", action={"verb": "continue"})
    out = list(rt._render_gate(gate, depth=0, color=True, width=80, recap=False))
    # continue -> green accent (pal._ACCENT["continue"])
    assert out[0].startswith("\x1b[32m")


def test_snapshot_covers_every_node_type() -> None:
    # Guard: the fixture must exercise all node types (design §11).
    tree = nv.build_tree(_sample_events())
    seen: set[str] = set()

    def walk(n):
        seen.add(type(n).__name__)
        for c in getattr(n, "children", []):
            walk(c)
        if isinstance(n, nv.SubagentFold):
            for c in n.tree.root.children:
                walk(c)
        if isinstance(n, nv.StepNode) and n.outcome:
            seen.add("OutcomeLine")
    walk(tree.root)
    required = {"WorkflowNode", "PhaseNode", "StepNode", "AgentTextNode",
                "UserMessageNode", "ThinkingFold", "ToolBurstFold", "SubagentFold",
                "GateNode", "ErrorNode", "StepContextNode", "OutcomeLine", "FallbackLine"}
    assert required <= seen, f"fixture missing node types: {required - seen}"


def test_literal_text_node_renders_as_bold_agent_prose() -> None:
    from imp.cli import narrative as nv
    from imp.cli import render_text as rt
    node = nv.LiteralTextNode(text="line one\nline two")
    out = list(rt._render(node, depth=1, full=False, color=False, width=80))
    assert any("line one" in ln for ln in out)
    assert any("line two" in ln for ln in out)


def test_breadcrumb_line_renders_check_step_and_seconds() -> None:
    from imp.cli import narrative as nv
    from imp.cli import render_text as rt
    # plain continue: no "→ continue" suffix
    bc = nv.BreadcrumbLine(step_name="spec", elapsed_s=12.4,
                           action={"verb": "continue"}, extras=[])
    out = "\n".join(rt._render(bc, depth=1, full=False, color=False, width=80))
    assert "✓ spec" in out and "12s" in out
    assert "→ continue" not in out
    # non-continue action: suffix appended; extras folded in
    bc2 = nv.BreadcrumbLine(step_name="code-reviewer", elapsed_s=41.0,
                            action={"verb": "goto", "target": "implement"},
                            extras=["status=Success"])
    out2 = "\n".join(rt._render(bc2, depth=1, full=False, color=False, width=80))
    assert "✓ code-reviewer" in out2 and "41s" in out2
    assert "status=Success" in out2
    assert "→ goto(implement)" in out2
