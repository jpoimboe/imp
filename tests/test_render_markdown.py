# tests/test_render_markdown.py
from __future__ import annotations

import pytest

from imp.cli import narrative as nv
from imp.cli.accounting import RowUsage
from imp.cli.render_markdown import (
    _md_escape,
    _subagent_status_md,
    render_markdown,
    render_summary_markdown,
)
from imp.cli.render_summary import (
    StepRow,
    SubagentRow,
    SummaryHeader,
    build_summary,
    summary_total,
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
        _ev("1", "quest_created", title="add auth"),
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
        _ev("1", "quest_created", title="<evil>&"),
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
        _ev("3", "agent_message", agent_id="step:p/s", provider="anthropic", model="m",
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
        _ev("3", "agent_message", agent_id="step:p/s", provider="anthropic", model="m",
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
        _ev("2", "engine_message", agent_id="step:p/s",
            subtype="agent_prompt", role="user",
            content=[{"type": "text", "text": "begin"}]),          # synthetic kickoff
        _ev("3", "awaiting_user_input", agent_id="step:p/s"),
        _ev("4", "user_message", agent_id="step:p/s",
            subtype="reply", role="user",
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
    tree = nv.NarrativeTree(root=root)
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
    sys_node = nv.EngineMessageNode(text="<script>alert(1)</script>",
                                    subtype="system_prompt")
    ctx = nv.StepContextNode(payload={"provider": "anthropic", "model": "claude",
                                      "max_tool_calls": 50})
    root = nv.WorkflowNode(title="t")
    root.children = [sys_node, ctx]
    tree = nv.NarrativeTree(root=root)

    default = render_markdown(tree, full=False)
    assert "<summary>Internal context — anthropic/claude</summary>" in default
    assert "context ·" not in default

    full = render_markdown(tree, full=True)
    assert "**Internal context — anthropic/claude**" in full
    assert "provider: anthropic" in full
    assert "max_tool_calls: 50" in full
    assert _in_code_fence(full, "<script>")   # untrusted system_prompt is fenced/inert


def _subagent_events():
    return [
        _ev("2", "step_started", step="s", kind="agent", phase="p",
            model="m", provider="anthropic"),
        _ev("tk1", "tool_call", agent_id="step:p/s", id="b1", name="sub_agent",
            input={"subagent_name": "reviewer", "task_description": "go"}),
        _ev("su1", "agent_message", agent_id="sub:tk1", provider="anthropic",
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
        _ev("1", "quest_created", title="add auth"),
        _ev("2", "step_started", step="spec", kind="agent", phase="design",
            model="m", provider="anthropic"),
        _ev("3", "agent_message", agent_id="step:design/spec", provider="anthropic",
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
                                    "max_tool_calls": 50}),
    ]
    phase.children = [step]
    root.children = [
        phase,
        nv.TiersResolvedNode(rows=[{"tier": "high", "provider": "anthropic",
                                    "model": "claude", "differ_tier": None,
                                    "differ_from": None}]),
        nv.WarningsSummaryNode(lines=[("disk low", 2)], total=2),
        nv.CompletedNode(quest_id="gumu81"),
        nv.MergedNode(quest_id="gumu81", base="main"),
        nv.FallbackLine(event_type="odd_event", payload={"k": "v"}),
    ]
    tree = nv.NarrativeTree(root=root)
    out = render_markdown(tree, full=True)
    assert "loading skill brainstorming" in out        # SkillLoadNode
    assert "→ continue" in out                         # OutcomeLine
    assert 'Set name to "new title"' in out            # RenameNode
    assert "Internal context — anthropic/claude" in out  # ctx fold
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
    tree = nv.NarrativeTree(root=root)
    out = render_markdown(tree, full=True)
    assert "> **error:** [script] boom" in out
    assert "> line1 &lt;x&gt;" in out     # detail escaped + per-line blockquoted
    assert "> line2" in out
    assert "line1 <x>" not in out         # raw untrusted detail never leaks


def test_error_node_warning_callout() -> None:
    root = nv.WorkflowNode(title="t")
    root.children = [nv.ErrorNode(message="disk low", is_warning=True)]
    tree = nv.NarrativeTree(root=root)
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
    tree = nv.NarrativeTree(root=root)
    assert "tiers resolved" not in render_markdown(tree, full=False)
    assert "tiers resolved" in render_markdown(tree, full=True)


def test_breadcrumb_interrupted_variant() -> None:
    root = nv.WorkflowNode(title="t")
    root.children = [nv.BreadcrumbLine(step_name="spec", interrupted=True)]
    tree = nv.NarrativeTree(root=root)
    out = render_markdown(tree, full=True)
    assert "_spec interrupted_" in out


def _tev(eid, etype, *, timestamp, **payload):
    return {"id": eid, "type": etype, "timestamp": timestamp, "payload": payload}


def test_summary_markdown_carries_header_and_step_table() -> None:
    events = [
        _tev("1", "quest_created", timestamp="2024-01-01T00:00:00Z", title="x"),
        _tev("2", "step_started", timestamp="2024-01-01T00:00:00Z",
             step="spec", kind="agent", phase="design"),
        _tev("3", "step_completed", timestamp="2024-01-01T00:01:00Z",
             step="spec", kind="agent", action={"verb": "continue"}, var_mutations={}),
    ]
    header = SummaryHeader(workflow="imp", quest_id="gumu81",
                           status="merged", total_seconds=summary_total(events))
    out = render_summary_markdown(build_summary(events), header=header)
    assert "# imp — gumu81" in out               # SummaryHeader workflow/id
    assert "merged" in out                        # status carried
    assert "## design" in out                     # phase grouping heading
    assert ("| Step | Duration | Fresh | Cache read | Cache write | Out | Cost |"
            in out)
    assert "Wait" not in out                      # Wait column dropped
    assert "| spec | 1m 0s | — | — | — | — | — |" in out
    assert "**Status:** merged" in out            # AC 22(d): identity only
    assert "**Total:**" not in out
    assert "**Tokens:**" not in out
    assert "**Cost:**" not in out


def test_summary_markdown_durations_exclude_wait_and_drop_wait_column() -> None:
    rows = [
        StepRow(phase="design", step="waited",
                start_ts="2024-01-01T00:00:00Z",
                end_ts="2024-01-01T00:00:30Z", wait_seconds=10.0),
        StepRow(phase="design", step="open",
                start_ts="2024-01-01T00:00:00Z", end_ts=None),
    ]
    header = SummaryHeader(workflow="imp", quest_id="c",
                           status="running", total_seconds=None)
    out = render_summary_markdown(rows, header=header)
    assert "Wait" not in out                          # Wait column dropped
    assert "| waited | 20s | — | — | — | — | — |" in out   # 30s wall - 10s wait
    assert "| open | running | — | — | — | — | — |" in out  # open step
    assert "**Total:** running" not in out                 # the total moved out


def test_summary_multiple_phases_grouped_in_order() -> None:
    rows = [
        StepRow(phase="design", step="spec",
                start_ts="2024-01-01T00:00:00Z",
                end_ts="2024-01-01T00:00:10Z"),
        StepRow(phase="build", step="impl",
                start_ts="2024-01-01T00:00:10Z",
                end_ts="2024-01-01T00:00:20Z"),
    ]
    header = SummaryHeader(workflow="imp", quest_id="c",
                           status="merged", total_seconds=30.0)
    out = render_summary_markdown(rows, header=header)
    assert out.index("## design") < out.index("## build")
    assert "| spec |" in out and "| impl |" in out


def test_summary_markdown_renders_subagent_rows_with_figures() -> None:
    rows = [
        StepRow(phase="implement", step="task-lead",
                start_ts="2024-01-01T00:00:00Z",
                end_ts="2024-01-01T00:01:00Z",
                usage=RowUsage(fresh_input=12000, cached_input=200,
                               cache_write=100, output=345, dollars=0.34,
                               priced=1),
                subagents=[SubagentRow(
                    name="explorer", label="map repo",
                    start_ts="2024-01-01T00:00:05Z",
                    end_ts="2024-01-01T00:00:50Z",
                    usage=RowUsage(fresh_input=200, output=20, dollars=0.05,
                                   priced=1))]),
    ]
    header = SummaryHeader(workflow="imp", quest_id="c",
                           status="merged", total_seconds=60.0)
    out = render_summary_markdown(rows, header=header)
    # AC 22(a): the four buckets are broken out, not collapsed into one figure.
    assert ("| task-lead | 1m 0s | 12.2k | 200 | 100 | 365 | ~$0.39 |"
            in out)
    assert ("| ↳ explorer (map repo) | 45s | 200 | — | — | 20 | ~$0.05 |"
            in out)


def test_summary_markdown_open_step_reflects_paused_status() -> None:
    hdr = SummaryHeader(workflow="feature", quest_id="abc",
                        status="paused", total_seconds=100.0)
    rows = [StepRow(phase="implement", step="task-lead",
                    start_ts="2026-01-01T00:00:00+00:00", end_ts=None)]
    out = render_summary_markdown(rows, header=hdr)
    assert "| task-lead | paused |" in out
    assert "| task-lead | running |" not in out


def test_markdown_full_renders_engine_message_labels():
    out = _md([
        _ev("1", "step_started", step="plan", kind="agent", phase="build",
            model="m", provider="anthropic"),
        _ev("e1", "engine_message", agent_id="step:build/plan",
            subtype="continuation", role="user",
            content=[{"type": "text", "text": "keep going"}]),
    ], full=True)
    assert "continuation" in out
    assert "keep going" in out


def test_markdown_step_context_has_no_system_prompt():
    out = _md([
        _ev("1", "step_started", step="plan", kind="agent", phase="build",
            model="m", provider="anthropic"),
        _ev("c1", "step_context", agent_id="step:build/plan",
            provider="anthropic", model="m", tools_schema=[], params={},
            max_tool_calls=40, system_prompt="do the thing SECRETLY"),
    ], full=True)
    assert "system_prompt:" not in out
    assert "Internal context" in out
    assert "do the thing SECRETLY" not in out


def test_markdown_lean_hides_engine_message():
    out = _md([
        _ev("1", "step_started", step="plan", kind="agent", phase="build",
            model="m", provider="anthropic"),
        _ev("e1", "engine_message", agent_id="step:build/plan",
            subtype="continuation", role="user",
            content=[{"type": "text", "text": "keep going"}]),
    ], full=False)
    assert "keep going" not in out
    assert "continuation" not in out


def test_markdown_pause_keeps_the_raw_reason() -> None:
    out = _md([
        _ev("1", "step_started", step="verify", kind="agent", phase="implement",
            model="m", provider="anthropic"),
        _ev("2", "workflow_paused", reason="max_loops"),
    ])
    assert "**paused (max_loops)**" in out
    assert "loop limit" not in out


def test_markdown_warning_omits_the_sandbox_detail() -> None:
    # AC 6: the markdown renderer is deliberately unchanged — a warning stays a
    # one-line blockquote there; only the text renderer gained the detail line.
    md = _md([
        _ev("1", "step_started", step="build", kind="agent", phase="implement",
            model="m", provider="anthropic"),
        _ev("2", "runner_warning",
            message="sandbox could not start the command; retrying (2/5)",
            detail="bwrap: Can't bind mount /x: No such file or directory"),
    ])
    assert "> **warning:** sandbox could not start the command; retrying (2/5)" in md
    assert "bwrap" not in md


def test_quiet_first_retry_is_omitted_from_the_lean_markdown() -> None:
    events = [_ev("r1", "agent_retry", category="auth", attempt=1,
                  backoff_seconds=1.0, message="401 revoked")]
    assert "retry 1" not in _md(events)
    assert ("> **warning:** [auth] retry 1 (1.0s) — 401 revoked"
            in _md(events, full=True))


def test_second_retry_still_shows_in_the_lean_markdown() -> None:
    events = [_ev("r1", "agent_retry", category="auth", attempt=2,
                  backoff_seconds=2.0, message="401 revoked")]
    assert "> **warning:** [auth] retry 2 (2.0s) — 401 revoked" in _md(events)


def test_summary_markdown_marks_inferred_ends_with_a_tilde() -> None:
    rows = [StepRow(phase="implement", step="task-lead",
                    start_ts="2026-01-01T00:00:00+00:00",
                    end_ts="2026-01-01T00:05:00+00:00", end_inferred=True,
                    subagents=[SubagentRow(name="coder",
                                           start_ts="2026-01-01T00:00:10+00:00",
                                           end_ts="2026-01-01T00:02:24+00:00",
                                           end_inferred=True)])]
    hdr = SummaryHeader(workflow="imp", quest_id="c", status="merged",
                        total_seconds=300.0)
    out = render_summary_markdown(rows, header=hdr)
    assert "| task-lead | ~5m 0s | — | — | — | — | — |" in out
    assert "| ↳ coder | ~2m 14s | — | — | — | — | — |" in out


def test_summary_markdown_open_spans_in_a_finished_quest_read_interrupted() -> None:
    rows = [StepRow(phase="implement", step="task-lead",
                    start_ts="2026-01-01T00:00:00+00:00", end_ts=None,
                    subagents=[SubagentRow(name="coder",
                                           start_ts="2026-01-01T00:00:10+00:00")])]
    hdr = SummaryHeader(workflow="imp", quest_id="c", status="merged",
                        total_seconds=300.0)
    out = render_summary_markdown(rows, header=hdr)
    assert "| task-lead | interrupted | — | — | — | — | — |" in out
    assert "| ↳ coder | interrupted | — | — | — | — | — |" in out
    assert "| task-lead | merged |" not in out


def test_summary_markdown_merge_blocked_open_step_reads_two_words() -> None:
    rows = [StepRow(phase="implement", step="task-lead",
                    start_ts="2026-01-01T00:00:00+00:00", end_ts=None)]
    hdr = SummaryHeader(workflow="imp", quest_id="c", status="merge_blocked",
                        total_seconds=100.0)
    out = render_summary_markdown(rows, header=hdr)
    assert "| task-lead | merge blocked | — | — | — | — | — |" in out


def test_agent_prose_colored_fence_exports_stripped_not_caret_escaped() -> None:
    out = _md([
        _ev("2", "step_started", step="s", kind="agent", phase="p",
            model="m", provider="anthropic"),
        _ev("3", "agent_message", agent_id="step:p/s", provider="anthropic", model="m",
            stop_reason="end_turn", usage={},
            raw={"content": [{"type": "text",
                              "text": "Look:\n\n```\n"
                                      "\x1b[1;33m▌ imp\x1b[0m planning\n```\n"}]}),
    ])
    assert "\x1b" not in out          # no escape bytes
    assert "^[[" not in out           # and no caret form either — SGR is dropped
    assert "▌ imp planning" in out    # the mockup's text survives


def test_tool_call_fence_exports_without_sgr_noise() -> None:
    out = _md([
        _ev("2", "step_started", step="s", kind="agent", phase="p",
            model="m", provider="anthropic"),
        _ev("3", "tool_call", agent_id="step:p/s", id="b1", name="bash",
            input={"command": "\x1b[31mred\x1b[0m"}),
    ], full=True)
    assert "\x1b" not in out
    assert "^[[" not in out
    assert "command=red" in out


def test_summary_markdown_carries_a_totals_row() -> None:
    # AC 22(b) / AC 19: the totals row holds what the rollup line dropped.
    rows = [
        StepRow(phase="implement", step="a",
                start_ts="2024-01-01T00:00:00Z", end_ts="2024-01-01T00:00:30Z",
                usage=RowUsage(fresh_input=1000, cached_input=200,
                               cache_write=100, output=40, dollars=0.20,
                               priced=1)),
        StepRow(phase="review", step="b",
                start_ts="2024-01-01T00:00:30Z", end_ts="2024-01-01T00:01:00Z",
                usage=RowUsage(fresh_input=1000, output=10, dollars=0.10,
                               priced=1),
                subagents=[SubagentRow(
                    name="coder",
                    start_ts="2024-01-01T00:00:35Z",
                    end_ts="2024-01-01T00:00:55Z",
                    usage=RowUsage(fresh_input=500, output=10, dollars=0.20,
                                   priced=1))]),
    ]
    header = SummaryHeader(workflow="imp", quest_id="c", status="merged",
                           total_seconds=60.0)
    out = render_summary_markdown(rows, header=header)
    assert "| total | 1m 0s | 2.5k | 200 | 100 | 60 | ~$0.50 |" in out
    assert out.index("## review") < out.index("| total |")


def test_summary_markdown_shows_the_legend_lines_for_the_cells_it_rendered():
    # AC 22(c) / AC 18: a mixed-auth run, tilde line first.
    rows = [
        StepRow(phase="p", step="metered",
                start_ts="2024-01-01T00:00:00Z", end_ts="2024-01-01T00:00:30Z",
                usage=RowUsage(fresh_input=100, output=10, dollars=2.00,
                               priced=1)),
        StepRow(phase="p", step="subscription",
                start_ts="2024-01-01T00:00:30Z", end_ts="2024-01-01T00:01:00Z",
                usage=RowUsage(fresh_input=100, output=10, dollars=4.00,
                               priced=1, notional=1)),
    ]
    header = SummaryHeader(workflow="imp", quest_id="c", status="merged",
                           total_seconds=60.0)
    out = render_summary_markdown(rows, header=header)
    tilde = "~ estimated at API list price"
    star = ("\\* theoretical API list price;"
            " actual subscription usage is much lower")
    floor = "≥ excludes steps run on a model absent from the price table"
    assert tilde in out and star in out
    assert out.index(tilde) < out.index(star)
    assert floor not in out                      # no unpriced model here


def _cc_fallback_events() -> list[dict]:
    """A claude step whose query_result carries token counts but no
    `total_cost_usd` — the shape the price-table fallback prices."""
    return [
        {"type": "step_started", "timestamp": "2024-01-01T00:00:00Z",
         "payload": {"step": "reviewer", "phase": "build"}},
        {"type": "agent_message", "agent_id": "a1",
         "timestamp": "2024-01-01T00:00:01Z",
         "payload": {"provider": "claude", "model": "claude-sonnet-4-6",
                     "usage": {"input_tokens": 9999, "output_tokens": 8888}}},
        {"type": "query_result", "agent_id": "a1",
         "timestamp": "2024-01-01T00:00:02Z",
         "payload": {"session_id": "s1", "usage": {},
                     "model_usage": {"claude-sonnet-4-6": {
                         "inputTokens": 5000, "outputTokens": 8000,
                         "cacheReadInputTokens": 200000,
                         "cacheCreationInputTokens": 0}}}},
        {"type": "step_completed", "timestamp": "2024-01-01T00:01:00Z",
         "payload": {"step": "reviewer", "action": {"verb": "continue"}}},
    ]


def test_summary_markdown_prices_a_vendor_less_claude_row():
    # AC 10: the export calls the same cost_cell and legend_lines, so the
    # fallback figure and its legend arrive with no work of its own.
    rows = build_summary(_cc_fallback_events())
    header = SummaryHeader(workflow="imp", quest_id="c", status="merged",
                           total_seconds=60.0)
    out = render_summary_markdown(rows, header=header)
    lines = out.split("\n")
    step_line = next(ln for ln in lines if ln.startswith("| reviewer |"))
    total_line = next(ln for ln in lines if ln.startswith("| total |"))
    assert step_line.endswith("| $0.20* |")
    assert total_line.endswith("| $0.20* |")
    legend = ("\\* theoretical API list price; actual subscription usage is"
              " much lower")
    assert out.rstrip("\n").endswith(legend)
    assert lines.index(total_line) < lines.index(legend)


def test_summary_markdown_with_no_steps_keeps_the_time_and_drops_the_legend():
    # Same reachable window as the terminal view: no step rows, but a real span.
    header = SummaryHeader(workflow="imp", quest_id="c", status="running",
                           total_seconds=2342.0)
    out = render_summary_markdown([], header=header)
    assert "| total | 39m 2s | — | — | — | — | — |" in out
    assert "estimated at API list price" not in out   # nothing was priced
    assert "##" not in out                            # no phase headings


def test_rotation_race_retry_renders_its_copy_in_markdown() -> None:
    events = [_ev("r1", "agent_retry", category="auth", kind="token_rotated",
                  attempt=2, backoff_seconds=1.0, message="401 revoked")]
    assert ("> **warning:** [auth] retry 2 (1.0s) — another session rotated the "
            "OAuth token; retrying with the new one") in _md(events)


_BARE_MOCKUP = ("[38;2;255;154;60mwarning: [auth] retry 2 (1.0s) — another session "
                "rotated the OAuth token; retrying with the new one[0m")


def test_agent_prose_bare_sgr_fence_exports_stripped() -> None:
    out = _md([
        _ev("2", "step_started", step="s", kind="agent", phase="p",
            model="m", provider="anthropic"),
        _ev("3", "agent_message", agent_id="step:p/s", provider="anthropic", model="m",
            stop_reason="end_turn", usage={},
            raw={"content": [{"type": "text",
                              "text": f"Look:\n\n```\n{_BARE_MOCKUP}\n```\n"}]}),
    ])
    assert "38;2" not in out          # the bare code does not leak into the export
    assert "\x1b" not in out          # nor an escape byte
    assert "^[[" not in out           # nor caret noise
    assert "warning: [auth] retry 2 (1.0s)" in out   # the mockup's text survives


def test_agent_prose_export_keeps_an_imp_model_id_intact() -> None:
    # Without the `]` lookahead the normalizer arms `[1m`, and `_strip_sgr` then
    # DELETES it — silently exporting `claude-opus-4-8]`.
    out = _md([
        _ev("2", "step_started", step="s", kind="agent", phase="p",
            model="m", provider="anthropic"),
        _ev("3", "agent_message", agent_id="step:p/s", provider="anthropic", model="m",
            stop_reason="end_turn", usage={},
            raw={"content": [{"type": "text",
                              "text": "```\nhigh: claude-opus-4-8[1m]\n```\n"}]}),
    ])
    assert "claude-opus-4-8[1m]" in out


def test_markdown_breadcrumb_carries_the_round_then_the_tally() -> None:
    out = _md([
        _ev("1", "quest_created", title="t"),
        _ev("2", "step_started", step="plan-reviewer", kind="agent", phase="planning",
            label="reviewing plan", round=2),
        _ev("3", "agent_result", agent_id="step:planning/plan-reviewer",
            status="Success", critical=0, major=2, minor=1),
        _ev("4", "step_completed", step="plan-reviewer", kind="agent",
            action={"verb": "continue"}, var_mutations={}),
    ])
    assert "_reviewing plan · complete · round 2 · 2 major, 1 minor_" in out


def test_markdown_shows_a_body_less_step_round_through_its_breadcrumb() -> None:
    # `verify` collapses to a single line in the text renderers; markdown has no
    # collapsed form, so neither surface omits the round.
    out = _md([
        _ev("1", "quest_created", title="t"),
        _ev("2", "step_started", step="verify", kind="script", phase="implement",
            label="running tests", round=2),
        _ev("3", "step_completed", step="verify", kind="script",
            action={"verb": "continue"}, var_mutations={}),
    ])
    assert "_running tests · complete · round 2_" in out


def test_agent_prose_colored_inline_span_exports_as_a_plain_code_span() -> None:
    out = _md([
        _ev("2", "step_started", step="s", kind="agent", phase="p",
            model="m", provider="anthropic"),
        _ev("3", "agent_message", agent_id="step:p/s", provider="anthropic", model="m",
            stop_reason="end_turn", usage={},
            raw={"content": [{"type": "text",
                              "text": "The gauge reads `[1;33m78%[0m` amber.\n"}]}),
    ])
    assert "The gauge reads `78%` amber." in out   # the visible sample, still a span
    assert "[1;33m" not in out                     # the documented "exports strip SGR"
    assert "\x1b" not in out                       # nor an escape byte
    assert "^[[" not in out                        # nor caret noise


def test_agent_prose_all_codes_span_exports_as_its_literal_text() -> None:
    # Arming an all-codes span would strip it to empty backticks, deleting the very
    # codes the sentence is about.
    text = "Write each SGR code without it — `[1;33m`, `[0m` — and imp restores it."
    out = _md([
        _ev("2", "step_started", step="s", kind="agent", phase="p",
            model="m", provider="anthropic"),
        _ev("3", "agent_message", agent_id="step:p/s", provider="anthropic", model="m",
            stop_reason="end_turn", usage={},
            raw={"content": [{"type": "text", "text": text + "\n"}]}),
    ])
    assert text in out          # both codes present, as two separate code spans
    assert "``" not in out      # no collapsed / empty backtick pair anywhere


def _quota_events() -> list[dict]:
    return [
        _tev("1", "step_started", timestamp="2024-01-01T00:00:00Z",
             step="spec", kind="agent", phase="design"),
        _tev("2", "quota_reading", timestamp="2024-01-01T00:00:30Z",
             provider="claude", plan=None,
             windows=[{"label": "5h", "used_pct": 44.0, "resets_at": 100}],
             raw={}),
        _tev("3", "step_completed", timestamp="2024-01-01T00:01:00Z",
             step="spec", kind="agent", action={"verb": "continue"},
             var_mutations={}),
    ]


def test_summary_markdown_has_no_quota_input_and_carries_no_quota_block() -> None:
    header = SummaryHeader(workflow="imp", quest_id="gumu81",
                           status="merged", total_seconds=60.0)
    out = render_summary_markdown(build_summary(_quota_events()),
                                  header=header)
    assert "**quota**" not in out
    assert "claude" not in out
    assert "5h" not in out
    assert "| total | 1m 0s |" in out
    with pytest.raises(TypeError):
        render_summary_markdown([], header=header, quota=())
