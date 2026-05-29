# tests/test_render_text.py
from __future__ import annotations

import json
from pathlib import Path

from imp.cli import narrative as nv
from imp.cli.render_text import render_tree


def _lines(events, *, full=False, width=80):
    tree = nv.build_tree(events)
    return list(render_tree(tree, full=full, color=False, width=width))


def _ev(eid, etype, agent_id=None, **payload):
    return {"id": eid, "agent_id": agent_id, "type": etype, "payload": payload}


def test_header_phase_step_outcome_render() -> None:
    out = "\n".join(_lines([
        _ev("1", "change_created", change_id=7, title="add auth"),
        _ev("2", "phase_started", phase="design"),
        _ev("3", "step_started", step="spec", model="m", provider="anthropic"),
        _ev("4", "step_completed", step="spec", action="continue"),
        _ev("5", "phase_completed", phase="design"),
    ]))
    assert "#7" in out and "add auth" in out
    assert "design" in out
    assert "spec" in out
    assert "continue" in out


def test_agent_text_rendered_in_full_not_truncated() -> None:
    long = "This is a long agent message. " * 10
    out = "\n".join(_lines([
        _ev("1", "phase_started", phase="p"),
        _ev("2", "step_started", step="s", model="m", provider="anthropic"),
        _ev("3", "llm_response", agent_id="step:p/s", provider="anthropic", model="m",
            stop_reason="end_turn", usage={},
            raw={"content": [{"type": "text", "text": long}]}),
    ], width=40))
    # Every word survives (no elision); wrapping happened (multiple lines).
    assert "message." in out
    assert out.count("\n") >= 5


def test_collapsed_folds_show_one_line_summaries() -> None:
    out = "\n".join(_lines([
        _ev("1", "phase_started", phase="p"),
        _ev("2", "step_started", step="s", model="m", provider="anthropic"),
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
        _ev("1", "phase_started", phase="p"),
        _ev("2", "step_started", step="s", model="m", provider="anthropic"),
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


def test_gate_and_error_lines() -> None:
    out = "\n".join(_lines([
        _ev("1", "phase_started", phase="p"),
        _ev("2", "gate_evaluated", kind="script", context={"exit": 0},
            action={"kind": "continue"}),
        _ev("3", "error", category="auth", message="bad key"),
    ]))
    assert "script gate" in out and "continue" in out
    assert "bad key" in out


def test_full_mode_expands_tools_thinking_and_subagent() -> None:
    out = "\n".join(_lines([
        _ev("1", "phase_started", phase="p"),
        _ev("2", "step_started", step="s", model="m", provider="anthropic"),
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
        _ev("1", "phase_started", phase="p"),
        _ev("2", "step_started", step="s", model="m", provider="anthropic"),
        _ev("3", "step_context", agent_id="step:p/s", provider="anthropic", model="claude",
            system_prompt="SYSTEM PROMPT TEXT", tools_schema=[{"name": "bash"}],
            params={"max_tokens": 8192}, max_tool_calls=50),
    ], full=True))
    assert "SYSTEM PROMPT TEXT" in out
    assert "- bash" in out


def test_step_context_collapsed_even_in_default_mode() -> None:
    # design §5.1: step_context is folded by default in BOTH views.
    out = "\n".join(_lines([
        _ev("1", "phase_started", phase="p"),
        _ev("2", "step_started", step="s", model="m", provider="anthropic"),
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
