# tests/test_render_markdown.py
from __future__ import annotations

from imp.cli import narrative as nv
from imp.cli.render_markdown import (
    _md_escape,
    _subagent_status_md,
    render_markdown,
)


def _ev(eid, etype, agent_id=None, **payload):
    return {"id": eid, "agent_id": agent_id, "type": etype, "payload": payload}


def _md(events, *, full=False):
    tree = nv.build_tree(events)
    return render_markdown(tree, full=full)


def _in_code_fence(md: str, needle: str) -> bool:
    """True when `needle` appears on a line that sits inside a ``` fence.
    Tracks the opening fence length so backtick runs inside the fenced body
    (which produce a longer fence) do not falsely toggle the state."""
    fence_len = None
    for line in md.split("\n"):
        stripped = line.lstrip()
        run = len(stripped) - len(stripped.lstrip("`"))
        if fence_len is None:
            if run >= 3:
                fence_len = run
            continue
        if run >= fence_len and stripped == "`" * run:
            fence_len = None
            continue
        if needle in line:
            return True
    return False


def test_md_escape_neutralizes_html_and_control_bytes() -> None:
    out = _md_escape("<script>&\x00")
    assert "&lt;script&gt;" in out
    assert "&amp;" in out
    assert "<script>" not in out
    assert "\x00" not in out
    assert "^@" in out            # C0/DEL caret-escaped via render_text._caret_escape


def test_heading_spine_workflow_phase_step() -> None:
    out = _md([
        _ev("1", "change_created", title="add auth"),
        _ev("2", "step_started", step="spec", kind="agent", phase="design",
            model="m", provider="anthropic"),
        _ev("3", "step_completed", step="spec", kind="agent",
            action={"verb": "continue"}, var_mutations={}),
    ])
    assert "# add auth" in out          # WorkflowNode → level-1 heading
    assert "## design" in out           # PhaseNode → level-2 heading
    assert "### spec" in out            # StepNode → level-3 heading


def test_heading_content_is_escaped_end_to_end() -> None:
    out = _md([
        _ev("1", "change_created", title="<evil>&"),
        _ev("2", "step_started", step="spec", kind="agent", phase="design",
            model="m", provider="anthropic"),
        _ev("3", "step_completed", step="spec", kind="agent",
            action={"verb": "continue"}, var_mutations={}),
    ])
    assert "# &lt;evil&gt;&amp;" in out
    assert "# <evil>" not in out


def test_agent_prose_escaped_and_passed_through() -> None:
    out = _md([
        _ev("2", "step_started", step="s", kind="agent", phase="p",
            model="m", provider="anthropic"),
        _ev("3", "llm_response", agent_id="step:p/s", provider="anthropic", model="m",
            stop_reason="end_turn", usage={},
            raw={"content": [{"type": "text",
                              "text": "hello **world** and <b>x</b>"}]}),
    ])
    assert "hello **world**" in out       # markdown formatting survives (not escaped)
    assert "&lt;b&gt;x&lt;/b&gt;" in out   # inline HTML is escaped
    assert "<b>x</b>" not in out


def test_hostile_agent_prose_is_escaped() -> None:
    out = _md([
        _ev("2", "step_started", step="s", kind="agent", phase="p",
            model="m", provider="anthropic"),
        _ev("3", "llm_response", agent_id="step:p/s", provider="anthropic", model="m",
            stop_reason="end_turn", usage={},
            raw={"content": [{"type": "text",
                              "text": "<script>alert(1)</script>\x00tail"}]}),
    ])
    assert "&lt;script&gt;" in out
    assert "<script>" not in out
    assert "\x00" not in out
    assert "^@" in out


