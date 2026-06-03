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
        _ev("1", "change_created", slug="add-auth-7", title="add auth"),
        _ev("3", "step_started", step="spec", kind="agent", phase="design",
            model="m", provider="anthropic"),
        _ev("4", "step_completed", step="spec", kind="agent",
            action={"verb": "continue"}, var_mutations={}),
    ]))
    assert "#add-auth-7" in out and "add auth" in out
    assert "design" in out
    assert "spec" in out
    assert "✓ spec" in out          # merged breadcrumb (was: "continue" in out)


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


def test_collapsed_folds_drop_thinking_and_tool_summaries() -> None:
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
    # Default mode drops both the thinking line and the tool-burst summary line.
    assert "thinking" not in out
    assert "tools" not in out
    assert "⏺" not in out


def test_collapsed_subagent_expands_with_outcome() -> None:
    out = _lines([
        _ev("2", "step_started", step="s", kind="agent", phase="p",
            model="m", provider="anthropic"),
        _ev("3", "step_context", agent_id="step:p/s", provider="anthropic",
            model="claude", system_prompt="S", tools_schema=[{"name": "a"}, {"name": "b"}],
            params={}, max_tool_calls=50),
        _ev("tk1", "tool_call", agent_id="step:p/s", id="b1", name="task",
            input={"subagent_name": "reviewer", "task_description": "go"}),
        _ev("su1", "llm_response", agent_id="sub:tk1", provider="anthropic", model="m",
            stop_reason="end_turn", usage={},
            raw={"content": [{"type": "text", "text": "looks good"}]}),
        _ev("r1", "tool_result", agent_id="step:p/s", id="b1",
            content='{"status":"Success","summary":"ok"}', error=False),
    ])
    joined = "\n".join(out)
    # Step-context still collapses to its one-line summary (untouched).
    assert "▸ context · anthropic/claude · 2 tools" in joined
    # Subagent: boundary header (no glyph, no `· N events`), recursed prose, outcome.
    assert any(ln.strip() == "↳ task → reviewer" for ln in out)
    assert "looks good" in joined
    assert any(ln.strip() == "│ → status=Success · summary=ok" for ln in out)
    # The old one-liner shape is gone.
    assert "▸ task → reviewer" not in joined
    assert "events" not in joined


def test_subagent_recurses_filters_nested_thinking_and_tools() -> None:
    out = "\n".join(_lines([
        _ev("2", "step_started", step="s", kind="agent", phase="p",
            model="m", provider="anthropic"),
        _ev("tk1", "tool_call", agent_id="step:p/s", id="b1", name="task",
            input={"subagent_name": "reviewer", "task_description": "go"}),
        _ev("t1", "thinking", agent_id="sub:tk1", content="nested musings"),
        _ev("c1", "tool_call", agent_id="sub:tk1", id="n1", name="bash", input={}),
        _ev("c2", "tool_result", agent_id="sub:tk1", id="n1", content="ok", error=False),
        _ev("su1", "llm_response", agent_id="sub:tk1", provider="anthropic", model="m",
            stop_reason="end_turn", usage={},
            raw={"content": [{"type": "text", "text": "nested prose"}]}),
        _ev("r1", "tool_result", agent_id="step:p/s", id="b1",
            content='{"status":"Success"}', error=False),
    ]))
    assert "nested prose" in out           # subagent prose recurses
    assert "nested musings" not in out     # nested thinking filtered (A1)
    assert "tools" not in out              # nested burst filtered (A2)
    assert "→ status=Success" in out       # outcome present


def test_subagent_outcome_present_even_with_prose() -> None:
    # Outcome is ALWAYS emitted when non-empty, alongside prose (decision ii).
    out = "\n".join(_lines([
        _ev("2", "step_started", step="s", kind="agent", phase="p",
            model="m", provider="anthropic"),
        _ev("tk1", "tool_call", agent_id="step:p/s", id="b1", name="task",
            input={"subagent_name": "reviewer", "task_description": "go"}),
        _ev("su1", "llm_response", agent_id="sub:tk1", provider="anthropic", model="m",
            stop_reason="end_turn", usage={},
            raw={"content": [{"type": "text", "text": "the prose"}]}),
        _ev("r1", "tool_result", agent_id="step:p/s", id="b1",
            content='{"status":"Success","summary":"done"}', error=False),
    ]))
    assert "the prose" in out
    assert "→ status=Success · summary=done" in out


