# tests/test_render_markdown.py
from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

import pytest
from markdown_it import MarkdownIt

from imp.cli import narrative as nv
from imp.cli import render_markdown as rm
from imp.cli.accounting import RowUsage
from imp.cli.quota_column import QuotaColumn, QuotaFallback
from imp.cli.quota_share import QuotaFigure, QuotaWithheld, Units, Withheld
from imp.cli.render_html import _fold_markers, _FoldOpen, render_html
from imp.cli.render_markdown import (
    _accept,
    _details,
    _escape_all,
    _md_escape,
    _same_tokens,
    _subagent_status_label,
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
from imp.cli.render_text import LOG_CATEGORIES
from tests._event_fixtures import ev, interrupted_task_lead_events, task_lead_started

_ALL_CATS = frozenset(LOG_CATEGORIES)


def _ev(eid, etype, agent_id=None, **payload):
    return {"id": eid, "agent_id": agent_id, "type": etype, "payload": payload}


def _md(events, *, full=False, show=None):
    tree = nv.build_tree(events)
    return render_markdown(tree, full=full, show=show)


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


def test_lean_omits_fold_bodies_and_keeps_the_tool_recap() -> None:
    out = _md(_burst_events())
    assert "<details>" not in out        # lean omits content, it does not merely fold it
    assert "pondering deeply" not in out  # thinking category off
    assert "1 call · bash" in out         # the burst recap survives as a plain paragraph
    assert "command=ls" not in out        # no call bodies
    assert "file-a" not in out


def test_shown_categories_collapse_to_details_folds() -> None:
    out = _md(_burst_events(), show=_ALL_CATS)
    assert "<summary>thinking</summary>" in out
    assert "pondering deeply" in out
    assert "<summary>1 call · bash" in out   # the recap becomes the fold's summary
    assert "command=ls" in out               # call bodies inside the fold
    assert "file-a" in out


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


def test_prompt_rows_off_lean_folded_when_shown_inline_under_full() -> None:
    sys_node = nv.EngineMessageNode(text="<script>alert(1)</script>",
                                    subtype="system_prompt")
    ctx = nv.StepContextNode(payload={"provider": "anthropic", "model": "claude",
                                      "max_tool_calls": 50})
    root = nv.WorkflowNode(title="t")
    root.children = [sys_node, ctx]
    tree = nv.NarrativeTree(root=root)

    default = render_markdown(tree, full=False)
    assert "Internal context" not in default   # prompts off: omitted, not folded
    assert "system prompt" not in default

    shown = render_markdown(tree, full=False, show=frozenset({"prompts"}))
    assert "<summary>system prompt</summary>" in shown
    assert "<summary>Internal context — anthropic/claude</summary>" in shown
    assert "context ·" not in shown
    assert _in_code_fence(shown, "<script>")   # untrusted system_prompt fenced/inert

    full = render_markdown(tree, full=True)
    assert "**Internal context — anthropic/claude**" in full
    assert "provider: anthropic" in full
    assert "max_tool_calls: 50" in full
    assert _in_code_fence(full, "<script>")


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


def test_subagent_fold_collapses_under_details_when_shown() -> None:
    out = _md(_subagent_events(), show=frozenset({"subagents"}))
    assert "<summary>· reviewer" in out   # the lean status line becomes the summary
    assert "looks good" in out            # recursed body inside the fold
    assert "all clear" in out             # structured outcome inside the fold
    assert "</details>" in out


def test_shown_subagent_with_no_body_keeps_its_status_row() -> None:
    sub = _make_subagent(running=True)
    root = nv.WorkflowNode(title="t")
    root.children = [sub]
    out = render_markdown(nv.NarrativeTree(root=root), full=False,
                          show=frozenset({"subagents"}))
    assert "· reviewer (running)" in out
    assert "<details>" not in out


def _make_subagent(**kw):
    defaults = dict(subagent_name="reviewer", event_id="e", block_id="b",
                    nested=nv.NarrativeBuilder(), running=False)
    defaults.update(kw)
    return nv.SubagentFold(**defaults)


def test_subagent_status_tags_and_elapsed() -> None:
    assert "(running)" in _subagent_status_label(_make_subagent(running=True))
    assert "(stalled)" in _subagent_status_label(
        _make_subagent(terminal_state="stalled"))
    assert "(interrupted)" in _subagent_status_label(
        _make_subagent(terminal_state="interrupted"))
    assert "(context exhausted)" in _subagent_status_label(
        _make_subagent(terminal_state="context_exhausted"))
    assert "(failed)" in _subagent_status_label(_make_subagent(is_error=True))
    row = _subagent_status_label(_make_subagent(start_ts="2024-01-01T00:00:00Z",
                                                end_ts="2024-01-01T00:00:05Z"))
    assert "reviewer" in row and "5" in row   # elapsed suffix present


def test_subagent_status_label_is_raw_and_unprefixed() -> None:
    assert _subagent_status_label(_make_subagent(subagent_name="<x>")) == "<x>"


def test_escape_all_strips_sgr_caret_escapes_and_html_escapes() -> None:
    assert _escape_all("\x1b[1;33m<b>&\x00") == "&lt;b&gt;&amp;^@"


def test_details_escapes_its_raw_summary_per_context() -> None:
    def tag(s: str) -> str:
        return f"[{s}]"
    assert _details("`<x>` & y", "body", full=False, esc=tag) == (
        "<details>\n<summary>`&lt;x&gt;` &amp; y</summary>\n\nbody\n\n</details>")
    assert _details("`<x>`", "body", full=True, esc=tag) == "**[`<x>`]**\n\nbody"
    assert _details("`<x>`", "", full=True, esc=tag) == "**[`<x>`]**"


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
    assert "**tiers resolved**" in out                 # TiersResolvedNode (full)
    assert "- high → anthropic/claude" in out          # tiers list item
    assert "**2 warnings this run**" in out            # WarningsSummaryNode
    assert "disk low" in out and "(×2)" in out
    assert "**✓ Completed gumu81.**" in out            # CompletedNode
    assert "**✓ Merged gumu81 into main.**" in out     # MergedNode
    assert "odd_event k=v" in out                      # FallbackLine


_SHA_A = "4091ab6d1f2e3d4c5b6a7988990011223344ffee"
_SHA_B = "832c04d7aabbccddeeff00112233445566778899"


def _footer_md(node) -> str:
    root = nv.WorkflowNode(title="demo")
    root.children = [node]
    return render_markdown(nv.NarrativeTree(root=root), full=False)


def test_merged_footer_gains_an_unquoted_commit_list() -> None:
    out = _footer_md(nv.MergedNode(
        quest_id="gumu81", base="main",
        commits=[{"sha": _SHA_A, "subject": "cli: derive the quest's commits"},
                 {"sha": _SHA_B, "subject": "cli: print the commit list"}]))
    # one block: no blank line between the bold footer and its list
    assert ("**✓ Merged gumu81 into main.**\n"
            "- `4091ab6d` cli: derive the quest's commits\n"
            "- `832c04d7` cli: print the commit list") in out
    assert "> - " not in out       # a bold footer takes a bare list, not a callout


def test_completed_footer_gains_the_same_list() -> None:
    out = _footer_md(nv.CompletedNode(quest_id="gumu81",
                                      commits=[{"sha": _SHA_A, "subject": "only"}]))
    assert "**✓ Completed gumu81.**\n- `4091ab6d` only" in out


def test_commit_subject_is_md_escaped_in_the_list() -> None:
    out = _footer_md(nv.MergedNode(
        quest_id="gumu81", base="main",
        commits=[{"sha": _SHA_A, "subject": "fix <script> & \x1b[2Jwipe"}]))
    assert "- `4091ab6d` fix &lt;script&gt; &amp; ^[[2Jwipe" in out
    assert "<script>" not in out
    assert "\x1b" not in out


def test_footers_with_no_commits_stay_the_bare_bold_line() -> None:
    merged = _footer_md(nv.MergedNode(quest_id="gumu81", base="main"))
    assert merged.strip().endswith("**✓ Merged gumu81 into main.**")
    assert "- `" not in merged
    completed = _footer_md(nv.CompletedNode(quest_id="gumu81", commits=[]))
    assert completed.strip().endswith("**✓ Completed gumu81.**")
    assert "- `" not in completed


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


def test_tiers_row_off_lean_folded_when_shown_bold_under_full() -> None:
    root = nv.WorkflowNode(title="t")
    root.children = [nv.TiersResolvedNode(rows=[{"tier": "high",
                                                 "provider": "anthropic",
                                                 "model": "claude",
                                                 "differ_tier": None,
                                                 "differ_from": None}])]
    tree = nv.NarrativeTree(root=root)
    assert "tiers resolved" not in render_markdown(tree, full=False)
    shown = render_markdown(tree, full=False, show=frozenset({"tiers"}))
    assert "<summary>tiers resolved</summary>" in shown
    assert "- high → anthropic/claude" in shown
    assert "**tiers resolved**" in render_markdown(tree, full=True)


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


def test_summary_markdown_total_subtracts_the_exclusion_union() -> None:
    rows = [StepRow(phase="design", step="spec",
                    start_ts="2024-01-01T00:00:00Z",
                    end_ts="2024-01-01T00:01:00Z")]
    header = SummaryHeader(
        workflow="imp", quest_id="c", status="merged", total_seconds=480.0,
        exclusions=nv.Exclusions((("2024-01-01T00:01:30Z",
                                   "2024-01-01T00:06:30Z"),)))
    out = render_summary_markdown(rows, header=header)
    assert "| total | 3m 0s | — | — | — | — | — |" in out   # 8m wall - 5m parked


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


def test_lean_omits_engine_bookkeeping_lines() -> None:
    root = nv.WorkflowNode(title="t")
    root.children = [nv.FallbackLine(event_type="token_refresh",
                                     payload={"outcome": "skipped_fresh"})]
    tree = nv.NarrativeTree(root=root)
    assert "token_refresh" not in render_markdown(tree, full=False)
    assert "token_refresh" not in render_markdown(tree, full=False,
                                                  show=_ALL_CATS - {"tiers"})
    assert "token_refresh outcome=skipped_fresh" in render_markdown(tree, full=True)


def test_quiet_first_retry_needs_every_category() -> None:
    events = [_ev("r1", "agent_retry", category="auth", attempt=1,
                  backoff_seconds=1.0, message="401 revoked")]
    assert "retry 1" not in _md(events, full=True, show=_ALL_CATS - {"thinking"})
    assert "retry 1" in _md(events, full=True, show=_ALL_CATS)


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
    hdr = SummaryHeader(workflow="imp", quest_id="c", status="completed",
                        total_seconds=300.0)
    out = render_summary_markdown(rows, header=hdr)
    assert "| task-lead | interrupted | — | — | — | — | — |" in out
    assert "| ↳ coder | interrupted | — | — | — | — | — |" in out
    assert "| task-lead | completed |" not in out


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


_FOOTNOTE_MD = ("\\* estimated to ±1 point of the weekly gauge; usage outside"
                " imp during the run skews it high")


def _quota_rows() -> list[StepRow]:
    """A subscription step with one subagent, plus a step that did no work."""
    sub = SubagentRow(name="coder", start_ts="2026-01-01T00:00:10+00:00",
                      end_ts="2026-01-01T00:00:40+00:00",
                      usage=RowUsage(fresh_input=200_000, output=3_000,
                                     dollars=1.0, priced=1, notional=1),
                      units=Units.of("claude", 1.0))
    return [
        StepRow(phase="plan", step="plan-writer",
                start_ts="2026-01-01T00:00:00+00:00",
                end_ts="2026-01-01T00:01:30+00:00",
                usage=RowUsage(fresh_input=1_000_000, output=20_000,
                               dollars=3.0, priced=1, notional=1),
                units=Units.of("claude", 2.0), subagents=[sub]),
        StepRow(phase="plan", step="plan-finalized",
                start_ts="2026-01-01T00:01:30+00:00",
                end_ts="2026-01-01T00:01:30+00:00"),
    ]


def test_summary_markdown_swaps_the_cost_column_for_the_weekly_share() -> None:
    # AC 19(a): 9 points of movement over a 10-unit window, so a row's share is
    # 0.9x its own units — the step's subtree drew 3 of them, its subagent 1.
    header = SummaryHeader(workflow="imp", quest_id="abc", status="merged",
                           total_seconds=2342.0)
    column = QuotaColumn((QuotaFigure("claude", 9.0, 3.0, 10.0),))
    out = render_summary_markdown(_quota_rows(), header=header, quota=column)
    assert ("| Step | Duration | Fresh | Cache read | Cache write | Out"
            " | Weekly* |") in out
    assert "| plan-writer | 1m 30s | 1.2M | — | — | 23.0k | 2.70% |" in out
    assert "| ↳ coder | 30s | 200.0k | — | — | 3.0k | 0.90% |" in out
    assert "| plan-finalized | 0s | — | — | — | — | — |" in out
    assert "| total | 39m 2s | 1.2M | — | — | 23.0k | 2.70% |" in out
    assert out.rstrip("\n").endswith(_FOOTNOTE_MD)   # AC 3, escaped sigil
    assert "Cost" not in out                          # AC 13: one money column
    assert "$" not in out


def test_summary_markdown_falls_back_to_cost_with_the_withheld_legend() -> None:
    # AC 19(a) over AC 6(b): no gauge resolved, so today's Cost column stays and
    # one extra line names why — under, not instead of, the dollar legend.
    header = SummaryHeader(workflow="imp", quest_id="abc", status="merged",
                           total_seconds=2342.0)
    still = ("quota below measurement resolution; the weekly gauge did not move"
             " during this run")
    out = render_summary_markdown(_quota_rows(), header=header,
                                  quota=QuotaFallback((still,)))
    assert ("| Step | Duration | Fresh | Cache read | Cache write | Out"
            " | Cost |") in out
    assert "| total | 39m 2s | 1.2M | — | — | 23.0k | $4.00* |" in out
    star = ("\\* theoretical API list price; actual subscription usage is much"
            " lower")
    assert star in out
    assert out.index(star) < out.index(still)
    assert "Weekly" not in out
    assert "%" not in out


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


def test_summary_markdown_without_a_quota_argument_is_unchanged() -> None:
    header = SummaryHeader(workflow="imp", quest_id="gumu81",
                           status="merged", total_seconds=60.0)
    rows = build_summary(_quota_events())
    out = render_summary_markdown(rows, header=header)
    assert out == render_summary_markdown(rows, header=header, quota=None)
    assert "**quota**" not in out       # no gauge block: saji99 dropped it
    assert "claude" not in out          # a quota_reading leaks no provider
    assert "5h" not in out              # nor a window label
    assert "| total | 1m 0s |" in out
    assert "Weekly" not in out


def test_summary_markdown_breaks_a_mixed_run_down_by_provider() -> None:
    # AC 14(a)/(e) on the export: claude's gauge moved 4 points over a 4-unit
    # window (its own 2 units => 2.00%); codex's did not move at all, so it has
    # no figure and says so in its own legend line.
    rows = [
        StepRow(phase="plan", step="plan-writer",
                start_ts="2026-01-01T00:00:00+00:00",
                end_ts="2026-01-01T00:01:30+00:00",
                usage=RowUsage(fresh_input=1_000_000, output=20_000,
                               dollars=3.0, priced=1, notional=1),
                units=Units.of("claude", 2.0)),
        StepRow(phase="plan", step="coder",
                start_ts="2026-01-01T00:01:30+00:00",
                end_ts="2026-01-01T00:02:30+00:00",
                usage=RowUsage(fresh_input=200_000, output=3_000, dollars=1.0,
                               priced=1, notional=1),
                units=Units.of("codex", 1.0)),
    ]
    header = SummaryHeader(workflow="imp", quest_id="abc", status="merged",
                           total_seconds=2342.0)
    column = QuotaColumn((QuotaFigure("claude", 4.0, 2.0, 4.0),
                          QuotaWithheld("codex", Withheld.BELOW_RESOLUTION)))
    out = render_summary_markdown(rows, header=header, quota=column)
    assert "| plan-writer | 1m 30s | 1.0M | — | — | 20.0k | 2.00% |" in out
    assert "| coder | 1m 0s | 200.0k | — | — | 3.0k | — |" in out
    assert "| total | 39m 2s | 1.2M | — | — | 23.0k | — |" in out
    # The share must sit in the quota column, not pad off the end of a row one
    # cell short: neither row here has a subagent, so every `| ↳ ` line is a
    # breakdown row.
    lines = out.splitlines()
    width = next(ln for ln in lines if ln.startswith("| Step |")).count("|")
    breakdown = [ln for ln in lines if ln.startswith("| ↳ ")]
    assert [ln.count("|") for ln in breakdown] == [width, width]
    assert [ln.split("|")[1].strip() for ln in breakdown] == ["↳ claude",
                                                             "↳ codex"]
    assert [ln.split("|")[-2].strip() for ln in breakdown] == ["2.00%", "—"]
    assert out.index("| total |") < out.index("| ↳ claude |")
    assert ("codex: quota below measurement resolution; the weekly gauge did"
            " not move during this run") in out


def test_accept_when_c_differs_only_inside_code() -> None:
    assert _accept("Use `a -> b`", "Use `a -&gt; b`")
    assert _accept("```\nif a < b:\n```", "```\nif a &lt; b:\n```")
    assert _accept("| `a -> b` | x |\n|---|---|\n| 1 | 2 |",
                   "| `a -&gt; b` | x |\n|---|---|\n| 1 | 2 |")


def test_same_tokens_rejects_added_raw_html() -> None:
    assert not _same_tokens("<b>x</b>", "&lt;b&gt;x&lt;/b&gt;")


def test_same_tokens_rejects_a_changed_summary_line() -> None:
    fold = "<details>\n<summary>{}</summary>\n\nbody\n\n</details>"
    assert not _same_tokens(fold.format("`<x>`"), fold.format("`&lt;x&gt;`"))


def test_same_tokens_rejects_a_cdata_section() -> None:
    assert not _same_tokens(
        "[a](b`c) <![CDATA[><img src=x onerror=alert(1)>]]> `d`",
        "[a](b`c) &lt;![CDATA[&gt;&lt;img src=x onerror=alert(1)&gt;]]&gt; `d`")


def test_same_tokens_rejects_a_changed_link_destination() -> None:
    assert not _same_tokens("[a](x&lt;y)", "[a](x&amp;lt;y)")


def test_fold_markers_catch_a_forged_fold_inside_a_code_span() -> None:
    c = "x `\n<details>\n<summary>s</summary>\n`"
    e = "x `\n&lt;details&gt;\n&lt;summary&gt;s&lt;/summary&gt;\n`"
    assert _fold_markers(c) == [_FoldOpen(1, "s")]
    assert _fold_markers(e) == []
    assert not _accept(c, e)


def _literal_tree(text: str) -> nv.NarrativeTree:
    root = nv.WorkflowNode(title="t")
    root.children = [nv.LiteralTextNode(text=text)]
    return nv.NarrativeTree(root=root)


def test_render_markdown_falls_back_to_full_escape_on_structural_change(
        monkeypatch) -> None:
    monkeypatch.setattr(rm, "_md_escape", lambda s: s)
    out = render_markdown(_literal_tree("<b>x</b>"), full=False)
    assert out == "# t\n\n&lt;b&gt;x&lt;/b&gt;\n"


def test_render_markdown_keeps_pass_c_when_structure_matches(monkeypatch) -> None:
    monkeypatch.setattr(rm, "_md_escape", lambda s: s)
    out = render_markdown(_literal_tree("Use `a -> b`"), full=False)
    assert out == "# t\n\nUse `a -> b`\n"


def test_summary_markdown_guard_picks_pass_c_or_full_escape(monkeypatch) -> None:
    monkeypatch.setattr(rm, "_md_escape", lambda s: s)
    def header(workflow: str) -> SummaryHeader:
        return SummaryHeader(workflow=workflow, quest_id="q", status="running",
                             total_seconds=1.0)
    kept = render_summary_markdown([], header=header("`a -> b`"))
    assert kept.startswith("# `a -> b` — q\n")
    fallback = render_summary_markdown([], header=header("<b>x</b>"))
    assert fallback.startswith("# &lt;b&gt;x&lt;/b&gt; — q\n")


_SAFE_FOLD_HTML = re.compile(r"<details>\n<summary>[^<>]*</summary>\n|</details>\n")


def _raw_html(md: str) -> list[str]:
    tokens = MarkdownIt("commonmark", {"html": True}).enable("table").parse(md)
    out: list[str] = []
    while tokens:
        tok = tokens.pop()
        if tok.type in ("html_inline", "html_block"):
            out.append(tok.content)
        tokens.extend(tok.children or [])
    return out


def _assert_safe(md: str, *, folds: int = 0) -> None:
    assert all(_SAFE_FOLD_HTML.fullmatch(h) for h in _raw_html(md))
    page = render_html(md)
    assert "<script" not in page and "<img" not in page
    assert page.count("<details>") == folds


def _full_escape(render: Callable[[], str]) -> str:
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(rm, "_md_escape", _escape_all)
        return render()


def _agent_events(*texts: str) -> list[dict]:
    return [_ev("2", "step_started", step="s", kind="agent", phase="p",
                model="m", provider="anthropic"),
            *(_ev(f"a{i}", "agent_message", agent_id="step:p/s", provider="anthropic",
                  model="m", stop_reason="end_turn", usage={},
                  raw={"content": [{"type": "text", "text": t}]})
              for i, t in enumerate(texts))]


def _node_tree(*nodes: Any) -> nv.NarrativeTree:
    root = nv.WorkflowNode(title="t")
    root.children = list(nodes)
    return nv.NarrativeTree(root=root)


def test_md_escape_passes_code_spans_verbatim() -> None:
    assert _md_escape("Use `a -> b` & <x>") == "Use `a -> b` &amp; &lt;x&gt;"
    assert _md_escape("``a ` <b>`` <c>") == "``a ` <b>`` &lt;c&gt;"
    assert _md_escape("x `a\n<b>\nc` <d>") == "x `a\n<b>\nc` &lt;d&gt;"


def test_md_escape_escapes_unclosed_and_backslashed_backticks() -> None:
    assert _md_escape("a `` <b> ` c") == "a `` &lt;b&gt; ` c"
    assert _md_escape("\\`<b>` y") == "\\`&lt;b&gt;` y"
    assert _md_escape("\\``<b>`") == "\\``<b>`"


def test_md_escape_passes_code_blocks_verbatim() -> None:
    assert _md_escape("<a>\n```\n<b>\n```\n<c>") == "&lt;a&gt;\n```\n<b>\n```\n&lt;c&gt;"
    assert _md_escape("- x\n\n  ~~~\n  <b>\n  ~~~") == "- x\n\n  ~~~\n  <b>\n  ~~~"


def test_md_escape_fully_escapes_blockquote_regions() -> None:
    assert _md_escape("> ```\n> <b>\n> ```\n\n`<c>`") == (
        "&gt; ```\n&gt; &lt;b&gt;\n&gt; ```\n\n`<c>`")
    assert _md_escape("- i\n\n  > `<b>`") == "- i\n\n  &gt; `&lt;b&gt;`"


def test_md_escape_handles_tabs_lazy_continuation_and_quoted_list_fences() -> None:
    assert _md_escape("x\n\n\tif a < b:") == "x\n\n\tif a < b:"
    assert _md_escape("-\tx\n\n\t```\n\t<b>\n\t```") == "-\tx\n\n\t```\n\t<b>\n\t```"
    assert _md_escape("> `<a>`\nlazy `<b>` <c>\n\n`<d>`") == (
        "&gt; `&lt;a&gt;`\nlazy `&lt;b&gt;` &lt;c&gt;\n\n`<d>`")
    assert _md_escape("> - x\n>\n>   ```\n>   <b>\n>   ```\n\n`<c>`") == (
        "&gt; - x\n&gt;\n&gt;   ```\n&gt;   &lt;b&gt;\n&gt;   ```\n\n`<c>`")


def test_agent_code_span_is_escaped_exactly_once() -> None:
    md = _md(_agent_events("Use `_cmd_*(args) -> int`"))
    assert "Use `_cmd_*(args) -> int`" in md
    page = render_html(md)
    assert "<code>_cmd_*(args) -&gt; int</code>" in page
    assert "&amp;gt;" not in page and "&amp;lt;" not in page


@pytest.mark.parametrize("text", [
    "Before:\n```\nif a < b:\n```",
    "Before:\n~~~\nif a < b:\n~~~",
    "Before:\n\n    if a < b:",
    "<div>\n```\nif a < b:\n```",
])
def test_agent_code_block_is_escaped_exactly_once(text: str) -> None:
    md = _md(_agent_events(text))
    assert "if a < b:" in md
    page = render_html(md)
    assert "<pre><code>if a &lt; b:\n</code></pre>" in page
    assert "&amp;lt;" not in page


def test_agent_html_outside_code_still_escapes() -> None:
    md = _md(_agent_events("<script> <b>x</b> & `ok`"))
    assert "&lt;script&gt; &lt;b&gt;x&lt;/b&gt; &amp; `ok`" in md


_IMG = "<img src=x onerror=alert(1)>"


@pytest.mark.parametrize(("tree", "show", "folds"), [
    (nv.build_tree(_agent_events("x `a\n<details>\nb`")), None, 0),
    (nv.build_tree(_agent_events("[a](b`c) <script> `d`")), None, 0),
    (_node_tree(nv.ErrorNode(
        message="```\n<script>alert(1)</script>\n<details>\n```")), None, 0),
    (_node_tree(nv.MergedNode(quest_id="q", base="main", commits=[
        {"sha": _SHA_A, "subject": f"    {_IMG}"}])), None, 0),
    (nv.build_tree(_agent_events(
        "```\nfoo", f"```\n<details>\n<summary>{_IMG}</summary>\n```")), None, 0),
    (_node_tree(nv.TiersResolvedNode(rows=[
        {"tier": "`", "provider": f"`{_IMG}`", "model": "m",
         "differ_tier": None, "differ_from": None}])), frozenset({"tiers"}), 1),
    (nv.build_tree(_agent_events("```\nfoo", "```\n<details>\nx\n<details>")),
     None, 0),
    (nv.build_tree(_agent_events(
        "x `" + "\n<details>\n<summary>s</summary>" * 5000 + "\n`")), None, 0),
    (nv.build_tree(_agent_events(
        f"[a](b`c) <![CDATA[>{_IMG}]]> `d`")), None, 0),
], ids=["span-details", "link-backtick", "error-fence", "commit-indent",
        "cross-text-fence", "tier-spans", "stray-details", "deep-folds", "cdata"])
def test_hostile_document_falls_back_to_full_escape(
        tree: nv.NarrativeTree, show: frozenset[str] | None, folds: int) -> None:
    md = render_markdown(tree, full=False, show=show)
    assert md == _full_escape(lambda: render_markdown(tree, full=False, show=show))
    _assert_safe(md, folds=folds)


def test_summary_step_code_span_is_safe_and_verbatim() -> None:
    header = SummaryHeader(workflow="imp", quest_id="q", status="merged",
                           total_seconds=1.0)

    def rows(step: str) -> list[StepRow]:
        return [StepRow(phase="p", step=step, start_ts="2026-01-01T00:00:00+00:00",
                        end_ts="2026-01-01T00:00:01+00:00")]
    hostile = render_summary_markdown(rows(f"`{_IMG}`"), header=header)
    _assert_safe(hostile)
    kept = render_summary_markdown(rows("`a -> b`"), header=header)
    assert "| `a -> b` |" in kept


def _burst_named(name: str) -> list[dict]:
    return [_ev("2", "step_started", step="s", kind="agent", phase="p",
                model="m", provider="anthropic"),
            _ev("4", "tool_call", agent_id="step:p/s", id="b1", name=name,
                input={"command": "ls"}),
            _ev("5", "tool_result", agent_id="step:p/s", id="b1",
                content="ok", error=False)]


def test_burst_recap_summary_is_escaped_exactly_once() -> None:
    md = _md(_burst_named("`<x>`"), show=frozenset({"tools"}))
    for doc in (md, render_html(md)):
        assert "<summary>1 call · `&lt;x&gt;`</summary>" in doc
        assert "&amp;" not in doc


def test_hidden_tools_recap_keeps_its_code_span_verbatim() -> None:
    assert "1 call · `<x>`" in _md(_burst_named("`<x>`"))


def _subagent_named(name: str) -> list[dict]:
    events = _subagent_events()
    events[1]["payload"]["input"]["subagent_name"] = name
    return events


def test_full_subagent_label_is_escaped_exactly_once() -> None:
    md = _md(_subagent_named("`<x>`"), full=True)
    assert "**`<x>`**" in md
    page = render_html(md)
    assert "<strong><code>&lt;x&gt;</code></strong>" in page
    assert "&amp;lt;" not in page


def test_inline_subagent_status_keeps_its_code_span_verbatim() -> None:
    assert "· `a -> b` · m" in _md(_subagent_named("`a -> b`"))


def test_commit_subject_and_error_keep_code_spans_verbatim() -> None:
    md = render_markdown(_node_tree(
        nv.ErrorNode(message="`a -> b`"),
        nv.MergedNode(quest_id="q", base="main",
                      commits=[{"sha": _SHA_A, "subject": "`a -> b`"}])), full=False)
    assert "> **error:** `a -> b`" in md
    assert "- `4091ab6d` `a -> b`" in md


@pytest.mark.parametrize(("quoted", "escaped"), [
    ("> ```\n> if a < b:\n> ```", "&gt; ```\n&gt; if a &lt; b:\n&gt; ```"),
    ("- item\n\n  > ```\n  > if a < b:\n  > ```",
     "- item\n\n  &gt; ```\n  &gt; if a &lt; b:\n  &gt; ```"),
])
def test_blockquoted_code_does_not_force_a_fallback(quoted: str, escaped: str) -> None:
    md = _md(_agent_events(quoted, "Use `a -> b`"))
    assert escaped in md
    assert "Use `a -> b`" in md


def _headings(md: str) -> list[str]:
    return [ln for ln in md.splitlines() if ln.startswith("#")]


def test_md_headings_follow_each_header_in_order() -> None:
    assert _headings(_md(interrupted_task_lead_events())) == [
        "# t", "## implementation phase · plan 1/1",
        "### task 1/2", "### task 1/2 (resume)", "### task 2/2"]


def test_md_reopen_restart_heads_the_step_with_its_resume_seed() -> None:
    md = _md([
        ev("1", "quest_created", quest_id=1, title="t"),
        task_lead_started("2"),
        ev("3", "workflow_paused", reason="interrupt", interrupt_reason="ctrl-c"),
        ev("4", "workflow_resumed"),
        task_lead_started("5", label="task 2/2", resumed=True, restarted=True),
    ])
    assert "### task 2/2 (resume)" in _headings(md)
    assert "### task 1/2" not in _headings(md)


def test_md_new_node_resume_heads_the_step_with_resume() -> None:
    md = _md([
        ev("1", "quest_created", quest_id=1, title="t"),
        ev("2", "workflow_paused", reason="interrupt", interrupt_reason="ctrl-c"),
        ev("3", "workflow_resumed"),
        task_lead_started("4", label="task 2/2", resumed=True, restarted=False),
    ])
    assert "### task 2/2 (resume)" in _headings(md)


def test_md_verb_push_keeps_the_entry_heading() -> None:
    md = _md([
        ev("1", "quest_created", quest_id=1, title="t"),
        ev("2", "step_started", step="plan-writer", kind="agent", phase="plan",
           model="m", provider="anthropic", label="writing plan"),
        ev("3", "step_label_updated", agent_id="step:plan/plan-writer",
           label="addressing plan review feedback"),
    ])
    assert "### writing plan" in _headings(md)
    assert "### addressing plan review feedback" not in _headings(md)