def test_genuine_user_turn_is_blockquote_synthetic_dropped() -> None:
    out = _md([
        _ev("1", "step_started", step="s", kind="agent", phase="p", model="m"),
        _ev("2", "user_message", agent_id="step:p/s",
            content=[{"type": "text", "text": "begin"}]),          # synthetic kickoff
        _ev("3", "awaiting_user_input", agent_id="step:p/s"),
        _ev("4", "user_message", agent_id="step:p/s",
            content=[{"type": "text", "text": "do the real thing"}]),
    ])
    assert "> do the real thing" in out    # genuine human turn → blockquote
    assert "begin" not in out              # synthetic prompt dropped


def test_user_turn_escaped_and_multiline_blockquote() -> None:
    out = _md([
        _ev("1", "step_started", step="s", kind="agent", phase="p", model="m"),
        _ev("2", "awaiting_user_input", agent_id="step:p/s"),
        _ev("3", "user_message", agent_id="step:p/s",
            content=[{"type": "text", "text": "<b>bad</b>\nsecond line"}]),
    ])
    assert "> &lt;b&gt;bad&lt;/b&gt;" in out   # untrusted HTML escaped inside blockquote
    assert "<b>bad</b>" not in out
    assert "> second line" in out             # multi-line prefixed per line


def test_literal_text_node_is_escaped() -> None:
    root = nv.WorkflowNode(title="t")
    root.children = [nv.LiteralTextNode(text="<script>x</script>")]
    tree = nv.NarrativeTree(root=root, tokens=nv.TokenTotals())
    out = render_markdown(tree, full=False)
    assert "&lt;script&gt;x&lt;/script&gt;" in out
    assert "<script>" not in out


def _burst_events():
    return [
        _ev("2", "step_started", step="s", kind="agent", phase="p",
            model="m", provider="anthropic"),
        _ev("3", "thinking", agent_id="step:p/s", content="pondering deeply"),
        _ev("4", "tool_call", agent_id="step:p/s", id="b1", name="bash",
            input={"command": "ls"}),
        _ev("5", "tool_result", agent_id="step:p/s", id="b1",
            content="file-a\nfile-b", error=False),
    ]


def test_folds_collapse_to_details_by_default() -> None:
    out = _md(_burst_events())
    assert "<details>" in out
    assert "<summary>thinking</summary>" in out
    assert "pondering deeply" in out            # collapsed, but present inside details
    # tool burst recap appears as a <summary>; the call count is present
    assert "1 call" in out


def test_folds_expand_inline_under_full() -> None:
    out = _md(_burst_events(), full=True)
    assert "<details>" not in out               # expanded inline, no collapse wrapper
    assert "**thinking**" in out
    assert "pondering deeply" in out
    assert "```" in out                         # tool I/O in a fenced code block
    assert "file-a" in out and "file-b" in out  # tool result body present


def test_hostile_tool_result_inert_in_code_fence() -> None:
    events = [
        _ev("2", "step_started", step="s", kind="agent", phase="p",
            model="m", provider="anthropic"),
        _ev("4", "tool_call", agent_id="step:p/s", id="b1", name="bash",
            input={"command": "cat evil"}),
        _ev("5", "tool_result", agent_id="step:p/s", id="b1",
            content="<script>alert(1)</script>\x00", error=False),
    ]
    out = _md(events, full=True)
    assert _in_code_fence(out, "<script>")      # inert: sits inside a ``` fence
    assert "\x00" not in out                    # control byte neutralized
    assert "^@" in out


def test_step_context_fold_default_full_and_hostile_prompt() -> None:
    ctx = nv.StepContextNode(payload={"provider": "anthropic", "model": "claude",
                                      "max_tool_calls": 50,
                                      "system_prompt": "<script>alert(1)</script>"})
    root = nv.WorkflowNode(title="t")
    root.children = [ctx]
    tree = nv.NarrativeTree(root=root, tokens=nv.TokenTotals())

    default = render_markdown(tree, full=False)
    assert "<summary>context · anthropic/claude</summary>" in default

    full = render_markdown(tree, full=True)
    assert "**context · anthropic/claude**" in full
    assert "provider: anthropic" in full
    assert "max_tool_calls: 50" in full
    assert _in_code_fence(full, "<script>")   # untrusted system_prompt is fenced/inert


