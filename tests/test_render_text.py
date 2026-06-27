# tests/test_render_text.py
from __future__ import annotations

import json
import re as _re
import unittest.mock
from pathlib import Path

from imp.cli import narrative as nv
from imp.cli import palette as pal
from imp.cli import render_text as rt
from imp.cli.render_text import render_tree


class _FakeRendered:
    def __init__(self, texts): self.texts = texts


def _lines(events, *, full=False, width=80):
    tree = nv.build_tree(events)
    return list(render_tree(tree, full=full, color=False, width=width))


def _ev(eid, etype, agent_id=None, **payload):
    return {"id": eid, "agent_id": agent_id, "type": etype, "payload": payload}


def test_phase_step_outcome_render_without_header() -> None:
    out = "\n".join(_lines([
        _ev("1", "change_created", title="add auth"),
        _ev("3", "step_started", step="spec", kind="agent", phase="design",
            model="m", provider="anthropic"),
        _ev("4", "step_completed", step="spec", kind="agent",
            action={"verb": "continue"}, var_mutations={}),
    ]))
    # No slug/title banner: the transcript opens directly on the first phase.
    assert "#add-auth-7" not in out
    assert "design" in out
    assert "spec" in out
    assert "spec · complete" in out          # merged breadcrumb (was: "continue" in out)


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


def test_collapsed_folds_drop_thinking_keep_burst_recap() -> None:
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
    # Thinking is still noise in the lean view — dropped entirely.
    assert "thinking" not in out
    # The tool burst now leaves ONE dark-gray recap line (point 8): distinct tool
    # names (first-seen order) + the call count — not the full per-call dump.
    assert "3 calls · bash · ls" in out
    # one recap line, not one per call
    assert out.count("3 calls · bash · ls") == 1
    assert "→ ok" not in out            # per-call results stay in --full only


def test_collapsed_subagent_renders_single_status_row() -> None:
    # Part D: lean collapses a subagent to ONE status row — no recursed prose,
    # no outcome, no SDK sentinel. (--full still expands; see test_full_mode_*.)
    out = _lines([
        _ev("2", "step_started", step="s", kind="agent", phase="p",
            model="m", provider="anthropic"),
        _ev("3", "step_context", agent_id="step:p/s", provider="anthropic",
            model="claude", system_prompt="S", tools_schema=[{"name": "a"}, {"name": "b"}],
            params={}, max_tool_calls=50),
        _ev("tk1", "tool_call", agent_id="step:p/s", id="b1", name="sub_agent",
            input={"subagent_name": "reviewer", "task_description": "go"}),
        _ev("su1", "llm_response", agent_id="sub:tk1", provider="anthropic", model="m",
            stop_reason="end_turn", usage={},
            raw={"content": [{"type": "text", "text": "looks good"}]}),
        _ev("r1", "tool_result", agent_id="step:p/s", id="b1",
            content='{"status":"Success","summary":"ok"}', error=False),
    ])
    joined = "\n".join(out)
    # The lean step-context line is gone (the model rides on the step header);
    # the context detail survives only in imp log --full.
    assert "▸ context" not in joined
    assert "s · m" in joined          # model on the step header
    # Subagent: single status row, complete. No bullet, no recursed prose, no outcome.
    assert any(ln.strip().startswith("reviewer") for ln in out)  # + model
    assert "· reviewer" not in joined   # the leading bullet is gone
    assert "looks good" not in joined
    assert "status=Success" not in joined
    # The old one-liner / events shape is gone.
    assert "▸ task → reviewer" not in joined
    assert "events" not in joined