# NOTE: there is intentionally NO doubly-nested (sub-subagent) test here.
# Routing is single-level by exact membership (narrative.py:763-770): a
# grandchild's `sub:<id>` events are NOT delegated into the child fold's nested
# builder, and spawned subagents receive `subagent_defs={}` so they cannot spawn
# subagents at all. A sub-subagent fixture would misroute (inner prose lands at
# the step level, inner fold stays empty) and any substring-presence assertion
# would pass for the wrong reason. The render code recurses correctly, but the
# scenario is unreachable until the engine enables nesting and routing becomes
# recursive/prefix-aware — out of scope for this plan.


def test_subagent_error_outcome_is_error_styled() -> None:
    out = list(_lines([
        _ev("2", "step_started", step="s", kind="agent", phase="p",
            model="m", provider="anthropic"),
        _ev("tk1", "tool_call", agent_id="step:p/s", id="b1", name="task",
            input={"subagent_name": "reviewer", "task_description": "go"}),
        _ev("r1", "tool_result", agent_id="step:p/s", id="b1",
            content="dispatch failed: boom", error=True),
    ], width=80))
    # Non-JSON error outcome renders as prose; verify it is present.
    assert any("dispatch failed: boom" in ln for ln in out)
    # And it is error-styled when color is on.
    color_out = list(render_tree(nv.build_tree([
        _ev("2", "step_started", step="s", kind="agent", phase="p",
            model="m", provider="anthropic"),
        _ev("tk1", "tool_call", agent_id="step:p/s", id="b1", name="task",
            input={"subagent_name": "reviewer", "task_description": "go"}),
        _ev("r1", "tool_result", agent_id="step:p/s", id="b1",
            content="dispatch failed: boom", error=True),
    ]), full=False, color=True, width=80))
    assert any("dispatch failed: boom" in ln and "\x1b[31m" in ln for ln in color_out)


def test_full_mode_also_emits_subagent_outcome() -> None:
    # --full must stay a superset of default: it expands thinking/tools AND emits
    # the structured return value (maintainer decision; deviation from the spec's
    # original "--full unchanged").
    out = "\n".join(_lines([
        _ev("2", "step_started", step="s", kind="agent", phase="p",
            model="m", provider="anthropic"),
        _ev("tk1", "tool_call", agent_id="step:p/s", id="b1", name="task",
            input={"subagent_name": "reviewer", "task_description": "go"}),
        _ev("su1", "llm_response", agent_id="sub:tk1", provider="anthropic", model="m",
            stop_reason="end_turn", usage={},
            raw={"content": [{"type": "text", "text": "verdict: approve"}]}),
        _ev("r1", "tool_result", agent_id="step:p/s", id="b1",
            content='{"status":"Success","summary":"ok"}', error=False),
    ], full=True))
    assert "▾ task → reviewer" in out      # expanded fold header (full shape)
    assert "verdict: approve" in out        # recursed prose
    assert "→ status=Success · summary=ok" in out   # outcome now present in full too


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


def test_agent_text_is_plain_when_color_enabled() -> None:
    node = nv.AgentTextNode(payload={})
    node._cache = _FakeRendered(["hello world"])
    out = list(rt._render(node, depth=0, full=False, color=True, width=80))
    # agent = () → calm baseline: prose carries NO ANSI at all.
    assert out == ["hello world"]


def test_empty_thinking_is_suppressed_both_modes() -> None:
    for content in ("", "   ", "\n\t "):
        node = nv.ThinkingFold(content=content)
        assert list(rt._render(node, depth=1, full=False, color=False, width=80)) == []
        assert list(rt._render(node, depth=1, full=True, color=False, width=80)) == []


def test_nonempty_thinking_dropped_in_default_mode() -> None:
    node = nv.ThinkingFold(content="abcde")
    assert list(rt._render(node, depth=1, full=False, color=False, width=80)) == []


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
                "GateNode", "ErrorNode", "StepContextNode", "OutcomeLine",
                "FallbackLine", "BreadcrumbLine", "RolesResolvedNode"}
    assert required <= seen, f"fixture missing node types: {required - seen}"