def _subagent_events():
    return [
        _ev("2", "step_started", step="s", kind="agent", phase="p",
            model="m", provider="anthropic"),
        _ev("tk1", "tool_call", agent_id="step:p/s", id="b1", name="sub_agent",
            input={"subagent_name": "reviewer", "task_description": "go"}),
        _ev("su1", "llm_response", agent_id="sub:tk1", provider="anthropic",
            model="m", stop_reason="end_turn", usage={},
            raw={"content": [{"type": "text", "text": "looks good"}]}),
        _ev("r1", "tool_result", agent_id="step:p/s", id="b1",
            content='{"status":"Success","summary":"all clear"}', error=False),
    ]


def test_subagent_lean_row_default_no_children_no_io() -> None:
    out = _md(_subagent_events())
    assert "· reviewer" in out          # lean one-line status row
    assert "looks good" not in out      # no recursed subagent I/O
    assert "all clear" not in out       # no outcome in default mode
    assert "<details>" not in out       # a subagent fold is NOT a <details>


def test_subagent_expands_inline_under_full() -> None:
    out = _md(_subagent_events(), full=True)
    assert "**reviewer**" in out        # expanded fold header
    assert "looks good" in out          # recursed subagent narrative
    assert "all clear" in out           # structured outcome rendered
    assert "<details>" not in out       # a subagent fold is never a <details>


def _make_subagent(**kw):
    defaults = dict(subagent_name="reviewer", event_id="e", block_id="b",
                    nested=nv.NarrativeBuilder(), running=False)
    defaults.update(kw)
    return nv.SubagentFold(**defaults)


def test_subagent_status_tags_and_elapsed() -> None:
    assert "(running)" in _subagent_status_md(_make_subagent(running=True))
    assert "(stalled)" in _subagent_status_md(
        _make_subagent(terminal_state="stalled"))
    assert "(interrupted)" in _subagent_status_md(
        _make_subagent(terminal_state="interrupted"))
    assert "(context exhausted)" in _subagent_status_md(
        _make_subagent(terminal_state="context_exhausted"))
    assert "(failed)" in _subagent_status_md(_make_subagent(is_error=True))
    row = _subagent_status_md(_make_subagent(start_ts="2024-01-01T00:00:00Z",
                                             end_ts="2024-01-01T00:00:05Z"))
    assert "reviewer" in row and "5" in row   # elapsed suffix present


def test_completion_breadcrumb_and_end_to_end_structure() -> None:
    out = _md([
        _ev("1", "change_created", title="add auth"),
        _ev("2", "step_started", step="spec", kind="agent", phase="design",
            model="m", provider="anthropic"),
        _ev("3", "llm_response", agent_id="step:design/spec", provider="anthropic",
            model="m", stop_reason="end_turn", usage={},
            raw={"content": [{"type": "text", "text": "drafted the spec"}]}),
        _ev("4", "step_completed", step="spec", kind="agent",
            action={"verb": "continue"}, var_mutations={}),
    ])
    # Spine + prose + a completion line, in document order.
    assert out.index("# add auth") < out.index("## design") < out.index("### spec")
    assert "drafted the spec" in out
    assert "_spec · complete_" in out             # exact BreadcrumbLine status line


def test_error_node_renders_as_blockquote_callout() -> None:
    out = _md([
        _ev("2", "step_started", step="s", kind="agent", phase="p", model="m"),
        _ev("3", "error", agent_id="step:p/s", message="boom", category="script"),
    ])
    assert "> **error:**" in out
    assert "boom" in out


def test_pause_node_renders_status_line() -> None:
    out = _md([
        _ev("2", "step_started", step="s", kind="agent", phase="p", model="m"),
        _ev("3", "workflow_paused", agent_id="step:p/s", reason="ctrl_c"),
    ])
    assert "paused" in out
    assert "ctrl_c" in out