def test_subagent_lean_suppresses_all_nested_io() -> None:
    # Part D: lean renders NO subagent I/O at all — not prose, not nested
    # thinking/tools, not the outcome. Just the one status row.
    out = "\n".join(_lines([
        _ev("2", "step_started", step="s", kind="agent", phase="p",
            model="m", provider="anthropic"),
        _ev("tk1", "tool_call", agent_id="step:p/s", id="b1", name="sub_agent",
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
    assert "reviewer" in out   # the single status row (no leading bullet)
    assert "· reviewer" not in out
    assert "nested prose" not in out       # subagent prose NOT recursed in lean
    assert "nested musings" not in out     # nested thinking suppressed
    assert "status=Success" not in out     # outcome NOT shown in lean


def test_subagent_outcome_suppressed_in_lean() -> None:
    # Part D supersedes the old "decision ii" (outcome shown in lean): lean now
    # shows neither the prose nor the outcome, only the status row.
    out = "\n".join(_lines([
        _ev("2", "step_started", step="s", kind="agent", phase="p",
            model="m", provider="anthropic"),
        _ev("tk1", "tool_call", agent_id="step:p/s", id="b1", name="sub_agent",
            input={"subagent_name": "reviewer", "task_description": "go"}),
        _ev("su1", "llm_response", agent_id="sub:tk1", provider="anthropic", model="m",
            stop_reason="end_turn", usage={},
            raw={"content": [{"type": "text", "text": "the prose"}]}),
        _ev("r1", "tool_result", agent_id="step:p/s", id="b1",
            content='{"status":"Success","summary":"done"}', error=False),
    ]))
    assert "reviewer" in out
    assert "· reviewer" not in out
    assert "the prose" not in out
    assert "status=Success" not in out


# NOTE: there is intentionally NO doubly-nested (sub-subagent) test here.
# Routing is single-level by exact membership (narrative.py:763-770): a
# grandchild's `sub:<id>` events are NOT delegated into the child fold's nested
# builder, and spawned subagents receive `subagent_defs={}` so they cannot spawn
# subagents at all. A sub-subagent fixture would misroute (inner prose lands at
# the step level, inner fold stays empty) and any substring-presence assertion
# would pass for the wrong reason. The render code recurses correctly, but the
# scenario is unreachable until the engine enables nesting and routing becomes
# recursive/prefix-aware — out of scope for this plan.


def test_subagent_error_renders_failed_status_row() -> None:
    # Part D: a failed subagent surfaces as (failed) in lean — NOT the raw
    # error outcome prose (which could leak the SDK sentinel).
    out = list(_lines([
        _ev("2", "step_started", step="s", kind="agent", phase="p",
            model="m", provider="anthropic"),
        _ev("tk1", "tool_call", agent_id="step:p/s", id="b1", name="sub_agent",
            input={"subagent_name": "reviewer", "task_description": "go"}),
        _ev("r1", "tool_result", agent_id="step:p/s", id="b1",
            content="dispatch failed: boom", error=True),
    ], width=80))
    assert any("reviewer (failed)" in ln for ln in out)
    assert not any("· reviewer" in ln for ln in out)              # no leading bullet
    assert not any("dispatch failed: boom" in ln for ln in out)   # outcome suppressed
    # The status row carries the subagent (magenta) color when color is on.
    color_out = list(render_tree(nv.build_tree([
        _ev("2", "step_started", step="s", kind="agent", phase="p",
            model="m", provider="anthropic"),
        _ev("tk1", "tool_call", agent_id="step:p/s", id="b1", name="sub_agent",
            input={"subagent_name": "reviewer", "task_description": "go"}),
        _ev("r1", "tool_result", agent_id="step:p/s", id="b1",
            content="dispatch failed: boom", error=True),
    ]), full=False, color=True, width=80))
    assert any("(failed)" in ln and "\x1b[35m" in ln for ln in color_out)


def test_full_mode_also_emits_subagent_outcome() -> None:
    # --full must stay a superset of default: it expands thinking/tools AND emits
    # the structured return value (maintainer decision; deviation from the spec's
    # original "--full unchanged").
    out = "\n".join(_lines([
        _ev("2", "step_started", step="s", kind="agent", phase="p",
            model="m", provider="anthropic"),
        _ev("tk1", "tool_call", agent_id="step:p/s", id="b1", name="sub_agent",
            input={"subagent_name": "reviewer", "task_description": "go"}),
        _ev("su1", "llm_response", agent_id="sub:tk1", provider="anthropic", model="m",
            stop_reason="end_turn", usage={},
            raw={"content": [{"type": "text", "text": "verdict: approve"}]}),
        _ev("r1", "tool_result", agent_id="step:p/s", id="b1",
            content='{"status":"Success","summary":"ok"}', error=False),
    ], full=True))
    assert "▾ reviewer" in out      # expanded fold header (full shape)
    assert "verdict: approve" in out        # recursed prose
    assert "→ status=Success · summary=ok" in out   # outcome now present in full too


def test_full_mode_expands_tools_thinking_and_subagent() -> None:
    out = "\n".join(_lines([
        _ev("2", "step_started", step="s", kind="agent", phase="p",
            model="m", provider="anthropic"),
        _ev("3", "thinking", agent_id="step:p/s", content="deep thoughts here"),
        _ev("4", "tool_call", agent_id="step:p/s", id="b1", name="bash",
            input={"command": "ls -la"}),
        _ev("5", "tool_result", agent_id="step:p/s", id="b1", content="total 0", error=False),
        _ev("tk1", "tool_call", agent_id="step:p/s", id="b2", name="sub_agent",
            input={"subagent_name": "reviewer", "task_description": "go"}),
        _ev("su1", "llm_response", agent_id="sub:tk1", provider="anthropic", model="m",
            stop_reason="end_turn", usage={},
            raw={"content": [{"type": "text", "text": "subagent verdict: approve"}]}),
        _ev("r1", "tool_result", agent_id="step:p/s", id="b2",
            content='{"status":"Success"}', error=False),
    ], full=True))
    assert "▾ thinking" in out and "deep thoughts here" in out
    assert "▾ tools" in out
    assert "· bash" in out and "command=ls -la" in out and "total 0" in out
    assert "▾ reviewer" in out
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


def test_full_mode_shows_the_replay_source_system_prompt() -> None:
    # The system prompt rendered by --full is the SAME step_context.system_prompt
    # that Task 11 replays on resume — so the displayed prompt is exactly what the
    # model saw on every turn, not a first-entry snapshot that drifts.
    stored = "ASSEMBLED SYSTEM PROMPT v1"
    out = "\n".join(_lines([
        _ev("2", "step_started", step="s", kind="agent", phase="p",
            model="m", provider="anthropic"),
        _ev("3", "step_context", agent_id="step:p/s", provider="anthropic",
            model="claude", system_prompt=stored, tools_schema=[{"name": "bash"}],
            params={"max_tokens": 8192}, max_tool_calls=50),
    ], full=True))
    assert stored in out


def test_step_context_dropped_from_lean_model_on_step_header() -> None:
    # Lean view: no `▸ context …` line at all — the model rides on the step
    # header (point 1); the system prompt / tool list survive in --full only.
    events = [
        _ev("2", "step_started", step="s", kind="agent", phase="p",
            model="claude-opus-4-7", provider="anthropic"),
        _ev("3", "step_context", agent_id="step:p/s", provider="anthropic",
            model="claude-opus-4-7", system_prompt="SYSTEM PROMPT TEXT",
            tools_schema=[], params={}, max_tool_calls=50),
    ]
    lean = "\n".join(_lines(events, full=False))
    assert "▸ context" not in lean
    assert "SYSTEM PROMPT TEXT" not in lean
    assert "s · claude-opus-4-7" in lean      # model on the step header
    full = "\n".join(_lines(events, full=True))
    assert "context" in full and "SYSTEM PROMPT TEXT" in full


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


def test_synthetic_user_message_dropped_genuine_shown() -> None:
    # point 3: only a human reply the engine awaited renders as a gutter turn; the
    # synthetic "begin" kickoff (no preceding awaiting_user_input) is dropped.
    out = "\n".join(_lines([
        _ev("1", "step_started", step="s", kind="agent", phase="p", model="m"),
        _ev("2", "user_message", agent_id="step:p/s",
            content=[{"type": "text", "text": "begin"}]),
        _ev("3", "awaiting_user_input", agent_id="step:p/s"),
        _ev("4", "user_message", agent_id="step:p/s",
            content=[{"type": "text", "text": "do the real thing"}]),
    ]))
    assert "begin" not in out
    assert "> do the real thing" in out   # caret-marked human turn at column 0


def test_interjected_user_message_renders_as_human_turn() -> None:
    # A human INTERJECT is delivered unprompted while the agent runs: the engine
    # emits user_message (the typed text) IMMEDIATELY followed by user_interjected
    # — with NO preceding awaiting_user_input. Without consuming user_interjected
    # the message would be tagged interactive=False and dropped as a synthetic
    # engine prompt. It must instead render as a genuine human turn (gutter).
    events = [
        _ev("1", "step_started", step="s", kind="agent", phase="p", model="m"),
        _ev("2", "llm_response", agent_id="step:p/s", provider="anthropic",
            model="m", stop_reason="end_turn", usage={},
            raw={"content": [{"type": "text", "text": "working on it"}]}),
        _ev("3", "user_message", agent_id="step:p/s",
            content=[{"type": "text", "text": "actually use postgres"}]),
        _ev("4", "user_interjected", agent_id="step:p/s",
            text="actually use postgres"),
    ]
    tree = nv.build_tree(events)
    out = "\n".join(render_tree(tree, full=False, color=False, width=80))
    assert "> actually use postgres" in out   # caret-marked human turn at column 0
    step = tree.root.children[0].children[0]
    assert isinstance(step, nv.StepNode)
    um = next(c for c in step.children if isinstance(c, nv.UserMessageNode))
    assert um.interactive is True


def test_step_interactive_sourced_from_awaiting_event_not_step_started() -> None:
    # Task 8: a step is "interactive" (it solicited the human) ONLY because it
    # emitted an awaiting_user_input event — NOT because step_started carried an
    # `interactive` key. A step whose stream has no awaiting_user_input is not
    # interactive even if step_started carried a stray interactive=True (the key
    # is gone in Task 7; the narrative ignores any leftover).
    solicited = nv.build_tree([
        _ev("1", "step_started", step="s", kind="agent", phase="p", model="m"),
        _ev("2", "awaiting_user_input", agent_id="step:p/s",
            prompt="Merge?"),
        _ev("3", "user_message", agent_id="step:p/s",
            content=[{"type": "text", "text": "yes"}]),
    ])
    step = solicited.root.children[0].children[0]
    assert isinstance(step, nv.StepNode)
    assert step.interactive is True

    not_solicited = nv.build_tree([
        # step_started carries a stray interactive=True payload key, but with no
        # awaiting_user_input event the step must NOT be interactive.
        _ev("1", "step_started", step="s", kind="agent", phase="p", model="m",
            interactive=True),
        _ev("2", "user_message", agent_id="step:p/s",
            content=[{"type": "text", "text": "begin"}]),
    ])
    step2 = not_solicited.root.children[0].children[0]
    assert isinstance(step2, nv.StepNode)
    assert step2.interactive is False



def test_resumed_step_reemit_awaiting_user_input_shows_caret() -> None:
    # L3: an attended resume re-emits awaiting_user_input with the stored prompt;
    # the reply that follows must still render with the human-reply caret.
    events = [
        _ev("1", "step_started", step="s", kind="agent", phase="p", model="m"),
        _ev("2", "awaiting_user_input", agent_id="step:p/s", prompt="Continue?"),
        _ev("3", "workflow_paused", reason="ctrl-c"),
        _ev("4", "workflow_resumed"),
        # resume re-enters the step and re-emits awaiting_user_input + the reply
        _ev("5", "step_started", step="s", kind="agent", phase="p", model="m"),
        _ev("6", "awaiting_user_input", agent_id="step:p/s", prompt="Continue?"),
        _ev("7", "user_message", agent_id="step:p/s",
            content=[{"type": "text", "text": "go ahead"}]),
    ]
    tree = nv.build_tree(events)
    out = "\n".join(render_tree(tree, full=False, color=False, width=80))
    assert "> go ahead" in out   # caret-marked human turn at column 0
    # The re-emitted awaiting_user_input uniquely sets interactive=True on the
    # resumed step (resume re-entry resets it to False first, so only the
    # re-emitted event could have set it back). Assert directly on the node.
    step = tree.root.children[0].children[0]
    assert isinstance(step, nv.StepNode)
    assert step.interactive is True


def test_chrome_step_collapses_to_single_breadcrumb() -> None:
    # §4: a chrome step (script/branch/merge gate) now prints a start header AND
    # a `complete` breadcrumb, just like any agent step.
    out = "\n".join(_lines([
        _ev("1", "change_created", title="t"),
        _ev("2", "step_started", step="spec-verify-commit", kind="script", phase="spec"),
        _ev("3", "step_completed", step="spec-verify-commit", kind="script",
            action={"verb": "continue"}),
    ]))
    assert "spec-verify-commit" in out
    assert "spec-verify-commit · complete" in out   # show_complete now unconditional, dotted


def test_open_chrome_step_shows_header() -> None:
    # §4: an OPEN chrome step (step_started seen, no step_completed yet) renders
    # its header line — just like any other open step. The old `name · running`
    # line is gone; the header is the step name, carrying the step color.
    out = _lines([
        _ev("1", "change_created", title="t"),
        _ev("2", "step_started", step="run-tests", kind="script",
            phase="implement"),
    ])
    hits = [ln for ln in out if "run-tests" in ln]
    assert len(hits) == 1
    assert "running" not in hits[0]
    # The header carries the STEP color (not breadcrumb gray).
    colored = list(render_tree(nv.build_tree([
        _ev("1", "change_created", title="t"),
        _ev("2", "step_started", step="run-tests", kind="script",
            phase="implement"),
    ]), full=False, color=True, width=80))
    header_ln = next(ln for ln in colored if "run-tests" in ln)
    assert pal._ANSI["step"] in header_ln
    # Once the gate completes, both header and `complete` breadcrumb appear.
    out2 = _lines([
        _ev("1", "change_created", title="t"),
        _ev("2", "step_started", step="run-tests", kind="script",
            phase="implement"),
        _ev("3", "step_completed", step="run-tests", kind="script",
            action={"verb": "continue"}),
    ])
    hits2 = [ln for ln in out2 if "run-tests" in ln]
    assert len(hits2) == 2                       # header + breadcrumb
    assert any("complete" in ln for ln in hits2)


def test_breadcrumb_and_fallback_recede_to_gray() -> None:
    # the per-step completion line now recedes to chrome gray; stray event
    # (fallback) lines stay dark-gray chrome too.
    crumb = next(iter(rt._render(
        nv.BreadcrumbLine(step_name="x", elapsed_s=3,
                          action={"verb": "goto", "target": "y"}),
        depth=0, full=False, color=True, width=80)))
    assert pal._ANSI["gray"] in crumb
    assert pal._ANSI["step"] not in crumb       # no longer bold step-blue
    fallback = next(iter(rt._render(
        nv.FallbackLine(event_type="change_merged", payload={"id": "s"}),
        depth=0, full=False, color=True, width=80)))
    assert pal._ANSI["gray"] in fallback


def test_header_line_matches_render_for_phase_and_step() -> None:
    phase = nv.PhaseNode(name="design")
    step = nv.StepNode(name="spec", kind="agent", interactive=True)
    branch = nv.StepNode(name="route", kind="branch")
    assert rt.header_line(phase, depth=0, color=False) == "── design phase ───"
    assert rt.header_line(step, depth=1, color=False) == "  spec"
    # Every kind renders as the bare step name (no special-case label).
    assert rt.header_line(branch, depth=2, color=False) == "    route"


def test_agent_text_is_plain_when_color_enabled() -> None:
    node = nv.AgentTextNode(payload={})
    node._cache = _FakeRendered(["hello world"])
    out = list(rt._render(node, depth=0, full=False, color=True, width=80))
    # calm baseline: the prose text carries no ANSI; only the turn bullet is bold.
    assert out == ["\x1b[1m• \x1b[0mhello world"]


def test_empty_thinking_is_suppressed_both_modes() -> None:
    for content in ("", "   ", "\n\t "):
        node = nv.ThinkingFold(content=content)
        assert list(rt._render(node, depth=1, full=False, color=False, width=80)) == []
        assert list(rt._render(node, depth=1, full=True, color=False, width=80)) == []


def test_nonempty_thinking_dropped_in_default_mode() -> None:
    node = nv.ThinkingFold(content="abcde")
    assert list(rt._render(node, depth=1, full=False, color=False, width=80)) == []


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
                "ErrorNode", "StepContextNode", "OutcomeLine",
                "FallbackLine", "BreadcrumbLine", "TiersResolvedNode"}
    assert required <= seen, f"fixture missing node types: {required - seen}"


def test_literal_text_node_renders_as_agent_prose() -> None:
    from imp.cli import narrative as nv
    from imp.cli import render_text as rt
    node = nv.LiteralTextNode(text="line one\nline two")
    out = list(rt._render(node, depth=1, full=False, color=False, width=80))
    assert any("line one" in ln for ln in out)
    assert any("line two" in ln for ln in out)


def test_literal_color_role_renders_plain_yellow_no_bullet() -> None:
    roled = list(rt._render(nv.LiteralTextNode(text="hi", color_role="lifecycle"),
                            depth=0, full=False, color=True, width=80))
    assert roled == [pal.ansi("hi", "yellow")]
    # No color role → unchanged: the bold "• " agent-prose bullet, via markdown.
    plain = list(rt._render(nv.LiteralTextNode(text="hi"),
                            depth=0, full=False, color=True, width=80))
    assert plain == ["\x1b[1m• \x1b[0mhi"]


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


def test_step_header_separated_from_first_prose_by_one_blank() -> None:
    # design (point 2): exactly ONE blank line between the step header and the
    # agent's first words — the label and the speech get air, but not a double gap.
    out = _lines([
        _ev("1", "step_started", step="spec", kind="agent", phase="design"),
        _ev("2", "llm_response", agent_id="a",
            raw={"content": [{"type": "text", "text": "Only prose."}]}, usage={}),
    ])
    idx = next(i for i, ln in enumerate(out) if "Only prose." in ln)
    assert out[idx - 1].strip() == "", "one blank separates the header from prose"
    assert out[idx - 2].strip() != "", "the step header sits just above that blank"


def test_tiers_resolved_attaches_at_root() -> None:
    # Fired before any step_started (agent_id=None) → lands at the workflow root.
    b = nv.NarrativeBuilder()
    b.add({"id": "e1", "agent_id": None, "type": "change_created",
           "payload": {"change_id": 1, "title": "t"}})
    b.add({"id": "e2", "agent_id": None, "type": "tiers_resolved",
           "payload": {"tiers": {
               "medium": {"provider": "anthropic", "model": "haiku",
                          "differ_tier": None, "differ_from": None}},
               "steps": {}}})
    root = b.tree.root
    nodes = [c for c in root.children if isinstance(c, nv.TiersResolvedNode)]
    assert len(nodes) == 1
    assert nodes[0].rows == [
        {"tier": "medium", "provider": "anthropic", "model": "haiku",
         "differ_tier": None, "differ_from": None, "warning": None}]


def test_tiers_resolved_renders_constrained_and_unconstrained() -> None:
    node = nv.TiersResolvedNode(rows=[
        {"tier": "medium", "provider": "anthropic", "model": "sonnet",
         "differ_tier": None, "differ_from": None},
        {"tier": "code-reviewer", "provider": "openai", "model": "gpt-5.5",
         "differ_tier": 1, "differ_from": "task-lead"},
        {"tier": "high", "provider": "anthropic", "model": "opus",
         "differ_tier": 2, "differ_from": "code-reviewer"},
    ])
    # The tier table is lean-suppressed (point 2) — it renders only in --full.
    out = list(rt._render(node, depth=0, full=True, color=False, width=120))

    assert out[0] == "tiers resolved:"

    # unconstrained: provider/model only, no differ annotation
    code_line = next(ln for ln in out if ln.lstrip().startswith("medium "))
    assert "→ anthropic/sonnet" in code_line
    assert "differ from" not in code_line

    # tier-1 constrained: vendor jump. The differ note is now on its OWN line
    # (not appended to the row) so a long note can't overflow and wrap mid-word.
    cr_line = next(ln for ln in out if "code-reviewer" in ln)
    assert "→ openai/gpt-5.5" in cr_line
    assert "differ from" not in cr_line
    assert any("differ from task-lead: tier 1, vendor jump" in ln for ln in out)

    # tier-2 constrained: same-vendor fallback
    spec_line = next(ln for ln in out if ln.lstrip().startswith("high"))
    assert "→ anthropic/opus" in spec_line
    assert "differ from" not in spec_line
    assert any("differ from code-reviewer: tier 2, same-vendor fallback" in ln for ln in out)


def test_breadcrumb_line_renders_check_step_and_seconds() -> None:
    # plain continue: gray "<name> · complete · <elapsed>", no verb suffix.
    bc = nv.BreadcrumbLine(step_name="spec", elapsed_s=12.4,
                           action={"verb": "continue"}, extras=[])
    line = "\n".join(rt._render(bc, depth=1, full=False, color=False, width=80))
    assert line == "  spec · complete · 12s"
    assert "→ continue" not in line
    # the DISPLAY label (step_label) replaces the raw id on the completion line.
    bc_lbl = nv.BreadcrumbLine(step_name="plan-reviewer", step_label="reviewing plan",
                               elapsed_s=5.0, action={"verb": "continue"}, extras=[])
    assert "\n".join(rt._render(bc_lbl, depth=1, full=False, color=False, width=80)) \
        == "  reviewing plan · complete · 5s"
    # A workflow route label no longer renders beneath the completion; a labeled
    # completion is the single gray completion line (the reason rides the next header).
    bc2 = nv.BreadcrumbLine(step_name="code-reviewer", elapsed_s=41.0,
                            action={"verb": "goto", "target": "task-lead",
                                    "resume": False, "rework": True},
                            label="addressing review feedback", extras=["status=Success"])
    lines2 = list(rt._render(bc2, depth=1, full=False, color=False, width=80))
    assert lines2 == ["  code-reviewer · complete · 41s"]
    # goto WITHOUT a route label: only the completion line, no extra line, no empty gap.
    bc3 = nv.BreadcrumbLine(step_name="spec-reviewer", elapsed_s=2.0,
                            action={"verb": "goto", "target": "plan-writer",
                                    "resume": False, "rework": False})
    lines3 = list(rt._render(bc3, depth=1, full=False, color=False, width=80))
    assert lines3 == ["  spec-reviewer · complete · 2s"]
    assert not any(" ·  · " in ln for ln in lines3)
    # a non-goto verb (pause) still renders its action_text after "complete".
    bc5 = nv.BreadcrumbLine(step_name="autosquash", elapsed_s=None,
                            action={"verb": "pause"}, extras=[])
    assert "\n".join(rt._render(bc5, depth=1, full=False, color=False, width=80)) \
        == "  autosquash · complete · pause"


def _sub(outcome=None, *, is_error=False, name="reviewer"):
    """A SubagentFold with an empty nested builder and the given outcome string."""
    return nv.SubagentFold(subagent_name=name, event_id="e", block_id="b",
                           nested=nv.NarrativeBuilder(), outcome=outcome,
                           is_error=is_error)


def test_header_line_renders_subagent_boundary() -> None:
    sub = _sub(name="reviewer")
    # Subagent color, no fold glyph, no (running)/(error) suffix.
    assert rt.header_line(sub, depth=2, color=False) == "    · reviewer"


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


def test_full_subagent_content_carries_magenta_gutter() -> None:
    # color=False, --full: the subagent's recursed prose + outcome carry a plain
    # `│ ` at the subagent's column; the expanded fold header does NOT. (In lean
    # there is no recursed content — see test_collapsed_subagent_renders_single_*.)
    out = _lines([
        _ev("2", "step_started", step="s", kind="agent", phase="p",
            model="m", provider="anthropic"),
        _ev("tk1", "tool_call", agent_id="step:p/s", id="b1", name="sub_agent",
            input={"subagent_name": "reviewer", "task_description": "go"}),
        _ev("su1", "llm_response", agent_id="sub:tk1", provider="anthropic", model="m",
            stop_reason="end_turn", usage={},
            raw={"content": [{"type": "text", "text": "looks good"}]}),
        _ev("r1", "tool_result", agent_id="step:p/s", id="b1",
            content='{"status":"Success"}', error=False),
    ], full=True)
    # prose + outcome at depth 1 (aligned indent: neither phase nor step adds a
    # level): col 0 is the subagent's own gutter.
    assert any(ln == "│ • looks good" for ln in out)
    assert any(ln == "│ → status=Success" for ln in out)


def test_steps_de_indent_to_column_zero() -> None:
    # The phase indent level is dropped: a step header inside a phase renders flush
    # at column 0 (was column 2), aligned with the phase rule.
    out = _lines([
        _ev("1", "change_created", title="t"),
        _ev("2", "step_started", step="spec", kind="agent", phase="design", model="m"),
        _ev("3", "llm_response", agent_id="a",
            raw={"content": [{"type": "text", "text": "hi"}]}, usage={}),
    ])
    header = next(ln for ln in out if "spec · m" in ln)
    assert header == "spec · m"                 # column 0, no leading indent


def test_subagent_row_de_indents_to_column_two() -> None:
    # The lean subagent status row shifts left with everything else: its own 2-col
    # label offset now lands it at column 2 (was column 4 = phase + step + label).
    out = _lines([
        _ev("1", "change_created", title="t"),
        _ev("2", "step_started", step="spec", kind="agent", phase="design", model="m"),
        _ev("3", "tool_call", agent_id="a", id="t1", name="sub_agent",
            input={"subagent_name": "reviewer"}),
        _ev("4", "llm_response", agent_id="sub:3",
            raw={"content": [{"type": "text", "text": "x"}]}, usage={}),
        _ev("5", "tool_result", agent_id="a", id="t1", content='{"status":"Success"}'),
    ])
    row = next(ln for ln in out if ln.strip() == "reviewer")
    assert row == "  reviewer"                  # column 2: step at col 0 + 2-space label


def test_subagent_header_excludes_own_gutter() -> None:
    sub = _sub(name="reviewer")
    assert rt.header_line(sub, depth=0, color=False) == "· reviewer"
    # ancestor gutters show; the header's own column does not.
    assert rt.header_line(sub, depth=1, color=False, gutters=(0,)) == "│ · reviewer"


def test_nested_subagent_emits_two_gutter_levels() -> None:
    # Sub-subagents can't be built through the event pipeline (single-level
    # routing), so construct the nested SubagentFold directly and render it.
    # The recursive gutter behavior lives in --full; lean collapses to one row.
    inner = nv.SubagentFold(subagent_name="inner", event_id="e2", block_id="b2",
                            nested=nv.NarrativeBuilder(), running=False)
    inner.nested.tree.root.children.append(nv.LiteralTextNode(text="deep finding"))
    outer = nv.SubagentFold(subagent_name="outer", event_id="e1", block_id="b1",
                            nested=nv.NarrativeBuilder(), running=False)
    outer.nested.tree.root.children.append(inner)
    out = list(rt._render(outer, depth=0, full=True, color=False, width=80))
    assert out[0] == "▾ outer"                       # depth 0, no gutter
    assert any(ln == "│ ▾ inner" for ln in out)      # depth 1, one gutter
    assert any(ln == "│ │ • deep finding" for ln in out)      # depth 2, two gutters


def test_errored_subagent_gutter_stays_magenta_with_red_outcome() -> None:
    # seam 1 / error path (--full): magenta gutter + red outcome TEXT on the same
    # line. In lean the outcome is suppressed; this seam lives in --full now.
    out = list(render_tree(nv.build_tree([
        _ev("2", "step_started", step="s", kind="agent", phase="p",
            model="m", provider="anthropic"),
        _ev("tk1", "tool_call", agent_id="step:p/s", id="b1", name="sub_agent",
            input={"subagent_name": "reviewer", "task_description": "go"}),
        _ev("r1", "tool_result", agent_id="step:p/s", id="b1",
            content="dispatch failed: boom", error=True),
    ]), full=True, color=True, width=80))
    line = next(ln for ln in out if "dispatch failed: boom" in ln)
    assert "\x1b[35m│ \x1b[0m" in line   # magenta gutter span (identity)
    assert "\x1b[31m" in line             # red outcome text span (status)


def test_full_subagent_nested_thinking_keeps_dim_text_and_gains_gutter() -> None:
    out = list(render_tree(nv.build_tree([
        _ev("2", "step_started", step="s", kind="agent", phase="p",
            model="m", provider="anthropic"),
        _ev("tk1", "tool_call", agent_id="step:p/s", id="b1", name="sub_agent",
            input={"subagent_name": "reviewer", "task_description": "go"}),
        _ev("t1", "thinking", agent_id="sub:tk1", content="nested musings"),
        _ev("r1", "tool_result", agent_id="step:p/s", id="b1",
            content='{"status":"Success"}', error=False),
    ]), full=True, color=True, width=80))
    line = next(ln for ln in out if "nested musings" in ln)
    assert "\x1b[35m│ \x1b[0m" in line   # magenta gutter
    assert "\x1b[2m" in line             # dim thinking text preserved


def test_phase_breadcrumb_renders_after_closed_phase() -> None:
    # Two phases: the first (spec) is closed when the second (plan) opens.
    # The breadcrumb requires both step timestamps to compute duration.
    events = [
        {"id": "e0", "agent_id": None, "type": "change_created",
         "payload": {"title": "t"}},
        {"id": "e1", "agent_id": None, "type": "step_started",
         "payload": {"step": "step1", "kind": "branch", "phase": "spec"},
         "timestamp": "2026-06-03T00:00:00+00:00"},
        {"id": "e2", "agent_id": None, "type": "step_completed",
         "payload": {"step": "step1", "kind": "branch", "action": {"verb": "continue"}},
         "timestamp": "2026-06-03T00:00:07+00:00"},
        # Starting plan phase forces spec phase to close
        {"id": "e3", "agent_id": None, "type": "step_started",
         "payload": {"step": "step2", "kind": "branch", "phase": "plan"},
         "timestamp": "2026-06-03T00:00:07+00:00"},
    ]
    out = list(render_tree(nv.build_tree(events), full=False, color=False, width=80))
    joined = "\n".join(out)
    # closed spec phase gets a breadcrumb with seconds suffix
    assert "spec phase complete" in joined
    spec_line = next(ln for ln in out if "spec phase complete" in ln)
    assert "s" in spec_line    # e.g. "7s"
    assert "7" in spec_line    # computed from timestamps
    # the breadcrumb is at depth 0 (same as the phase header), plain gray, no rule
    assert spec_line == "spec phase complete · 7s"


_ANSI_RE = _re.compile(r"\x1b\[[0-9;]*m")


def _visible(s: str) -> str:
    return _ANSI_RE.sub("", s)


def test_markdown_block_keeps_gutter_and_visible_width() -> None:
    from imp.cli.render_text import _render_markdown_block
    text = "# Heading\n\nSome **bold** and a list:\n\n- one\n- two\n"
    lines = _render_markdown_block(text, width=40, depth=2, color=True,
                                   gutters=(1,))   # one subagent gutter level
    assert lines, "expected rendered lines"
    for ln in lines:
        # never splits an escape: every ESC[ is followed by a terminator 'm'
        assert ln.count("\x1b[") == len(_re.findall(r"\x1b\[[0-9;]*m", ln))
        # depth=2 / gutters=(1,): level-0 is plain ("  "), level-1 is the gutter bar.
        # The visible prefix must be exactly "  │ " — two spaces then the bar.
        assert _visible(ln).startswith("  │ ")
    # heading text survives markdown rendering
    assert any("Heading" in _visible(ln) for ln in lines)


def test_markdown_block_plain_when_color_off() -> None:
    from imp.cli.render_text import _render_markdown_block
    lines = _render_markdown_block("**bold** text", width=40, depth=0,
                                   color=False, gutters=())
    joined = "\n".join(lines)
    assert "\x1b[" not in joined          # no ANSI when color disabled
    assert "bold" in joined


def test_inline_code_is_cyan_not_bold_and_no_dark_bg() -> None:
    # rich's default theme paints inline `code` with \x1b[1;36;40m (bold + cyan
    # + black bg). The Theme({"markdown.code": "cyan"}) override drops bold and
    # the dark bg cleanly; the span stays foreground-cyan.
    from imp.cli.render_text import _render_markdown_block
    lines = _render_markdown_block("Try `inline code` here.", width=40, depth=0,
                                   color=True, gutters=())
    joined = "\n".join(lines)
    assert "inline code" in _visible(joined)
    assert joined.count("\x1b[1m") == 1     # only the turn bullet is bold
    assert "\x1b[40m" not in joined         # no dark default-bg
    assert "\x1b[1;36;40m" not in joined    # no rich-default combined code
    assert "\x1b[36m" in joined             # still cyan foreground


def test_markdown_headers_are_cyan_bold_not_magenta() -> None:
    # rich's defaults paint headers magenta (h2 underline / h3 bold). We recolor
    # every level to the same cyan as inline `code`, keeping bold but dropping
    # underline, so headers wear imp's accent rather than rich's magenta. Rendered
    # at depth=0 with no gutter so the only color in play is the heading itself.
    from imp.cli.render_text import _render_markdown_block
    for header in ("# Title", "## Section", "### Sub"):
        lines = _render_markdown_block(header, width=44, depth=0, color=True,
                                       gutters=())
        joined = "\n".join(lines)
        assert "36m" in joined, f"{header!r} should be cyan"      # cyan foreground
        assert "35m" not in joined, f"{header!r} should not be magenta"
        # bold is param 1 inside the combined SGR (e.g. \x1b[1;36m)
        assert _re.search(r"[\[;]1[;m]", joined), f"{header!r} should stay bold"
        # underline is param 4; it must be gone now
        assert not _re.search(r"[\[;]4[;m]", joined), f"{header!r} should not be underlined"


def test_markdown_fenced_code_block_has_no_background_fill() -> None:
    # The fenced block renders syntax-highlighted tokens on the terminal's own
    # background (no full-width dark bar), the same calm treatment inline `code`
    # gets — so the block reads as indented colored code, not a bleeding box.
    from imp.cli.render_text import _render_markdown_block
    lines = _render_markdown_block(
        "```python\ndef hi():\n    return 1\n```", width=44, depth=0, color=True,
        gutters=())
    joined = "\n".join(lines)
    assert "\x1b[40m" not in joined          # no dark default-bg fill
    assert "def hi():" in _visible(joined)   # code survives rendering
    assert "return 1" in _visible(joined)


def test_markdown_block_blank_line_inside_prose_carries_magenta_gutter() -> None:
    """Two paragraphs (blank line between) rendered with a subagent gutter level
    must emit the bare gutter-indent on the blank line — keeping the magenta │
    continuous through the prose block. Inspected on the line list so any
    regression where the blank renders as a raw "" is caught."""
    from imp.cli.render_text import _render_markdown_block
    lines = _render_markdown_block(
        "paragraph one\n\nparagraph two", width=40, depth=1, color=True,
        gutters=(0,))
    bar = "\x1b[35m│ \x1b[0m"
    # exactly one blank line carrying the bare gutter-indent (and nothing else)
    assert bar in lines, f"gutter-only blank line missing from {lines!r}"
    # the bare-gutter line is exactly the indent (no trailing whitespace)
    blank_indices = [i for i, ln in enumerate(lines) if ln == bar]
    assert len(blank_indices) == 1
    # prose surrounds it
    assert any("paragraph one" in ln for ln in lines[:blank_indices[0]])
    assert any("paragraph two" in ln for ln in lines[blank_indices[0] + 1:])


def test_prose_then_structural_leaf_separated_by_blank() -> None:
    """After any prose block, the next emission in the same children-context
    gets one blank-line separator — not only another prose block. Today the
    step_completed breadcrumb sits directly under the last prose line; the
    generalized rule inserts one gutter-bearing blank between them."""
    out = _lines([
        _ev("1", "change_created", title="t"),
        _ev("2", "step_started", step="spec", kind="agent", phase="design",
            model="m", provider="anthropic"),
        _ev("3", "llm_response", agent_id="a",
            raw={"content": [{"type": "text", "text": "Drafting."}]}, usage={}),
        _ev("4", "step_completed", step="spec", kind="agent",
            action={"verb": "continue"}, var_mutations={}),
    ])
    prose_i = next(i for i, ln in enumerate(out) if "Drafting." in ln)
    bc_i = next(i for i, ln in enumerate(out) if "spec · complete" in ln)
    assert bc_i > prose_i
    assert out[bc_i - 1].strip() == "", (
        "prose must be blank-separated from the following breadcrumb")


def test_prose_then_subagent_header_separated_by_blank() -> None:
    """Prose then a subagent header (a non-prose container child) also gets a
    blank separator — the rule applies to ALL non-prose next children, not just
    leaves."""
    out = _lines([
        _ev("1", "change_created", title="t"),
        _ev("2", "step_started", step="spec", kind="agent", phase="design",
            model="m", provider="anthropic"),
        _ev("3", "llm_response", agent_id="a",
            raw={"content": [{"type": "text", "text": "Drafting."}]}, usage={}),
        _ev("tk", "tool_call", agent_id="a", id="b1", name="sub_agent",
            input={"subagent_name": "reviewer"}),
        _ev("r", "tool_result", agent_id="a", id="b1",
            content='{"status":"Success"}'),
    ])
    prose_i = next(i for i, ln in enumerate(out) if "Drafting." in ln)
    hdr_i = next(i for i, ln in enumerate(out) if ln.strip().startswith("reviewer"))
    assert hdr_i > prose_i
    assert out[hdr_i - 1].strip() == ""


def test_user_message_orange_caret_then_text_color_on() -> None:
    """The user turn renders at the node's own depth (depth=1 → a 2-space indent).
    First line: a brand-orange `>` caret (outside any slate) + one space + the body.
    Continuation lines drop the caret (a blank column in its place) so the body
    aligns under the first line's text. No background fill in the ansi theme."""
    node = nv.UserMessageNode(text="line one\nline two", interactive=True)
    out = list(rt._render(node, depth=1, full=False, color=True, width=80))
    assert len(out) == 2
    assert out[0] == "  \x1b[33m>\x1b[0m line one"
    assert out[1] == "    line two"               # caret column blank, body aligned
    assert "\x1b[48;2;" not in out[0] and "\x1b[104m" not in out[0]   # no bg


def test_user_message_caret_with_subtle_bg_in_truecolor_theme() -> None:
    """The reprinted (scrolled-back) user turn carries a subtle entered-input
    background in a truecolor theme, opened AFTER the caret: the `>` sits outside
    the slate; the separating space + the text sit on it, and it closes at text end
    (no full-width fill → clean copy/paste)."""
    pal.set_theme("dark")
    try:
        node = nv.UserMessageNode(text="hello", interactive=True)
        out = list(rt._render(node, depth=0, full=False, color=True, width=80))
    finally:
        pal.set_theme("ansi")
    assert len(out) == 1
    caret = "\x1b[38;2;255;154;60m>\x1b[0m"   # truecolor brand-orange caret
    bg = "\x1b[48;2;43;47;58m"                 # subtle slate (#2B2F3A) after the caret
    assert out[0] == caret + bg + " hello\x1b[0m"   # space + text on the bg, reset at end


def test_user_message_multiline_continuation_carries_slate_no_caret() -> None:
    """A multi-line truecolor turn: the continuation line drops the caret (blank
    column in its place) but still carries the slate band, aligned under the body."""
    pal.set_theme("dark")
    try:
        node = nv.UserMessageNode(text="one\ntwo", interactive=True)
        out = list(rt._render(node, depth=0, full=False, color=True, width=80))
    finally:
        pal.set_theme("ansi")
    assert len(out) == 2
    caret = "\x1b[38;2;255;154;60m>\x1b[0m"
    bg = "\x1b[48;2;43;47;58m"
    assert out[0] == caret + bg + " one\x1b[0m"   # caret outside, band hugs the text
    assert out[1] == " " + bg + " two\x1b[0m"     # blank caret column, band continues


def test_user_message_no_color_plain_caret() -> None:
    node = nv.UserMessageNode(text="line one\nline two", interactive=True)
    out = list(rt._render(node, depth=1, full=False, color=False, width=80))
    joined = "\n".join(out)
    assert "\x1b[94m" not in joined and "\x1b[104m" not in joined and "┃" not in joined
    # Color off degrades to a plain `> {text}` caret with no slate/SGR background.
    assert out[0] == "  > line one"
    assert out[1] == "    line two"


def test_lean_subagent_is_single_status_row() -> None:
    # Lean: a subagent renders as ONE status row — no children, no outcome prose,
    # no SDK rejection sentinel.
    from imp.cli import narrative as nv
    from imp.cli.render_text import _render

    fold = nv.SubagentFold(subagent_name="reviewer", event_id="e", block_id="b",
                           nested=nv.NarrativeBuilder())
    fold.running = False
    fold.outcome = ("The user doesn't want to proceed with this tool use. "
                    "The tool use was rejected...")  # the sentinel
    lines = list(_render(fold, depth=0, full=False, color=False, width=80))
    text = "\n".join(lines)
    assert "reviewer" in text            # one clean row, no bullet, no status suffix
    assert "· reviewer" not in text
    assert "doesn't want to proceed" not in text   # sentinel never surfaces
    assert len(lines) == 1                # exactly one status row


def test_lean_subagent_status_glyphs() -> None:
    from imp.cli import narrative as nv
    from imp.cli.render_text import subagent_status_row

    def row(**kw):
        f = nv.SubagentFold(subagent_name="s", event_id="e", block_id="b",
                            nested=nv.NarrativeBuilder())
        for k, v in kw.items():
            setattr(f, k, v)
        return subagent_status_row(f, depth=0, color=False, gutters=())

    # Success carries NO status suffix (point 6 — it read as noise); only the
    # abnormal terminal states keep a (state) tag so a broken subagent still stands out.
    # No leading bullet: the name is indented two cols under the agent turn.
    assert row(running=False) == "  s"
    assert "(" not in row(running=False)
    assert "(running)" in row(running=True)
    assert "(failed)" in row(running=False, is_error=True)
    assert "(stalled)" in row(running=False, is_error=True, terminal_state="stalled")
    assert "(interrupted)" in row(running=False, is_error=True,
                                  terminal_state="interrupted")


def test_full_subagent_unchanged_shows_children() -> None:
    # --full still recurses (regression guard).
    from imp.cli import narrative as nv
    from imp.cli.render_text import _render
    fold = nv.SubagentFold(subagent_name="reviewer", event_id="e", block_id="b",
                           nested=nv.NarrativeBuilder())
    fold.running = False
    fold.outcome = '{"summary": "did the thing"}'
    lines = list(_render(fold, depth=0, full=True, color=False, width=80))
    assert any("did the thing" in ln for ln in lines)   # outcome still shown in --full


def test_stalled_subagent_renders_stalled_glyph_through_builder() -> None:
    # END-TO-END (no hand-built {"kind": ...}): feed the REAL task tool_result the
    # provider builds from dispatch_task's stalled RuntimeError, through the
    # NarrativeBuilder, then render lean — assert (stalled).
    from imp.cli import narrative as nv
    from imp.cli.render_text import _render

    exc = RuntimeError("subagent 'reviewer' failed (config) [stalled]")
    content = f"{type(exc).__name__}: {exc}"   # provider formatting

    b = nv.NarrativeBuilder()
    b.add({"type": "tool_call", "payload": {
        "name": "sub_agent", "id": "b1",
        "input": {"subagent_name": "reviewer", "task_description": "x"}}})
    b.add({"type": "tool_result", "payload": {
        "id": "b1", "content": content, "error": True}})

    folds = [n for n in b.tree.root.children if isinstance(n, nv.SubagentFold)]
    assert folds, "expected a SubagentFold from the task tool_call/result pair"
    fold = folds[0]
    assert fold.terminal_state == "stalled"
    lines = list(_render(fold, depth=0, full=False, color=False, width=80))
    assert any("(stalled)" in ln for ln in lines)


def test_full_burst_per_call_args_and_output_block() -> None:
    body = "\n".join(f"line{i}" for i in range(1, 13))      # 12 source lines
    out = "\n".join(_lines([
        _ev("2", "step_started", step="s", kind="agent", phase="p",
            model="m", provider="anthropic"),
        _ev("4", "tool_call", agent_id="step:p/s", id="b1", name="bash",
            input={"command": "make test\n&& echo done"}),
        _ev("5", "tool_result", agent_id="step:p/s", id="b1", content=body),
        _ev("7", "step_completed", step="s", kind="agent",
            action={"verb": "continue"}, var_mutations={}),
    ], full=True))
    # multi-line arg value collapsed to a single space:
    assert "· bash  command=make test && echo done" in out
    # output block: first source line under the "→ " column, 10 shown, tail count:
    assert "→ line1" in out
    assert "line10" in out
    assert "line11" not in out
    assert "… (+2 more lines)" in out


def test_full_burst_killed_wins_over_error_tag() -> None:
    out = "\n".join(_lines([
        _ev("2", "step_started", step="s", kind="agent", phase="p"),
        _ev("4", "tool_call", agent_id="step:p/s", id="b1", name="bash",
            input={"command": "x"}),
        _ev("5", "tool_result", agent_id="step:p/s", id="b1", content="boom",
            error=True),
        _ev("k1", "tool_killed", agent_id="step:p/s", id="b1", name="bash"),
        _ev("7", "step_completed", step="s", kind="agent",
            action={"verb": "continue"}, var_mutations={}),
    ], full=True))
    assert "· bash [killed]" in out
    assert "[error]" not in out


def test_full_burst_empty_result_no_arrow_but_footer_renders() -> None:
    out = "\n".join(_lines([
        _ev("2", "step_started", step="s", kind="agent", phase="p"),
        _ev("4", "tool_call", agent_id="step:p/s", id="b1", name="web_search",
            input={"query": "x"}),
        _ev("5", "tool_result", agent_id="step:p/s", id="b1", content="",
            citations=[{"url": "https://a.test"}]),
        _ev("7", "step_completed", step="s", kind="agent",
            action={"verb": "continue"}, var_mutations={}),
    ], full=True))
    assert "· web_search  query=x" in out
    assert "→ " not in out                       # empty result: no arrow anchor
    assert "Sources:" in out and "https://a.test" in out


def test_full_burst_citation_footer_bel_becomes_caret() -> None:
    out = "\n".join(_lines([
        _ev("2", "step_started", step="s", kind="agent", phase="p",
            model="m", provider="anthropic"),
        _ev("3", "tool_call", agent_id="step:p/s", id="b1", name="web_search",
            input={"query": "x"}),
        _ev("4", "tool_result", agent_id="step:p/s", id="b1", content="",
            citations=[{"title": "ring\x07bell", "url": "https://a.test"}]),
        _ev("6", "step_completed", step="s", kind="agent",
            action={"verb": "continue"}, var_mutations={}),
    ], full=True))
    assert "\x07" not in out
    assert "^G" in out


def test_full_burst_empty_burst_renders_nothing() -> None:
    burst = nv.ToolBurstFold(calls=[])
    lines = list(rt._render_burst(burst, depth=1, full=True, color=False, width=80))
    assert lines == []


def test_burst_recap_is_count_first() -> None:
    out = "\n".join(_lines([
        _ev("2", "step_started", step="s", kind="agent", phase="p"),
        _ev("4", "tool_call", agent_id="step:p/s", id="b1", name="read_file",
            input={"path": "x"}),
        _ev("5", "tool_result", agent_id="step:p/s", id="b1", content="ok"),
        _ev("6", "tool_call", agent_id="step:p/s", id="b2", name="bash",
            input={"command": "y"}),
        _ev("7", "tool_result", agent_id="step:p/s", id="b2", content="ok"),
        _ev("8", "step_completed", step="s", kind="agent",
            action={"verb": "continue"}, var_mutations={}),
    ]))
    assert "2 calls · read_file · bash" in out
    assert "read_file · bash · 2 calls" not in out


def test_chrome_step_prints_header_and_complete_breadcrumb() -> None:
    out = "\n".join(_lines([
        _ev("2", "step_started", step="gate", kind="script", phase="verify"),
        _ev("3", "step_completed", step="gate", kind="script",
            action={"verb": "continue"}, var_mutations={}),
    ]))
    assert "gate" in out
    assert "gate · complete" in out          # show_complete now unconditional, dotted


def test_is_chrome_step_is_deleted() -> None:
    assert not hasattr(rt, "is_chrome_step")
    assert not hasattr(rt, "_CHROME_KINDS")


def test_error_detail_block_renders_in_full_when_adds_info() -> None:
    out = "\n".join(_lines([
        _ev("e1", "error", category="loop", message="[loop] repeated failure x3: clip…",
            detail={"full_summary": "the complete unclipped detail line"}),
    ], full=True))
    assert "the complete unclipped detail line" in out


def test_error_detail_block_suppressed_when_substring_of_message() -> None:
    out = "\n".join(_lines([
        _ev("e1", "error", category="loop",
            message="[loop] repeated failure x3: embedded detail",
            detail={"full_summary": "embedded detail"}),
    ], full=True))
    assert out.count("embedded detail") == 1     # message only; no extra block


def test_error_detail_block_absent_in_lean() -> None:
    out = "\n".join(_lines([
        _ev("e1", "error", category="loop", message="[loop] x3: clip…",
            detail={"full_summary": "full detail here"}),
    ], full=False))
    assert "full detail here" not in out


def test_interrupt_renders_interrupted_not_complete_pause() -> None:
    out = "\n".join(_lines([
        _ev("1", "step_started", step="spec-writer", kind="agent", phase="design",
            model="m", provider="anthropic"),
        _ev("2", "error", category="config", kind="interrupted",
            message="runner cancelled by interrupt"),
        _ev("3", "step_completed", step="spec-writer", kind="agent",
            action={"verb": "pause"}, var_mutations={}),
        _ev("4", "workflow_paused", reason="interrupt", interrupt_reason="ctrl-c"),
    ]))
    assert "spec-writer interrupted" in out
    assert "complete · pause" not in out
    assert "error:" not in out
    assert "runner cancelled by interrupt" not in out
    assert "paused (ctrl-c)" in out
    assert "⏸" not in out


def test_intentional_pause_still_renders_complete_pause() -> None:
    out = "\n".join(_lines([
        _ev("1", "step_started", step="spec-writer", kind="agent", phase="design",
            model="m", provider="anthropic"),
        _ev("2", "step_completed", step="spec-writer", kind="agent",
            action={"verb": "pause"}, var_mutations={}),
    ]))
    assert "spec-writer · complete · pause" in out
    assert "interrupted" not in out


def test_merge_step_pause_breadcrumb_reads_blocked() -> None:
    bc = nv.BreadcrumbLine(step_name="merge-to-base", elapsed_s=None,
                           action={"verb": "pause"}, extras=[], kind="merge_and_exit_worktree")
    line = "\n".join(rt._render(bc, depth=1, full=False, color=False, width=80))
    assert line == "  merge-to-base blocked"
    assert "complete" not in line
    # a NON-merge pause keeps the existing wording
    bc2 = nv.BreadcrumbLine(step_name="autosquash", elapsed_s=None,
                            action={"verb": "pause"}, extras=[], kind="script")
    line2 = "\n".join(rt._render(bc2, depth=1, full=False, color=False, width=80))
    assert line2 == "  autosquash · complete · pause"


def test_merge_step_pause_breadcrumb_pipeline() -> None:
    tree = nv.build_tree([
        _ev("1", "step_started", step="merge-to-base",
            kind="merge_and_exit_worktree", phase="review"),
        _ev("2", "step_completed", step="merge-to-base", kind="merge_and_exit_worktree",
            action={"verb": "pause"}, var_mutations={}),
    ])
    # The BreadcrumbLine produced through the pipeline must also read "blocked",
    # not "complete · pause", verifying that kind=node.kind is wired in _h_step_completed.
    rendered = "\n".join(rt.render_tree(tree, full=False, color=False, width=80))
    assert "merge-to-base blocked" in rendered
    assert "complete" not in rendered


def test_merged_node_renders_friendly_footer() -> None:
    node = nv.MergedNode(change_id="2026-06-12-run-x", base="master")
    plain = "\n".join(rt._render(node, depth=0, full=False, color=False, width=80))
    assert plain == "Merged 2026-06-12-run-x into master."
    colored = "\n".join(rt._render(node, depth=0, full=False, color=True, width=80))
    assert "✓ Merged 2026-06-12-run-x into master." in colored
    assert pal._ANSI["yellow"] in colored
    assert pal._ANSI["green"] not in colored


def test_merged_node_renders_lifecycle_wordmark_in_all_themes() -> None:
    # the merge footer carries the theme's wordmark SGR (the warm accent, not green)
    node = nv.MergedNode(change_id="2026-06-12-run-x", base="master")
    for theme in ("ansi", "dark", "light"):
        pal.set_theme(theme)
        try:
            colored = "\n".join(rt._render(node, depth=0, full=False, color=True, width=80))
            assert pal.sgr("wordmark", enabled=True) in colored
            assert pal._ANSI["green"] not in colored
        finally:
            pal.set_theme("ansi")


def test_complete_step_findings_hidden_lean_visible_full() -> None:
    events = [
        _ev("1", "step_started", step="rev", kind="agent", phase="review",
            model="m", provider="anthropic"),
        _ev("2", "agent_output", agent_id="step:review/rev",
            verdict="rework",
            findings="Line 1 is wrong.\nLine 2 needs a guard.\nLine 3 leaks."),
        _ev("3", "step_completed", step="rev", kind="agent",
            action={"verb": "continue"}, var_mutations={}),
    ]
    lean = "\n".join(_lines(events, full=False))
    full = "\n".join(_lines(events, full=True))
    assert "Line 2 needs a guard." not in lean    # hidden by default
    assert "Line 2 needs a guard." in full         # surfaced under --full


def test_clean_review_findings_hidden_by_default() -> None:
    events = [
        _ev("1", "step_started", step="review", kind="agent", phase="review",
            model="m", provider="anthropic"),
        _ev("c1", "tool_call", agent_id="step:review/review", id="b1",
            name="sub_agent", input={"subagent_name": "code-reviewer",
                                "task_description": "review"}),
        _ev("r1", "tool_result", agent_id="step:review/review", id="b1",
            content=json.dumps({"status": "Success", "verdict": "approve",
                                "findings": ""}), error=False),
        _ev("ao", "agent_output", agent_id="step:review/review",
            status="Success", verdict="approve",
            findings="review complete: one nit\nno blocking issues"),
        _ev("sc", "step_completed", step="review", kind="agent",
            action={"verb": "goto", "target": "integrate-rebase"}, var_mutations={}),
    ]
    lean = "\n".join(_lines(events))
    full = "\n".join(_lines(events, full=True))
    assert "panel:" not in lean
    assert "code-reviewer" in lean          # the subagent fold still renders
    assert "one nit" not in lean            # findings hidden by default (E1)
    assert "one nit" in full                # surfaced under --full


def test_display_tool_name_strips_mcp_namespace() -> None:
    assert nv.display_tool_name("mcp__invoke") == "invoke"
    assert nv.display_tool_name("mcp__imp__read_file") == "read_file"
    assert nv.display_tool_name("bash") == "bash"


def test_burst_recap_strips_mcp_prefix_from_tool_names() -> None:
    out = "\n".join(_lines([
        _ev("2", "step_started", step="s", kind="agent", phase="p",
            model="m", provider="anthropic"),
        _ev("3", "tool_call", agent_id="step:p/s", id="b1",
            name="mcp__invoke", input={}),
        _ev("4", "tool_result", agent_id="step:p/s", id="b1",
            content="ok", error=False),
    ]))
    assert "invoke" in out
    assert "mcp__" not in out


def test_call_args_line_strips_mcp_prefix_from_tool_name() -> None:
    out = "\n".join(_lines([
        _ev("2", "step_started", step="s", kind="agent", phase="p",
            model="m", provider="anthropic"),
        _ev("3", "tool_call", agent_id="step:p/s", id="b1",
            name="mcp__invoke", input={}),
        _ev("4", "tool_result", agent_id="step:p/s", id="b1",
            content="ok", error=False),
    ], full=True))
    assert "invoke" in out
    assert "mcp__" not in out


def test_subagent_suspended_structured_kind_renders_interrupted() -> None:
    # Structured kind="interrupted" wins over the is_error->(failed) fallback.
    out = "\n".join(_lines([
        _ev("2", "step_started", step="s", kind="agent", phase="p",
            model="m", provider="anthropic"),
        _ev("3", "tool_call", agent_id="step:p/s", id="b1", name="sub_agent",
            input={"subagent_name": "reviewer"}),
        _ev("4", "tool_result", agent_id="step:p/s", id="b1",
            content="SubagentSuspended: subagent paused", error=True,
            kind="interrupted"),
    ]))
    assert "(interrupted)" in out
    assert "(failed)" not in out


def test_subagent_suspended_bracket_marker_renders_interrupted() -> None:
    # Bracketed [interrupted] marker in is_error content classifies as interrupted.
    out = "\n".join(_lines([
        _ev("2", "step_started", step="s", kind="agent", phase="p",
            model="m", provider="anthropic"),
        _ev("3", "tool_call", agent_id="step:p/s", id="b1", name="sub_agent",
            input={"subagent_name": "reviewer"}),
        _ev("4", "tool_result", agent_id="step:p/s", id="b1",
            content="SubagentSuspended: subagent paused [interrupted]", error=True),
    ]))
    assert "(interrupted)" in out
    assert "(failed)" not in out


def test_abnormal_then_healthy_same_name_subagent_shows_no_retrying() -> None:
    # a same-name subagent re-dispatched under the SAME opener
    # after an abnormal close must NOT inherit a "retrying (n)" tag. The first close
    # still shows its terminal (failed) tag; the second healthy row is clean.
    out = "\n".join(_lines([
        _ev("2", "step_started", step="s", kind="agent", phase="p",
            model="m", provider="anthropic"),
        _ev("3", "tool_call", agent_id="step:p/s", id="b1", name="sub_agent",
            input={"subagent_name": "reviewer"}),
        _ev("4", "tool_result", agent_id="step:p/s", id="b1",
            content="boom", error=True),
        _ev("5", "tool_call", agent_id="step:p/s", id="b2", name="sub_agent",
            input={"subagent_name": "reviewer"}),
        _ev("6", "tool_result", agent_id="step:p/s", id="b2",
            content='{"status": "Success"}', error=False),
    ]))
    assert "(failed)" in out          # the abnormal first close still reads (failed)
    assert "retrying" not in out      # but no retry annotation anywhere


def test_parallel_same_name_subagents_not_marked_retrying() -> None:
    # Two reviewers dispatched concurrently (both open before either closes) is a
    # panel, not a retry — neither row gets a retrying annotation.
    out = "\n".join(_lines([
        _ev("2", "step_started", step="s", kind="agent", phase="p",
            model="m", provider="anthropic"),
        _ev("3", "tool_call", agent_id="step:p/s", id="b1", name="sub_agent",
            input={"subagent_name": "reviewer"}),
        _ev("4", "tool_call", agent_id="step:p/s", id="b2", name="sub_agent",
            input={"subagent_name": "reviewer"}),
        _ev("5", "tool_result", agent_id="step:p/s", id="b1",
            content='{"status": "Success"}', error=False),
        _ev("6", "tool_result", agent_id="step:p/s", id="b2",
            content='{"status": "Success"}', error=False),
    ]))
    assert "retrying" not in out



def test_live_viewer_suppresses_step_context_block_even_in_full() -> None:
    events = [
        _ev("2", "step_started", step="s", kind="agent", phase="p",
            model="m", provider="anthropic"),
        _ev("3", "step_context", agent_id="step:p/s", provider="anthropic",
            model="claude", system_prompt="SYSTEM PROMPT TEXT",
            tools_schema=[{"name": "bash"}], params={}, max_tool_calls=50),
    ]
    live = "\n".join(render_tree(nv.build_tree(events), full=True, color=False,
                                 width=80, live_viewer=True))
    assert "SYSTEM PROMPT TEXT" not in live
    assert "- bash" not in live
    assert "context ·" not in live          # the whole fold (header included) is gone
    log = "\n".join(render_tree(nv.build_tree(events), full=True, color=False,
                                width=80))
    assert "SYSTEM PROMPT TEXT" in log and "- bash" in log   # imp log --full unchanged


def test_live_viewer_keeps_raw_tool_output_in_full() -> None:
    events = [
        _ev("2", "step_started", step="s", kind="agent", phase="p", model="m"),
        _ev("4", "tool_call", agent_id="step:p/s", id="b1", name="bash",
            input={"command": "ls -la"}),
        _ev("5", "tool_result", agent_id="step:p/s", id="b1",
            content="RAW-FILE-OUTPUT", error=False),
    ]
    live = "\n".join(render_tree(nv.build_tree(events), full=True, color=False,
                                 width=80, live_viewer=True))
    assert "RAW-FILE-OUTPUT" in live      # raw tool output still renders (AC3)


def test_breadcrumb_with_label_has_no_route_line() -> None:
    lines = list(rt._render(
        nv.BreadcrumbLine(
            step_name="finalize", kind="agent", elapsed_s=2,
            action={"verb": "goto", "target": "wait-for-head",
                    "resume": False, "rework": False},
            label="entering the merge queue"),
        depth=0, full=False, color=False, width=80))
    assert lines == ["finalize · complete · 2s"]


def test_breadcrumb_without_label_has_no_route_line() -> None:
    lines = list(rt._render(
        nv.BreadcrumbLine(
            step_name="finalize", kind="agent", elapsed_s=2,
            action={"verb": "goto", "target": "wait-for-head",
                    "resume": False, "rework": False},
            label=None),
        depth=0, full=False, color=False, width=80))
    assert lines == ["finalize · complete · 2s"]
    assert not any(ln.strip().startswith("→") for ln in lines)
    assert not any(" ·  · " in ln for ln in lines)   # no empty segment


def test_route_reason_rides_target_step_header() -> None:
    events = [
        _ev("1", "change_created", change_id=1, title="x"),
        _ev("2", "step_started", step="code-reviewer", kind="agent", phase="review",
            model="m", provider="anthropic"),
        _ev("3", "step_completed", step="code-reviewer", kind="agent",
            action={"verb": "goto", "target": "task-lead",
                    "resume": False, "rework": True},
            var_mutations={}, label="addressing review feedback"),
        _ev("4", "step_started", step="task-lead", kind="agent", phase="implement",
            model="opus", provider="anthropic",
            route_label="addressing review feedback"),
    ]
    rendered = _lines(events)
    # No "→ addressing review feedback" route line anywhere.
    assert not any(ln.strip().startswith("→ addressing review feedback")
                   for ln in rendered)
    # The reason rides the task-lead header as the yellow suffix (last segment).
    hdr = next(ln for ln in rendered if "task-lead" in ln and "complete" not in ln)
    assert hdr.strip().endswith("· addressing review feedback")


def test_header_line_falls_back_to_name_when_no_phase_or_position() -> None:
    # With no phase_label and position_label=False the header collapses to node.name.
    labeled = nv.StepNode(name="code-reviewer", kind="agent", label="reviewing code")
    assert rt.header_line(labeled, depth=1, color=False) == "  code-reviewer"
    unlabeled = nv.StepNode(name="commit-check", kind="script")
    assert rt.header_line(unlabeled, depth=1, color=False) == "  commit-check"


def test_header_line_appends_gray_resume_marker() -> None:
    # No phase_label, position_label=False: base collapses to node.name.
    node = nv.StepNode(name="code-reviewer", kind="agent",
                       label="reviewing code", model="opus", resumed=True)
    # plain text: marker sits after the name, before the model suffix.
    assert rt.header_line(node, depth=0, color=False) == "code-reviewer (resume) · opus"
    # color: the " (resume)" span is chrome gray (theme-resolved).
    colored = rt.header_line(node, depth=0, color=True)
    assert pal.ansi(" (resume)", "gray") in colored
    # a non-resumed step carries no marker.
    fresh = nv.StepNode(name="code-reviewer", kind="agent",
                        label="reviewing code", model="opus")
    assert "(resume)" not in rt.header_line(fresh, depth=0, color=False)
    # a resumed step with no model has no model tail.
    no_model = nv.StepNode(name="commit-check", kind="script",
                           label="checking commit", resumed=True)
    assert rt.header_line(no_model, depth=0, color=False) == "commit-check (resume)"


def test_caret_escape_maps_control_bytes_to_caret_notation() -> None:
    # cat -v / less convention: c -> "^" + chr(ord(c) ^ 0x40).
    assert rt._caret_escape("\x07") == "^G"   # BEL
    assert rt._caret_escape("\x1b") == "^["    # ESC
    assert rt._caret_escape("\x00") == "^@"    # NUL
    assert rt._caret_escape("\x7f") == "^?"    # DEL
    assert rt._caret_escape("\r") == "^M"      # bare CR rewrites the TTY line
    assert rt._caret_escape("\x08") == "^H"    # BS


def test_caret_escape_preserves_tab_newline_and_printable() -> None:
    # \t and \n are load-bearing for layout — never escaped.
    assert rt._caret_escape("\t") == "\t"
    assert rt._caret_escape("\n") == "\n"
    # Ordinary printable / non-ASCII multibyte text is unchanged.
    assert rt._caret_escape("héllo · 世界") == "héllo · 世界"


def test_caret_escape_only_touches_control_bytes_in_mixed_string() -> None:
    assert rt._caret_escape("a\x07b\x1bc\nd") == "a^Gb^[c\nd"


def test_full_tool_result_and_args_control_bytes_become_caret() -> None:
    out = "\n".join(_lines([
        _ev("2", "step_started", step="s", kind="agent", phase="p",
            model="m", provider="anthropic"),
        _ev("3", "tool_call", agent_id="step:p/s", id="b1", name="bash",
            input={"cmd": "ring\x07now"}),
        _ev("4", "tool_result", agent_id="step:p/s", id="b1",
            content="ring\x07null\x00esc\x1bdone", error=False),
    ], full=True))
    # color=False here, so the ONLY ESC byte that could appear is content ESC.
    assert "\x07" not in out
    assert "\x00" not in out
    assert "\x1b" not in out
    assert "^G" in out and "^@" in out and "^[" in out   # caret notation present


def test_full_tool_result_pyc_marshal_bytes_no_bel() -> None:
    # live-view-attach-replay-style fixture: a tool result carrying .pyc marshal
    # bytes (BEL/NUL/ESC) renders with zero raw BEL.
    marshal = "\x07\x00\x63\x00\x00\x00\x1b\x08"
    out = "\n".join(_lines([
        _ev("2", "step_started", step="s", kind="agent", phase="p",
            model="m", provider="anthropic"),
        _ev("3", "tool_call", agent_id="step:p/s", id="b1", name="grep", input={}),
        _ev("4", "tool_result", agent_id="step:p/s", id="b1",
            content=f"matched cache {marshal} bytecode", error=False),
    ], full=True))
    assert "\x07" not in out


def test_colored_render_keeps_sgr_but_caret_escapes_content() -> None:
    tree = nv.build_tree([
        _ev("2", "step_started", step="s", kind="agent", phase="p",
            model="m", provider="anthropic"),
        _ev("3", "tool_call", agent_id="step:p/s", id="b1", name="bash", input={}),
        _ev("4", "tool_result", agent_id="step:p/s", id="b1",
            content="beep\x07", error=False),
    ])
    out = "\n".join(render_tree(tree, full=True, color=True, width=80))
    assert "\x1b[" in out        # imp's own SGR color codes survive (color not broken)
    assert "\x07" not in out     # content BEL is escaped, not emitted raw
    assert "^G" in out


def test_full_thinking_control_bytes_escaped() -> None:
    out = "\n".join(_lines([
        _ev("2", "step_started", step="s", kind="agent", phase="p",
            model="m", provider="anthropic"),
        _ev("3", "thinking", agent_id="step:p/s", content="mull\x07ing\x00over\x1bit"),
    ], full=True))
    assert "\x07" not in out
    assert "\x00" not in out
    assert "\x1b" not in out
    assert "^G" in out


def test_prose_leaf_control_bytes_escaped() -> None:
    out = "\n".join(_lines([
        _ev("2", "step_started", step="s", kind="agent", phase="p",
            model="m", provider="anthropic"),
        _ev("3", "llm_response", agent_id="step:p/s", provider="anthropic", model="m",
            stop_reason="end_turn", usage={},
            raw={"content": [{"type": "text", "text": "hello\x07 world\x00"}]}),
    ], full=True))
    assert "\x07" not in out
    assert "\x00" not in out
    assert "^G" in out


def test_user_message_control_bytes_escaped() -> None:
    node = nv.UserMessageNode(text="please\x07 run\x00 it", interactive=True)
    out = "\n".join(rt._render(node, depth=1, full=True, color=False, width=80))
    assert "\x07" not in out
    assert "\x00" not in out
    assert "^G" in out


def test_literal_text_node_control_bytes_escaped() -> None:
    node = nv.LiteralTextNode(text="hi\x07 there\x00")
    out = "\n".join(rt._render(node, depth=0, full=True, color=False, width=80))
    assert "\x07" not in out
    assert "\x00" not in out
    assert "^G" in out


def test_error_message_and_detail_control_bytes_escaped() -> None:
    node = nv.ErrorNode(message="boom\x07", category="tool",
                        detail="trace\x00line", is_warning=False)
    out = "\n".join(rt._render(node, depth=0, full=True, color=False, width=80))
    assert "\x07" not in out
    assert "\x00" not in out
    assert "^G" in out and "^@" in out
    assert "error: [tool]" in out          # imp's framing stays intact, unescaped


def test_rename_title_control_bytes_escaped() -> None:
    node = nv.RenameNode(name="my\x07change\x00")
    out = "\n".join(rt._render(node, depth=0, full=True, color=False, width=80))
    assert "\x07" not in out
    assert "\x00" not in out
    assert "^G" in out and "^@" in out
    assert "Set name to" in out             # imp's framing stays intact, unescaped


def test_breadcrumb_extras_control_bytes_escaped() -> None:
    node = nv.BreadcrumbLine(step_name="review", extras=["verdict=pass\x07", "n=3\x00"])
    out = "\n".join(rt._render(node, depth=0, full=True, color=False, width=80))
    assert "\x07" not in out
    assert "\x00" not in out
    assert "^G" in out and "^@" in out


def test_complete_step_prose_field_control_bytes_escaped() -> None:
    out = "\n".join(rt._render_output_args(
        {"outcome": "did\x07 the\x00 thing and then some more text " * 3},
        depth=0, color=False, width=80))
    assert "\x07" not in out
    assert "\x00" not in out
    assert "^G" in out
    assert "^@" in out


def test_subagent_outcome_scalar_and_block_control_bytes_escaped() -> None:
    raw = json.dumps({"verdict": "pass\x07", "detail": "line one\nline\x00 two more"})
    sub = nv.SubagentFold(subagent_name="reviewer", event_id="e1", block_id="b1",
                          nested=nv.NarrativeBuilder())
    sub.outcome = raw
    out = "\n".join(rt.subagent_outcome_lines(sub, depth=0, color=False, width=80))
    assert "\x07" not in out
    assert "\x00" not in out
    assert "^G" in out and "^@" in out
    assert "→" in out


def test_subagent_outcome_nondict_prose_control_bytes_escaped() -> None:
    sub = nv.SubagentFold(subagent_name="reviewer", event_id="e1", block_id="b1",
                          nested=nv.NarrativeBuilder())
    sub.outcome = "dispatch failed\x07 hard\x00"
    out = "\n".join(rt.subagent_outcome_lines(sub, depth=0, color=False, width=80))
    assert "\x07" not in out
    assert "\x00" not in out
    assert "^G" in out
    assert "^@" in out


def test_fallback_payload_values_control_bytes_escaped() -> None:
    node = nv.FallbackLine(event_type="weird_event", payload={"detail": "raw\x07 val\x00"})
    out = "\n".join(rt._render(node, depth=0, full=True, color=False, width=80))
    assert "\x07" not in out
    assert "\x00" not in out
    assert "^G" in out and "^@" in out
    assert "weird_event" in out             # imp-assigned event_type label is intact


def test_step_context_content_control_bytes_escaped() -> None:
    node = nv.StepContextNode(payload={
        "provider": "anthropic", "model": "m", "max_tool_calls": 5,
        "system_prompt": "be helpful\x07 always\x00",
        "tools_schema": [{"name": "bash\x1b"}],
    })
    out = "\n".join(rt._render_step_context(node, depth=0, full=True, color=False,
                                            width=80))
    assert "\x07" not in out
    assert "\x00" not in out
    assert "\x1b" not in out
    assert "^G" in out and "^@" in out and "^[" in out


def test_fmt_seconds_done_suppresses_sub_second_keeps_whole() -> None:
    # Under one second renders nothing (no lying "0s"); one second and up
    # delegates to fmt_seconds (Ns / Mm Ss).
    assert rt.fmt_seconds_done(0) == ""
    assert rt.fmt_seconds_done(0.4) == ""
    assert rt.fmt_seconds_done(0.999) == ""
    assert rt.fmt_seconds_done(None) == ""
    assert rt.fmt_seconds_done(1) == "1s"
    assert rt.fmt_seconds_done(12.4) == "12s"
    assert rt.fmt_seconds_done(336) == "5m 36s"
    # fmt_seconds itself is unchanged: the live clock still shows 0s.
    assert rt.fmt_seconds(0.4) == "0s"


def test_recap_sites_drop_sub_second_duration_and_separator() -> None:
    # 1) phase-complete line: phase duration DERIVES from its child steps'
    # timestamps (PhaseNode carries no start/end of its own); a sub-second span
    # drops " · <secs>" entirely.
    pn = nv.PhaseNode(name="design", closed=True)
    pn.children.append(nv.StepNode(
        name="spec", start_ts="2024-01-01T00:00:00",
        end_ts="2024-01-01T00:00:00.500000"))
    line = rt.phase_breadcrumb_line(pn, depth=0, color=False)
    assert line == "design phase complete"
    assert " · " not in line

    # 2) step breadcrumb: sub-second elapsed drops the duration, no dangling sep.
    bc = nv.BreadcrumbLine(step_name="spec", elapsed_s=0.4,
                           action={"verb": "continue"}, extras=[])
    bc_line = "\n".join(rt._render(bc, depth=1, full=False, color=False, width=80))
    assert bc_line == "  spec · complete"
    assert not bc_line.rstrip().endswith("·")
    # a one-second-plus step still shows its duration.
    bc2 = nv.BreadcrumbLine(step_name="spec", elapsed_s=12.4,
                            action={"verb": "continue"}, extras=[])
    assert "\n".join(rt._render(bc2, depth=1, full=False, color=False, width=80)) \
        == "  spec · complete · 12s"

    # 3) finished subagent (running=False): sub-second run drops " · <secs>".
    sub = nv.SubagentFold(subagent_name="reviewer", event_id="e", block_id="b",
                          nested=nv.NarrativeBuilder(), running=False,
                          start_ts="2024-01-01T00:00:00",
                          end_ts="2024-01-01T00:00:00.500000")
    row = rt.subagent_status_row(sub, depth=0, color=False)
    assert row == "  reviewer"
    assert "·" not in row


def test_opening_intent_renders_user_turn_with_gutter() -> None:
    tree = nv.build_tree([
        {"id": "e1", "agent_id": None, "type": "change_created",
         "payload": {"handle": "h", "title": "t"}},
        {"id": "e2", "agent_id": None, "type": "opening_intent",
         "payload": {"text": "add a flag"}},
    ])
    body = list(rt.render_tree(tree, full=False, color=False, width=80))
    assert any(ln == "> add a flag" for ln in body)   # depth-0, caret-marked human turn


def test_burst_summary_drops_sub_second_duration() -> None:
    burst = nv.ToolBurstFold(
        start_ts="2024-01-01T00:00:00", end_ts="2024-01-01T00:00:00.500000")
    burst.calls.append(
        nv.ToolCall(call_id="b1", name="read_file", input={"p": "1"}))
    summary = rt._burst_summary(burst)
    assert summary == "1 call · read_file"


def test_burst_summary_duration_uses_prose_span_not_tool_span() -> None:
    burst = nv.ToolBurstFold(
        calls=[nv.ToolCall(call_id="b1", name="bash", input={})],
        start_ts="2026-06-03T00:00:05+00:00",
        end_ts="2026-06-03T00:00:06+00:00",          # tool span = 1s
        span_start_ts="2026-06-03T00:00:00+00:00",
        span_end_ts="2026-06-03T00:00:09+00:00",     # prose span = 9s
    )
    # The recap reports the 9s prose → prose span, not the 1s tool span.
    assert rt._burst_summary(burst) == "1 call · bash · 9s"


def test_burst_summary_falls_back_to_tool_span_when_no_prose_span() -> None:
    burst = nv.ToolBurstFold(
        calls=[nv.ToolCall(call_id="b1", name="bash", input={})],
        start_ts="2026-06-03T00:00:00+00:00",
        end_ts="2026-06-03T00:00:04+00:00",          # tool span = 4s
        span_start_ts=None,
        span_end_ts=None,
    )
    # No span boundaries → fall back to the tool span (4s) rather than dropping it.
    assert rt._burst_summary(burst) == "1 call · bash · 4s"


def test_burst_summary_sub_second_prose_span_drops_duration() -> None:
    burst = nv.ToolBurstFold(
        calls=[nv.ToolCall(call_id="b1", name="bash", input={})],
        start_ts="2026-06-03T00:00:00+00:00",
        end_ts="2026-06-03T00:00:05+00:00",                  # tool span 5s (would show if used)
        span_start_ts="2026-06-03T00:00:00+00:00",
        span_end_ts="2026-06-03T00:00:00.500000+00:00",      # prose span 0.5s
    )
    # Sub-second prose span → the fmt_seconds_done < 1s rule drops the suffix.
    assert rt._burst_summary(burst) == "1 call · bash"


def test_burst_summary_falls_back_to_tool_end_when_no_closing_prose() -> None:
    burst = nv.ToolBurstFold(
        calls=[nv.ToolCall(call_id="b1", name="bash", input={})],
        start_ts="2026-06-03T00:00:02+00:00",
        end_ts="2026-06-03T00:00:06+00:00",          # tool span end = 6s from step
        span_start_ts="2026-06-03T00:00:00+00:00",   # step boundary
        span_end_ts=None,                             # no closing prose
    )
    # span_start_ts set, span_end_ts=None → use span_start_ts and fall back to end_ts.
    # Duration = 00:06 - 00:00 = 6s.
    assert rt._burst_summary(burst) == "1 call · bash · 6s"


def test_skill_load_renders_gerund() -> None:
    node = nv.SkillLoadNode(name="refactor", source="human")
    out = list(rt._render(node, depth=1, full=False, color=False, width=80))
    joined = "\n".join(out)
    assert "loading skill refactor" in joined
    assert "load skill refactor" not in joined   # imperative form is gone


def test_route_label_header_suffix_renders_lifecycle_yellow() -> None:
    node = nv.StepNode(name="task-lead", kind="agent", label="implementing the plan",
                       model="opus", route_label="addressing review feedback")
    with unittest.mock.patch.dict(pal.NODE_COLORS, {"lifecycle": ("cyan",)}):
        for theme in ("ansi", "dark", "light"):
            pal.set_theme(theme)
            try:
                hdr = rt.header_line(node, depth=0, color=True)
                assert pal.ansi(" · addressing review feedback", "cyan") in hdr
                assert pal._ANSI["yellow"] not in hdr.split("opus", 1)[-1]
            finally:
                pal.set_theme("ansi")


def test_pause_node_renders_lifecycle_yellow_with_glyph() -> None:
    node = nv.PauseNode(reason="ctrl-c")
    plain = "\n".join(rt._render(node, depth=0, full=False, color=False, width=80))
    assert plain == "paused (ctrl-c)"          # no glyph when color is off
    colored = "\n".join(rt._render(node, depth=0, full=False, color=True, width=80))
    assert "⏸ paused (ctrl-c)" in colored
    assert pal._ANSI["yellow"] in colored
    assert pal._ANSI["dim"] not in colored     # was dim metadata; no longer


def test_pause_node_renders_lifecycle_wordmark_in_all_themes() -> None:
    node = nv.PauseNode(reason="ctrl-c")
    for theme in ("ansi", "dark", "light"):
        pal.set_theme(theme)
        try:
            colored = "\n".join(rt._render(node, depth=0, full=False, color=True, width=80))
            assert pal.sgr("wordmark", enabled=True) in colored
            assert pal._ANSI["dim"] not in colored
        finally:
            pal.set_theme("ansi")


def test_phase_start_rule_is_lifecycle_wordmark() -> None:
    # The phase header is the `── <phase> ───` rule in the lifecycle role (now the
    # wordmark warm accent, non-bold) — not the bare name in bold-blue `phase`.
    # The role resolves per active theme, so assert it across ansi/dark/light.
    for theme in ("ansi", "dark", "light"):
        pal.set_theme(theme)
        try:
            out = list(render_tree(nv.build_tree([
                _ev("1", "change_created", title="t"),
                _ev("2", "step_started", step="spec", kind="agent",
                     phase="design", model="m"),
            ]), full=False, color=True, width=80))
            rule = next(ln for ln in out if "── design phase ───" in ln)
            assert pal.sgr("wordmark", enabled=True) in rule, theme
            assert pal._ANSI["blue"] not in rule, theme   # not the bold-blue phase color
        finally:
            pal.set_theme("ansi")


def test_outcome_line_renders_lifecycle_yellow_not_accent_by_verb() -> None:
    line = next(iter(rt._render(nv.OutcomeLine(action={"verb": "continue"}),
                                depth=0, full=False, color=True, width=80)))
    assert "→ continue" in line
    assert pal._ANSI["yellow"] in line
    # was accent-by-verb: continue resolved to green — that must be gone.
    assert pal._ANSI["green"] not in line


def test_outcome_line_renders_lifecycle_wordmark_in_all_themes() -> None:
    for theme in ("ansi", "dark", "light"):
        pal.set_theme(theme)
        try:
            line = next(iter(rt._render(nv.OutcomeLine(action={"verb": "continue"}),
                                        depth=0, full=False, color=True, width=80)))
            assert pal.sgr("wordmark", enabled=True) in line
            assert pal._ANSI["green"] not in line
        finally:
            pal.set_theme("ansi")


def test_warning_node_renders_warning_wordmark_in_all_themes() -> None:
    node = nv.ErrorNode(message="careful", is_warning=True)
    for theme in ("ansi", "dark", "light"):
        pal.set_theme(theme)
        try:
            colored = "\n".join(rt._render(node, depth=0, full=False, color=True, width=80))
            assert "warning: careful" in colored
            assert pal.sgr("wordmark", enabled=True) in colored
        finally:
            pal.set_theme("ansi")


def test_header_line_appends_route_label_suffix() -> None:
    # No phase_label, position_label=False: base collapses to node.name.
    # Forward-routed header: yellow " · <reason>" rides last, after the model.
    fwd = nv.StepNode(name="task-lead", kind="agent", label="implementing the plan",
                      model="opus", route_label="addressing review feedback")
    assert rt.header_line(fwd, depth=0, color=False) == \
        "task-lead · opus · addressing review feedback"
    # Resume header: " (resume)", then the model, then the reason — always last.
    res = nv.StepNode(name="task-lead", kind="agent", label="implementing the plan",
                      model="opus", resumed=True,
                      route_label="addressing review feedback")
    assert rt.header_line(res, depth=0, color=False) == \
        "task-lead (resume) · opus · addressing review feedback"
    # The reason segment is painted lifecycle (yellow), distinct from the step color.
    colored = rt.header_line(fwd, depth=0, color=True)
    assert pal.ansi(" · addressing review feedback",
                    *pal.NODE_COLORS["lifecycle"]) in colored
    # No route_label → byte-identical to today (no trailing segment).
    bare = nv.StepNode(name="task-lead", kind="agent", label="implementing the plan",
                       model="opus")
    assert rt.header_line(bare, depth=0, color=False) == "task-lead · opus"


def test_phase_end_rule_collapses_when_subsecond() -> None:
    # A sub-second phase drops the `· <elapsed>` suffix entirely: there must be no
    # dangling `· ` before the tail dashes.
    events = [
        {"id": "e0", "agent_id": None, "type": "change_created", "payload": {"title": "t"}},
        {"id": "e1", "agent_id": None, "type": "step_started",
         "payload": {"step": "s1", "kind": "branch", "phase": "spec"},
         "timestamp": "2026-06-03T00:00:00.0+00:00"},
        {"id": "e2", "agent_id": None, "type": "step_completed",
         "payload": {"step": "s1", "kind": "branch", "action": {"verb": "continue"}},
         "timestamp": "2026-06-03T00:00:00.4+00:00"},
        # Starting the plan phase forces the spec phase (0.4s) to close.
        {"id": "e3", "agent_id": None, "type": "step_started",
         "payload": {"step": "s2", "kind": "branch", "phase": "plan"},
         "timestamp": "2026-06-03T00:00:00.4+00:00"},
    ]
    out = list(render_tree(nv.build_tree(events), full=False, color=False, width=80))
    line = next(ln for ln in out if "spec phase complete" in ln)
    assert line == "spec phase complete"


def test_compose_label_joins_present_segments() -> None:
    assert rt.compose_label(
        ["implementation phase · plan 1/3", "finalizing", None]
    ) == "implementation phase · plan 1/3 · finalizing"
    assert rt.compose_label([None, "spec"]) == "spec"
    assert rt.compose_label([None, None]) == ""
    assert rt.compose_label(["", "step"]) == "step"
    assert rt.compose_label(["", None]) == ""


def test_header_line_composes_phase_label_into_step_header() -> None:
    # With position_label=False the verb label drops; only phase_label remains.
    step = nv.StepNode(name="finalize", kind="agent",
                       phase_label="implementation phase · plan 1/3",
                       label="finalizing")
    assert rt.header_line(step, depth=1, color=False) == (
        "  implementation phase · plan 1/3")


def test_phase_divider_uses_phase_label() -> None:
    phase = nv.PhaseNode(name="code", label="implementation phase · plan 1/3")
    assert rt.header_line(phase, depth=0, color=False) == (
        "── implementation phase · plan 1/3 ───")


def test_phase_breadcrumb_uses_phase_label() -> None:
    phase = nv.PhaseNode(name="code", label="implementation phase · plan 1/3")
    assert rt.phase_breadcrumb_line(phase, depth=0, color=False) == (
        "implementation phase · plan 1/3 complete")


def test_opening_intent_renders_under_first_step_header_before_narration() -> None:
    out = _lines([
        _ev("e1", "change_created", title="add auth"),
        {"id": "e2", "agent_id": None, "type": "opening_intent",
         "payload": {"text": "add a --json flag"}},
        _ev("s1", "step_started", step="spec", kind="agent", phase="design",
            model="m", provider="anthropic"),
        _ev("ll", "llm_response", agent_id="step:design/spec", provider="anthropic",
            model="m", stop_reason="end_turn", usage={},
            raw={"content": [{"type": "text", "text": "starting the work"}]}),
    ])
    joined = "\n".join(out)
    assert "> add a --json flag" in joined
    step_idx = next(i for i, ln in enumerate(out) if "spec" in ln)
    intent_idx = next(i for i, ln in enumerate(out) if "add a --json flag" in ln)
    narr_idx = next(i for i, ln in enumerate(out) if "starting the work" in ln)
    assert step_idx < intent_idx < narr_idx


def test_breadcrumb_completion_composes_phase_label() -> None:
    bc = nv.BreadcrumbLine(
        step_name="finalize", kind="agent", elapsed_s=0.0,
        action={"verb": "continue"}, step_label="finalizing",
        phase_label="implementation phase · plan 1/3", show_complete=True)
    out = list(rt._render(bc, depth=1, full=False, color=False, width=80))
    assert out
    assert out[0].strip().startswith(
        "implementation phase · plan 1/3 · finalizing · complete")


def test_breadcrumb_without_phase_label_is_unchanged() -> None:
    bc = nv.BreadcrumbLine(
        step_name="router", kind="branch", elapsed_s=0.0,
        action={"verb": "continue"}, step_label="routing", show_complete=True)
    out = list(rt._render(bc, depth=1, full=False, color=False, width=80))
    assert out and out[0].strip().startswith("routing · complete")


def test_markdown_single_newlines_render_as_separate_lines() -> None:
    # A single "\n" is a markdown-it softbreak (rendered as a space); we rewrite it to hardbreak.
    from imp.cli.render_text import _markdown_to_ansi
    out = _markdown_to_ansi("alpha\nbeta\ngamma", width=40, color=False)
    visible = [ln.rstrip() for ln in out.splitlines() if ln.strip()]
    assert visible == ["alpha", "beta", "gamma"]


def test_markdown_paragraph_breaks_and_lists_unaffected_by_hardbreak() -> None:
    # The rewrite must not disturb paragraph separation or bullet lists.
    from imp.cli.render_text import _markdown_to_ansi
    para = [ln.rstrip() for ln in _markdown_to_ansi("p1\n\np2", 40, False).splitlines()]
    assert para == ["p1", "", "p2"]
    items = [ln.rstrip() for ln in _markdown_to_ansi("- one\n- two", 40, False).splitlines()]
    assert items == ["", " • one", " • two"]


def test_markdown_hr_capped_to_display_width_and_gray() -> None:
    # Console (wrap) width is huge; the HR must still cap at the display width
    # and render as gray ─ identical to palette.rule().
    rendered = rt._markdown_to_ansi("before\n\n---\n\nafter", 10000, True, display_width=24)
    hr_lines = [ln for ln in rendered.splitlines() if "─" in ln]
    assert hr_lines, "expected a horizontal-rule line"
    assert hr_lines[0] == pal.rule(24, enabled=True)


def test_markdown_hr_capped_to_console_width_when_smaller_than_display() -> None:
    # rule_width = min(display_width, console_width): narrow console wins.
    # Test _CappedHR directly so rich's own console-width clipping cannot mask a missing min().
    hr_cls = rt._capped_hr_cls(200)
    hr = hr_cls()

    class _Opts:
        max_width = 30

    (text,) = hr.__rich_console__(None, _Opts())
    assert text.plain == "─" * 30  # min(200, 30); would be "─" * 200 without min()


def test_render_tree_caps_hr_while_prose_stays_unwrapped() -> None:
    # Wrap width is huge (10000) so prose does NOT wrap; display_width is 40 so
    # the markdown HR caps at 40 — the two widths are decoupled.
    long_line = "X" * 200  # no spaces: only wraps if the wrap width forces it
    out = list(render_tree(
        nv.build_tree([
            _ev("2", "step_started", step="s", kind="agent", phase="p",
                model="m", provider="anthropic"),
            _ev("3", "llm_response", agent_id="step:p/s", provider="anthropic",
                model="m", stop_reason="end_turn", usage={},
                raw={"content": [{"type": "text", "text": long_line + "\n\n---\n\nafter"}]}),
        ]),
        full=False, color=False, width=10000, display_width=40))
    assert any(long_line in ln for ln in out)          # prose unwrapped
    assert any(ln.count("─") == 40 for ln in out)      # HR capped to display width
    assert not any(ln.count("─") > 40 for ln in out)


def test_step_header_drops_verb_label_for_verb_step() -> None:
    from imp.cli import narrative as nv
    from imp.cli.render_text import header_line
    node = nv.StepNode(name="plan-writer", kind="agent", label="writing plan",
                       phase_label="planning phase · plan 2/4", position_label=False)
    line = header_line(node, depth=0, color=False)
    assert line.strip() == "planning phase · plan 2/4"
    assert "writing plan" not in line


def test_step_header_keeps_position_label_for_position_step() -> None:
    from imp.cli import narrative as nv
    from imp.cli.render_text import header_line
    node = nv.StepNode(name="task-lead", kind="agent", label="task 2/3",
                       phase_label="implementation phase · plan 1/1",
                       position_label=True)
    line = header_line(node, depth=0, color=False)
    assert line.strip() == "implementation phase · plan 1/1 · task 2/3"


def test_step_header_position_label_without_phase_falls_to_label() -> None:
    from imp.cli import narrative as nv
    from imp.cli.render_text import header_line
    node = nv.StepNode(name="task-lead", kind="agent", label="task 2/3",
                       phase_label=None, position_label=True)
    line = header_line(node, depth=0, color=False)
    assert line.strip() == "task 2/3"