def test_literal_text_node_renders_as_agent_prose() -> None:
    from imp.cli import narrative as nv
    from imp.cli import render_text as rt
    node = nv.LiteralTextNode(text="line one\nline two")
    out = list(rt._render(node, depth=1, full=False, color=False, width=80))
    assert any("line one" in ln for ln in out)
    assert any("line two" in ln for ln in out)


def test_consecutive_prose_turns_separated_by_blank_line() -> None:
    # Two real prose turns with a thinking-only turn between them (which emits no
    # AgentTextNode and renders nothing in lean mode). They must NOT bunch — one
    # blank line separates them, restoring the spacing the empty-node used to give.
    out = _lines([
        _ev("1", "step_started", step="spec", kind="agent", phase="design"),
        _ev("2", "llm_response", agent_id="a",
            raw={"content": [{"type": "text", "text": "First prose turn."}]}, usage={}),
        _ev("3", "thinking", agent_id="a", content="hmm"),
        _ev("4", "llm_response", agent_id="a",   # thinking-only → no AgentTextNode
            raw={"content": [{"type": "thinking", "thinking": "hmm"}]}, usage={}),
        _ev("5", "llm_response", agent_id="a",
            raw={"content": [{"type": "text", "text": "Second prose turn."}]}, usage={}),
    ])
    body = [ln for ln in out if ln.strip()]   # sanity: both prose lines present
    assert any("First prose turn." in ln for ln in body)
    assert any("Second prose turn." in ln for ln in body)
    second = next(i for i, ln in enumerate(out) if "Second prose turn." in ln)
    assert out[second - 1].strip() == "", "consecutive prose turns must be blank-separated"


def test_single_prose_turn_has_no_leading_blank() -> None:
    # The FIRST prose block under a step must not get a spurious leading blank.
    out = _lines([
        _ev("1", "step_started", step="spec", kind="agent", phase="design"),
        _ev("2", "llm_response", agent_id="a",
            raw={"content": [{"type": "text", "text": "Only prose."}]}, usage={}),
    ])
    idx = next(i for i, ln in enumerate(out) if "Only prose." in ln)
    assert out[idx - 1].strip() != "", "step header should sit directly above first prose"


def test_roles_resolved_attaches_at_root() -> None:
    # Fired before any step_started (agent_id=None) → lands at the workflow root.
    b = nv.NarrativeBuilder()
    b.add({"id": "e1", "agent_id": None, "type": "change_created",
           "payload": {"change_id": 1, "title": "t"}})
    b.add({"id": "e2", "agent_id": None, "type": "roles_resolved",
           "payload": {"roles": {
               "code": {"provider": "anthropic", "model": "haiku",
                        "constraint": None, "differ_tier": None,
                        "differ_from": None}}}})
    root = b.tree.root
    nodes = [c for c in root.children if isinstance(c, nv.RolesResolvedNode)]
    assert len(nodes) == 1
    assert nodes[0].rows == [
        {"role": "code", "provider": "anthropic", "model": "haiku",
         "differ_tier": None, "differ_from": None}]


def test_roles_resolved_renders_constrained_and_unconstrained() -> None:
    node = nv.RolesResolvedNode(rows=[
        {"role": "code", "provider": "anthropic", "model": "haiku",
         "differ_tier": None, "differ_from": None},
        {"role": "code_review", "provider": "openai", "model": "gpt-5.2",
         "differ_tier": 1, "differ_from": "code"},
        {"role": "spec", "provider": "anthropic", "model": "opus",
         "differ_tier": 2, "differ_from": "code"},
    ])
    out = list(rt._render(node, depth=0, full=False, color=False, width=120))

    assert out[0] == "roles resolved:"

    # unconstrained: provider/model only, no differ annotation
    code_line = next(ln for ln in out if ln.lstrip().startswith("code "))
    assert "→ anthropic/haiku" in code_line
    assert "differ from" not in code_line

    # tier-1 constrained: vendor jump
    cr_line = next(ln for ln in out if "code_review" in ln)
    assert "→ openai/gpt-5.2" in cr_line
    assert "(differ from code: tier 1, vendor jump)" in cr_line

    # tier-2 constrained: same-vendor fallback
    spec_line = next(ln for ln in out if ln.lstrip().startswith("spec"))
    assert "→ anthropic/opus" in spec_line
    assert "(differ from code: tier 2, same-vendor fallback)" in spec_line


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