def test_structure_covers_line_and_footer_nodes() -> None:
    # Golden/structure guard: a hand-built tree exercising the remaining line and
    # footer branches so they stay covered against regression. Built directly (not
    # via events) because these nodes arise from many disparate event shapes.
    root = nv.WorkflowNode(title="demo")
    phase = nv.PhaseNode(name="design")
    step = nv.StepNode(name="spec", kind="agent")
    step.children = [
        nv.SkillLoadNode(name="brainstorming"),
        nv.OutcomeLine(action={"verb": "continue"}),
        nv.RenameNode(name="new title"),
        nv.StepContextNode(payload={"provider": "anthropic", "model": "claude",
                                    "max_tool_calls": 50, "system_prompt": "SYS"}),
    ]
    phase.children = [step]
    root.children = [
        phase,
        nv.TiersResolvedNode(rows=[{"tier": "high", "provider": "anthropic",
                                    "model": "claude", "differ_tier": None,
                                    "differ_from": None}]),
        nv.WarningsSummaryNode(lines=[("disk low", 2)], total=2),
        nv.CompletedNode(change_id="gumu81"),
        nv.MergedNode(change_id="gumu81", base="main"),
        nv.FallbackLine(event_type="odd_event", payload={"k": "v"}),
    ]
    tree = nv.NarrativeTree(root=root, tokens=nv.TokenTotals())
    out = render_markdown(tree, full=True)
    assert "loading skill brainstorming" in out        # SkillLoadNode
    assert "→ continue" in out                         # OutcomeLine
    assert 'Set name to "new title"' in out            # RenameNode
    assert "context · anthropic/claude" in out         # StepContextNode fold (full)
    assert "tiers resolved:" in out                    # TiersResolvedNode (full)
    assert "- high → anthropic/claude" in out          # tiers list item
    assert "**2 warnings this run**" in out            # WarningsSummaryNode
    assert "disk low" in out and "(×2)" in out
    assert "**✓ Completed gumu81.**" in out            # CompletedNode
    assert "**✓ Merged gumu81 into main.**" in out     # MergedNode
    assert "odd_event k=v" in out                      # FallbackLine


def test_error_node_detail_block_rendered_and_escaped() -> None:
    root = nv.WorkflowNode(title="t")
    root.children = [nv.ErrorNode(message="boom", category="script",
                                  detail="line1 <x>\nline2")]
    tree = nv.NarrativeTree(root=root, tokens=nv.TokenTotals())
    out = render_markdown(tree, full=True)
    assert "> **error:** [script] boom" in out
    assert "> line1 &lt;x&gt;" in out     # detail escaped + per-line blockquoted
    assert "> line2" in out
    assert "line1 <x>" not in out         # raw untrusted detail never leaks


def test_error_node_warning_callout() -> None:
    root = nv.WorkflowNode(title="t")
    root.children = [nv.ErrorNode(message="disk low", is_warning=True)]
    tree = nv.NarrativeTree(root=root, tokens=nv.TokenTotals())
    out = render_markdown(tree, full=True)
    assert "> **warning:** disk low" in out
    assert "> **error:**" not in out


def test_tiers_resolved_suppressed_in_default_mode() -> None:
    root = nv.WorkflowNode(title="t")
    root.children = [nv.TiersResolvedNode(rows=[{"tier": "high",
                                                 "provider": "anthropic",
                                                 "model": "claude",
                                                 "differ_tier": None,
                                                 "differ_from": None}])]
    tree = nv.NarrativeTree(root=root, tokens=nv.TokenTotals())
    assert "tiers resolved" not in render_markdown(tree, full=False)
    assert "tiers resolved" in render_markdown(tree, full=True)


def test_breadcrumb_interrupted_variant() -> None:
    root = nv.WorkflowNode(title="t")
    root.children = [nv.BreadcrumbLine(step_name="spec", interrupted=True)]
    tree = nv.NarrativeTree(root=root, tokens=nv.TokenTotals())
    out = render_markdown(tree, full=True)
    assert "_spec interrupted_" in out