def _sub(outcome=None, *, is_error=False, name="reviewer"):
    """A SubagentFold with an empty nested builder and the given outcome string."""
    return nv.SubagentFold(subagent_name=name, event_id="e", block_id="b",
                           nested=nv.NarrativeBuilder(), outcome=outcome,
                           is_error=is_error)


def test_header_line_renders_subagent_boundary() -> None:
    sub = _sub(name="reviewer")
    # Subagent color, no fold glyph, no (running)/(error) suffix.
    assert rt.header_line(sub, depth=2, color=False) == "    ↳ task → reviewer"


def test_subagent_outcome_empty_yields_nothing() -> None:
    assert list(rt.subagent_outcome_lines(_sub(outcome=None), depth=1,
                                          color=False, width=80)) == []
    assert list(rt.subagent_outcome_lines(_sub(outcome=""), depth=1,
                                          color=False, width=80)) == []


def test_subagent_outcome_scalars_inline_and_block_with_real_newlines() -> None:
    outcome = json.dumps({"status": "Success", "blocking": False,
                          "report": "line one\nline two"})
    lines = list(rt.subagent_outcome_lines(_sub(outcome=outcome), depth=1,
                                           color=False, width=80))
    # Short scalars on one inline → line, at the passed depth (2-space indent).
    assert lines[0] == "  → status=Success · blocking=False"
    # The multi-line string field becomes a labeled block at the same depth,
    # body indented one level deeper.
    assert "  report:" in lines
    assert any("line one" in ln for ln in lines)
    assert any("line two" in ln for ln in lines)
    # The escaped \n from json.dumps must NOT leak through as a literal.
    assert not any("\\n" in ln for ln in lines)


def test_subagent_outcome_non_json_renders_as_prose() -> None:
    lines = list(rt.subagent_outcome_lines(_sub(outcome="dispatch failed: boom",
                                                is_error=True),
                                           depth=1, color=False, width=80))
    assert lines == ["  dispatch failed: boom"]


def test_gutter_indent_plain_when_no_gutters() -> None:
    assert rt._gutter_indent(0, (), color=False) == ""
    assert rt._gutter_indent(3, (), color=False) == "      "   # 3 * "  "
    # color on, no gutters → still plain spaces (no ANSI).
    assert rt._gutter_indent(2, (), color=True) == "    "


def test_gutter_indent_substitutes_magenta_bar_at_gutter_columns() -> None:
    # color=False: the │ glyph is plain text (NO_COLOR attribution).
    assert rt._gutter_indent(1, (0,), color=False) == "│ "
    assert rt._gutter_indent(3, (2,), color=False) == "    │ "      # cols 0,1 plain, col 2 bar
    assert rt._gutter_indent(2, (0, 1), color=False) == "│ │ "      # two nested levels
    # color=True: each bar column is an independently magenta-wrapped "│ ".
    bar = "\x1b[35m│ \x1b[0m"
    assert rt._gutter_indent(1, (0,), color=True) == bar
    assert rt._gutter_indent(2, (0, 1), color=True) == bar + bar


def test_wrap_returns_colored_gutter_composed_lines() -> None:
    # No gutters, color off: identical to the old indent+text shape.
    assert rt._wrap("a b", 80, 1, False) == ["  a b"]
    # No gutters, color on: content wrapped in codes, indent outside the span.
    assert rt._wrap("hi", 80, 1, True, "green") == ["  \x1b[32mhi\x1b[0m"]
    # With a gutter: magenta bar prefix, content colored independently.
    assert rt._wrap("hi", 80, 1, True, "green", gutters=(0,)) == \
        ["\x1b[35m│ \x1b[0m" if False else "\x1b[35m│ \x1b[0m\x1b[32mhi\x1b[0m"]
    # Blank paragraph line keeps the (gutter-bearing) indent only.
    assert rt._wrap("", 80, 1, False, gutters=(0,)) == ["│ "]


def test_wrap_width_is_invariant_under_gutter() -> None:
    # The │ substitutes within the indent, so wrap points don't move: the gutter
    # and the plain run wrap into the same number of lines.
    text = "word " * 40
    plain = rt._wrap(text, 40, 2, False)
    gutter = rt._wrap(text, 40, 2, False, gutters=(1,))
    assert len(plain) == len(gutter)


def test_subagent_content_carries_magenta_gutter() -> None:
    # color=False: the subagent's prose + outcome carry a plain `│ ` at the
    # subagent's column; the header (one level out) does NOT.
    out = _lines([
        _ev("2", "step_started", step="s", kind="agent", phase="p",
            model="m", provider="anthropic"),
        _ev("tk1", "tool_call", agent_id="step:p/s", id="b1", name="task",
            input={"subagent_name": "reviewer", "task_description": "go"}),
        _ev("su1", "llm_response", agent_id="sub:tk1", provider="anthropic", model="m",
            stop_reason="end_turn", usage={},
            raw={"content": [{"type": "text", "text": "looks good"}]}),
        _ev("r1", "tool_result", agent_id="step:p/s", id="b1",
            content='{"status":"Success"}', error=False),
    ])
    # header at depth 2 (under phase→step): 4 spaces, no gutter.
    assert any(ln == "    ↳ task → reviewer" for ln in out)
    # prose + outcome at depth 3: cols 0,1 plain + col 2 gutter.
    assert any(ln == "    │ looks good" for ln in out)
    assert any(ln == "    │ → status=Success" for ln in out)


def test_subagent_header_excludes_own_gutter() -> None:
    sub = _sub(name="reviewer")
    assert rt.header_line(sub, depth=0, color=False) == "↳ task → reviewer"
    # ancestor gutters show; the header's own column does not.
    assert rt.header_line(sub, depth=1, color=False, gutters=(0,)) == "│ ↳ task → reviewer"


def test_nested_subagent_emits_two_gutter_levels() -> None:
    # Sub-subagents can't be built through the event pipeline (single-level
    # routing), so construct the nested SubagentFold directly and render it.
    inner = nv.SubagentFold(subagent_name="inner", event_id="e2", block_id="b2",
                            nested=nv.NarrativeBuilder())
    inner.nested.tree.root.children.append(nv.LiteralTextNode(text="deep finding"))
    outer = nv.SubagentFold(subagent_name="outer", event_id="e1", block_id="b1",
                            nested=nv.NarrativeBuilder())
    outer.nested.tree.root.children.append(inner)
    out = list(rt._render(outer, depth=0, full=False, color=False, width=80))
    assert out[0] == "↳ task → outer"                       # depth 0, no gutter
    assert any(ln == "│ ↳ task → inner" for ln in out)      # depth 1, one gutter
    assert any(ln == "│ │ deep finding" for ln in out)      # depth 2, two gutters


def test_errored_subagent_gutter_stays_magenta_with_red_outcome() -> None:
    # seam 1 / error path: magenta gutter + red outcome TEXT on the same line.
    out = list(render_tree(nv.build_tree([
        _ev("2", "step_started", step="s", kind="agent", phase="p",
            model="m", provider="anthropic"),
        _ev("tk1", "tool_call", agent_id="step:p/s", id="b1", name="task",
            input={"subagent_name": "reviewer", "task_description": "go"}),
        _ev("r1", "tool_result", agent_id="step:p/s", id="b1",
            content="dispatch failed: boom", error=True),
    ]), full=False, color=True, width=80))
    line = next(ln for ln in out if "dispatch failed: boom" in ln)
    assert "\x1b[35m│ \x1b[0m" in line   # magenta gutter span (identity)
    assert "\x1b[31m" in line             # red outcome text span (status)


def test_full_subagent_nested_thinking_keeps_dim_text_and_gains_gutter() -> None:
    out = list(render_tree(nv.build_tree([
        _ev("2", "step_started", step="s", kind="agent", phase="p",
            model="m", provider="anthropic"),
        _ev("tk1", "tool_call", agent_id="step:p/s", id="b1", name="task",
            input={"subagent_name": "reviewer", "task_description": "go"}),
        _ev("t1", "thinking", agent_id="sub:tk1", content="nested musings"),
        _ev("r1", "tool_result", agent_id="step:p/s", id="b1",
            content='{"status":"Success"}', error=False),
    ]), full=True, color=True, width=80))
    line = next(ln for ln in out if "nested musings" in ln)
    assert "\x1b[35m│ \x1b[0m" in line   # magenta gutter
    assert "\x1b[2m" in line             # dim thinking text preserved
