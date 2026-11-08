# tests/test_render_text.py
from __future__ import annotations

import json
import re as _re
import unittest.mock
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from imp.agents.transcript import ContinuationNote, deliver_continuation_reply
from imp.cli import narrative as nv
from imp.cli import palette as pal
from imp.cli import render_text as rt
from imp.cli.render_text import render_tree
from imp.engine.state import InMemoryEventLog
from tests._event_fixtures import (
    INTERNAL_TOOLS,
    agent_text,
    assert_spec_marker_scrollback,
    ev,
    express_spec_marker_events,
    interrupted_task_lead_events,
    subagent_then_bash_events,
    task_lead_started,
    titled_subagent_events,
)

_MAX_LOOPS_COPY = (
    "a step kept sending work back and hit its loop limit; the quest is "
    "paused and resumable — resume to continue with a fresh loop budget"
)
_CONTEXT_WINDOW_COPY = (
    "a step kept filling its context and used up its handoffs; the quest is "
    "paused and resumable — resume to continue in a fresh session with a "
    "fresh handoff budget"
)


class _FakeRendered:
    def __init__(self, texts): self.texts = texts


def _lines(events, *, full=False, width=80):
    tree = nv.build_tree(events)
    return list(render_tree(tree, full=full, color=False, width=width))


def _ev(eid, etype, agent_id=None, **payload):
    return {"id": eid, "agent_id": agent_id, "type": etype, "payload": payload}


def test_phase_step_outcome_render_without_header() -> None:
    out = "\n".join(_lines([
        _ev("1", "quest_created", title="add auth"),
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
        _ev("3", "agent_message", agent_id="step:p/s", provider="anthropic", model="m",
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
            model="claude", tools_schema=[{"name": "a"}, {"name": "b"}],
            params={}, max_tool_calls=50),
        _ev("tk1", "tool_call", agent_id="step:p/s", id="b1", name="sub_agent",
            input={"subagent_name": "reviewer", "task_description": "go"}),
        _ev("su1", "agent_message", agent_id="sub:tk1", provider="anthropic", model="m",
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
        _ev("su1", "agent_message", agent_id="sub:tk1", provider="anthropic", model="m",
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
        _ev("su1", "agent_message", agent_id="sub:tk1", provider="anthropic", model="m",
            stop_reason="end_turn", usage={},
            raw={"content": [{"type": "text", "text": "the prose"}]}),
        _ev("r1", "tool_result", agent_id="step:p/s", id="b1",
            content='{"status":"Success","summary":"done"}', error=False),
    ]))
    assert "reviewer" in out
    assert "· reviewer" not in out
    assert "the prose" not in out
    assert "status=Success" not in out


def test_subagent_error_renders_failed_status_row() -> None:
    # Part D: a failed subagent surfaces as · failed in lean — NOT the raw
    # error outcome prose (which could leak the SDK sentinel).
    out = list(_lines([
        _ev("2", "step_started", step="s", kind="agent", phase="p",
            model="m", provider="anthropic"),
        _ev("tk1", "tool_call", agent_id="step:p/s", id="b1", name="sub_agent",
            input={"subagent_name": "reviewer", "task_description": "go"}),
        _ev("r1", "tool_result", agent_id="step:p/s", id="b1",
            content="dispatch failed: boom", error=True),
    ], width=80))
    assert any("reviewer · failed" in ln for ln in out)
    assert not any("· reviewer" in ln for ln in out)              # no leading bullet
    assert not any("dispatch failed: boom" in ln for ln in out)   # outcome suppressed
    # With color on, the row renders ANSI; the failed tag's gold is asserted below.
    color_out = list(render_tree(nv.build_tree([
        _ev("2", "step_started", step="s", kind="agent", phase="p",
            model="m", provider="anthropic"),
        _ev("tk1", "tool_call", agent_id="step:p/s", id="b1", name="sub_agent",
            input={"subagent_name": "reviewer", "task_description": "go"}),
        _ev("r1", "tool_result", agent_id="step:p/s", id="b1",
            content="dispatch failed: boom", error=True),
    ]), full=False, color=True, width=80))
    # The failed tag is imp's brand gold (wordmark) when color is on.
    assert any("· failed" in ln and pal.sgr("wordmark", enabled=True) in ln
               for ln in color_out)


def test_full_mode_also_emits_subagent_outcome() -> None:
    # --full must stay a superset of default: it expands thinking/tools AND emits
    # the structured return value (maintainer decision; deviation from the spec's
    # original "--full unchanged").
    out = "\n".join(_lines([
        _ev("2", "step_started", step="s", kind="agent", phase="p",
            model="m", provider="anthropic"),
        _ev("tk1", "tool_call", agent_id="step:p/s", id="b1", name="sub_agent",
            input={"subagent_name": "reviewer", "task_description": "go"}),
        _ev("su1", "agent_message", agent_id="sub:tk1", provider="anthropic", model="m",
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
        _ev("su1", "agent_message", agent_id="sub:tk1", provider="anthropic", model="m",
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
            tools_schema=[{"name": "bash"}], params={"max_tokens": 8192}, max_tool_calls=50),
        _ev("4", "engine_message", agent_id="step:p/s",
            role="system", subtype="system_prompt",
            content=[{"type": "text", "text": "SYSTEM PROMPT TEXT"}]),
    ], full=True))
    assert "system prompt:" in out
    assert "SYSTEM PROMPT TEXT" in out
    assert "- bash" in out


def test_full_mode_shows_the_replay_source_system_prompt() -> None:
    # The system prompt rendered by --full is the engine_message/system_prompt event
    # emitted on each turn — so the displayed prompt is exactly what the model saw.
    stored = "ASSEMBLED SYSTEM PROMPT v1"
    out = "\n".join(_lines([
        _ev("2", "step_started", step="s", kind="agent", phase="p",
            model="m", provider="anthropic"),
        _ev("3", "step_context", agent_id="step:p/s", provider="anthropic",
            model="claude", tools_schema=[{"name": "bash"}],
            params={"max_tokens": 8192}, max_tool_calls=50),
        _ev("4", "engine_message", agent_id="step:p/s",
            role="system", subtype="system_prompt",
            content=[{"type": "text", "text": stored}]),
    ], full=True))
    assert "system prompt:" in out
    assert stored in out


def test_step_context_dropped_from_lean_model_on_step_header() -> None:
    # Lean view: no `▸ context …` line at all — the model rides on the step
    # header (point 1); the system prompt / tool list survive in --full only.
    events = [
        _ev("2", "step_started", step="s", kind="agent", phase="p",
            model="claude-opus-4-7", provider="anthropic"),
        _ev("3", "step_context", agent_id="step:p/s", provider="anthropic",
            model="claude-opus-4-7", tools_schema=[], params={}, max_tool_calls=50),
        _ev("4", "engine_message", agent_id="step:p/s",
            role="system", subtype="system_prompt",
            content=[{"type": "text", "text": "SYSTEM PROMPT TEXT"}]),
    ]
    lean = "\n".join(_lines(events, full=False))
    assert "▸ context" not in lean
    assert "SYSTEM PROMPT TEXT" not in lean
    assert "s · claude-opus-4-7" in lean      # model on the step header
    full = "\n".join(_lines(events, full=True))
    assert "context" in full
    assert "system prompt:" in full
    assert "SYSTEM PROMPT TEXT" in full


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
    # Engine-sent kickoff (engine_message/agent_prompt) is hidden; only a
    # human reply the engine awaited renders as a gutter turn (user_message/reply).
    out = "\n".join(_lines([
        _ev("1", "step_started", step="s", kind="agent", phase="p", model="m"),
        _ev("2", "engine_message", agent_id="step:p/s",
            role="user", subtype="agent_prompt",
            content=[{"type": "text", "text": "begin"}]),
        _ev("3", "awaiting_user_input", agent_id="step:p/s"),
        _ev("4", "user_message", agent_id="step:p/s",
            role="user", subtype="reply",
            content=[{"type": "text", "text": "do the real thing"}]),
    ]))
    assert "begin" not in out
    assert "> do the real thing" in out   # caret-marked human turn at column 0


def test_interjected_user_message_renders_as_human_turn() -> None:
    # A human INTERJECT is delivered unprompted while the agent runs: the engine
    # emits a user_message/interject event — with NO preceding awaiting_user_input.
    # It must render as a genuine human turn (gutter).
    events = [
        _ev("1", "step_started", step="s", kind="agent", phase="p", model="m"),
        _ev("2", "agent_message", agent_id="step:p/s", provider="anthropic",
            model="m", stop_reason="end_turn", usage={},
            raw={"content": [{"type": "text", "text": "working on it"}]}),
        _ev("3", "user_message", agent_id="step:p/s",
            role="user", subtype="interject",
            content=[{"type": "text", "text": "actually use postgres"}]),
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
        _ev("5", "step_started", step="s", kind="agent", phase="p", model="m",
            restarted=True),
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


def test_chrome_step_collapses_to_single_line() -> None:
    out = _lines([
        _ev("1", "quest_created", title="t"),
        _ev("2", "step_started", step="spec-verify-commit", kind="script", phase="spec"),
        _ev("3", "step_completed", step="spec-verify-commit", kind="script",
            action={"verb": "continue"}),
    ])
    hits = [ln for ln in out if "spec-verify-commit" in ln]
    assert hits == ["spec-verify-commit · complete"]   # header+breadcrumb collapsed


def test_open_chrome_step_shows_header_then_collapses_on_complete() -> None:
    open_out = _lines([
        _ev("1", "quest_created", title="t"),
        _ev("2", "step_started", step="run-tests", kind="script", phase="implement"),
    ])
    open_hits = [ln for ln in open_out if "run-tests" in ln]
    assert len(open_hits) == 1
    assert "complete" not in open_hits[0]              # in-flight, not "complete"
    colored = list(render_tree(nv.build_tree([
        _ev("1", "quest_created", title="t"),
        _ev("2", "step_started", step="run-tests", kind="script", phase="implement"),
    ]), full=False, color=True, width=80))
    assert pal._ANSI["step"] in next(ln for ln in colored if "run-tests" in ln)
    done_out = _lines([
        _ev("1", "quest_created", title="t"),
        _ev("2", "step_started", step="run-tests", kind="script", phase="implement"),
        _ev("3", "step_completed", step="run-tests", kind="script",
            action={"verb": "continue"}),
    ])
    done_hits = [ln for ln in done_out if "run-tests" in ln]
    assert done_hits == ["run-tests · complete"]       # collapsed to one line


def test_ansi_step_header_is_bold_bright_blue() -> None:
    pal.set_theme("ansi")
    colored = list(render_tree(nv.build_tree([
        _ev("1", "quest_created", title="t"),
        _ev("2", "step_started", step="run-tests", kind="script", phase="implement"),
    ]), full=False, color=True, width=80))
    header = next(ln for ln in colored if "run-tests" in ln)
    assert "\x1b[1m\x1b[94m" in header
    assert not any("\x1b[34m" in ln for ln in colored)


def test_step_has_body_ignores_only_the_trailing_breadcrumb() -> None:
    node = nv.StepNode(name="s", kind="script")
    assert nv.step_has_body(node) is False
    node.children.append(nv.BreadcrumbLine(step_name="s", elapsed_s=0.0,
                                           action={"verb": "continue"}))
    assert nv.step_has_body(node) is False           # a breadcrumb is not a body
    node.children.append(nv.LiteralTextNode(text="hi"))
    assert nv.step_has_body(node) is True


def test_deferred_leaf_holds_empty_script_not_bodied_or_agent() -> None:
    empty = nv.StepNode(name="verify", kind="script", show_label=True)
    assert nv.deferred_leaf(empty) is True            # body-less script → collapsible
    empty.children.append(nv.LiteralTextNode(text="surfaced output"))
    assert nv.deferred_leaf(empty) is False           # a real body → not collapsed
    agent = nv.StepNode(name="spec", kind="agent", show_label=True)
    assert nv.deferred_leaf(agent) is False           # agents always emit a body


def test_deferred_leaf_keeps_hidden_body_emitting_step_atomic() -> None:
    # A HIDDEN step that emits an error body must STAY a leaf (body-insensitive) so
    # the live driver renders it atomically at close — reveal-on-pause == imp log.
    node = nv.StepNode(name="merge-guard", kind="script", show_label=False)
    assert nv.deferred_leaf(node) is True
    node.children.append(nv.ErrorNode(message="boom", category="script"))
    assert nv.deferred_leaf(node) is True             # error body does NOT unhold it


def test_collapsed_step_line_two_tone_label_and_gray_tail() -> None:
    node = nv.StepNode(name="verify", kind="script", label="running tests",
                       show_label=True, closed=True,
                       start_ts="2026-06-10T00:00:00", end_ts="2026-06-10T00:00:05")
    assert rt.collapsed_step_line(node, depth=1, color=False) == \
        "  running tests · complete · 5s"
    colored = rt.collapsed_step_line(node, depth=1, color=True)
    assert pal._ANSI["step"] in colored               # label in step color
    assert pal._ANSI["gray"] in colored               # tail in breadcrumb gray


def test_collapsed_step_line_prepends_phase_label() -> None:
    node = nv.StepNode(name="verify", kind="script", label="running tests",
                       phase_label="integration phase · plan 1/1",
                       show_label=True, closed=True,
                       start_ts="2026-06-10T00:00:00", end_ts="2026-06-10T00:00:05")
    assert rt.collapsed_step_line(node, depth=1, color=False) == \
        "  integration phase · plan 1/1 · running tests · complete · 5s"
    colored = rt.collapsed_step_line(node, depth=1, color=True)
    assert pal._ANSI["step"] in colored   # phase · label portion in step color
    assert pal._ANSI["gray"] in colored   # ` · complete · 5s` tail in breadcrumb gray


def test_body_less_visible_step_collapses_to_one_line() -> None:
    out = _lines([
        _ev("1", "quest_created", title="t"),
        _ev("2", "step_started", step="verify", kind="script", phase="implement",
            label="running tests"),
        _ev("3", "step_completed", step="verify", kind="script",
            action={"verb": "continue"}),
    ])
    hits = [ln for ln in out if "running tests" in ln]
    assert hits == ["running tests · complete"]       # header+breadcrumb collapsed


def test_visible_step_with_body_renders_header_not_collapsed() -> None:
    out = _lines([
        _ev("1", "quest_created", title="t"),
        _ev("2", "step_started", step="plan-finalized", kind="script", phase="plan",
            label="finalizing plan", show_output=True),
        _ev("3", "script_output", step="plan-finalized", stdout="plan 1/2 · 3 tasks"),
        _ev("4", "step_completed", step="plan-finalized", kind="script",
            action={"verb": "continue"}),
    ])
    joined = "\n".join(out)
    assert "plan 1/2 · 3 tasks" in joined              # body survives
    assert "finalizing plan · complete" in joined      # breadcrumb, not a collapsed line
    hits = [ln for ln in out if "finalizing plan" in ln]
    assert len(hits) >= 2                              # header line + breadcrumb line


def test_hidden_spec_written_marker_precedes_planning_rule() -> None:
    assert_spec_marker_scrollback(_lines(express_spec_marker_events()))


def test_visible_show_output_false_step_with_error_body_renders_full_frame() -> None:
    # Exercises the `not step_has_body` disjunct of deferred_leaf: a script step
    # with show_output=False (step_no_body=True) gains a body via an ErrorNode child,
    # so deferred_leaf flips False and the full frame (header + body + breadcrumb) renders.
    out = _lines([
        _ev("1", "quest_created", title="t"),
        _ev("2", "step_started", step="verify-build", kind="script", phase="implement",
            label="verifying build"),
        _ev("3", "error", message="build failed: missing dependency", category="runner"),
        _ev("4", "step_completed", step="verify-build", kind="script",
            action={"verb": "continue"}),
    ])
    joined = "\n".join(out)
    assert "build failed: missing dependency" in joined  # error body rendered
    assert "verifying build · complete" in joined        # breadcrumb present
    hits = [ln for ln in out if "verifying build" in ln]
    assert len(hits) >= 2                                # header line + breadcrumb line


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
        nv.FallbackLine(event_type="quest_merged", payload={"id": "s"}),
        depth=0, full=True, color=True, width=80)))
    assert pal._ANSI["gray"] in fallback


def test_fallback_line_dropped_in_lean_mode() -> None:
    # Engine bookkeeping events with no curated rendering (vars_backfilled,
    # workflow_refreshed, …) are noise in the lean/live view; --full keeps them.
    node = nv.FallbackLine(event_type="vars_backfilled",
                           payload={"names": ["auto_merge"]})
    assert list(rt._render(node, depth=0, full=False, color=False, width=80)) == []
    assert list(rt._render(node, depth=0, full=True, color=False, width=80)) != []


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
        _ev("2", "agent_message", agent_id="a",
            raw={"content": [{"type": "text", "text": "First prose turn."}]}, usage={}),
        _ev("3", "thinking", agent_id="a", content="hmm"),
        _ev("4", "agent_message", agent_id="a",   # thinking-only → no AgentTextNode
            raw={"content": [{"type": "thinking", "thinking": "hmm"}]}, usage={}),
        _ev("5", "agent_message", agent_id="a",
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
        _ev("2", "agent_message", agent_id="a",
            raw={"content": [{"type": "text", "text": "Only prose."}]}, usage={}),
    ])
    idx = next(i for i, ln in enumerate(out) if "Only prose." in ln)
    assert out[idx - 1].strip() == "", "one blank separates the header from prose"
    assert out[idx - 2].strip() != "", "the step header sits just above that blank"


def test_tiers_resolved_attaches_at_root() -> None:
    # Fired before any step_started (agent_id=None) → lands at the workflow root.
    b = nv.NarrativeBuilder()
    b.add({"id": "e1", "agent_id": None, "type": "quest_created",
           "payload": {"quest_id": 1, "title": "t"}})
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
         "differ_tier": None, "differ_from": None}]


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
        _ev("su1", "agent_message", agent_id="sub:tk1", provider="anthropic", model="m",
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
        _ev("1", "quest_created", title="t"),
        _ev("2", "step_started", step="spec", kind="agent", phase="design", model="m"),
        _ev("3", "agent_message", agent_id="a",
            raw={"content": [{"type": "text", "text": "hi"}]}, usage={}),
    ])
    header = next(ln for ln in out if "spec · m" in ln)
    assert header == "spec · m"                 # column 0, no leading indent


def test_subagent_row_de_indents_to_column_two() -> None:
    # The lean subagent status row shifts left with everything else: its own 2-col
    # label offset now lands it at column 2 (was column 4 = phase + step + label).
    out = _lines([
        _ev("1", "quest_created", title="t"),
        _ev("2", "step_started", step="spec", kind="agent", phase="design", model="m"),
        _ev("3", "tool_call", agent_id="a", id="t1", name="sub_agent",
            input={"subagent_name": "reviewer"}),
        _ev("4", "agent_message", agent_id="sub:3",
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
    tree = nv.build_tree([
        _ev("s", "step_started", step="s", kind="agent", phase="p"),
        _ev("e1", "tool_call", agent_id="step:p/s", id="b1", id_ns="block",
            name="sub_agent", input={"subagent_name": "outer"}),
        _ev("e2", "tool_call", agent_id="sub:e1", id="b2", id_ns="block",
            name="sub_agent", input={"subagent_name": "inner"}),
        _ev("deep", "agent_message", agent_id="sub:e2", provider="codex", usage={},
            raw={"content": [{"type": "text", "text": "deep finding"}]}),
        _ev("r2", "tool_result", agent_id="sub:e1", id="b2", id_ns="block",
            content="inner done", error=False),
        _ev("r1", "tool_result", agent_id="step:p/s", id="b1", id_ns="block",
            content="outer done", error=False),
    ])
    step = tree.root.children[0].children[0]
    outer = next(c for c in step.children if isinstance(c, nv.SubagentFold))
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
        {"id": "e0", "agent_id": None, "type": "quest_created",
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


def _color_off_prose(md: str) -> str:
    return "\n".join(rt._render_markdown_block(rt._caret_escape_prose(md), 60, 0, False))


def test_inline_code_color_off_keeps_backticks() -> None:
    assert "Try `inline code` here." in _color_off_prose("Try `inline code` here.")


def test_inline_code_color_off_keeps_a_double_backtick_run() -> None:
    assert "``a `b` c``" in _color_off_prose("``a `b` c``")


def test_inline_code_color_off_pads_content_edged_with_a_backtick() -> None:
    assert "`` `x` ``" in _color_off_prose("`` `x` ``")
    assert "`` `x ``" in _color_off_prose("`` `x ``")
    assert "`` x` ``" in _color_off_prose("`` x` ``")


def test_inline_code_color_off_keeps_backticks_in_table_heading_and_link() -> None:
    assert "`cell`" in _color_off_prose("| A |\n|---|\n| `cell` |")
    assert "Head `h` x" in _color_off_prose("# Head `h` x")
    assert "see `lnk` now" in _color_off_prose("see [`lnk`](https://example.com) now")


def test_inline_code_color_off_leaves_code_block_bodies_undelimited() -> None:
    fenced = _color_off_prose("```\nfence body\n```")
    assert "fence body" in fenced and "`" not in fenced
    indented = _color_off_prose("para\n\n    indented body\n")
    assert "indented body" in indented and "`" not in indented


def test_inline_code_color_off_splices_a_colored_sample_inside_the_backticks() -> None:
    out = _color_off_prose("g `[1;33m78%[0m` a")
    assert "g `78%` a" in out
    assert "\x1b" not in out
    assert "[1;33m" not in out
    assert rt._SPAN_UNIT not in out


def test_inline_code_color_on_has_no_backticks() -> None:
    out = _visible("\n".join(rt._render_markdown_block("Try `inline code` here.", 40, 0, True)))
    assert "inline code" in out
    assert "`" not in out


def test_inline_code_color_off_question_block_keeps_backticks() -> None:
    lines = rt.render_question_block("Try `inline code` here.", 60, 0, False)
    assert "Try `inline code` here." in "\n".join(lines)


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
        _ev("1", "quest_created", title="t"),
        _ev("2", "step_started", step="spec", kind="agent", phase="design",
            model="m", provider="anthropic"),
        _ev("3", "agent_message", agent_id="a",
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
        _ev("1", "quest_created", title="t"),
        _ev("2", "step_started", step="spec", kind="agent", phase="design",
            model="m", provider="anthropic"),
        _ev("3", "agent_message", agent_id="a",
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


def test_user_message_bold_emphasis_ansi_color_on() -> None:
    """Under the `ansi` 16-color theme the scrolled-back user turn is bold
    default-fg text (no reverse-video, no band): a brand-orange `>` caret sits
    outside the emphasis, then one unpainted separating space precedes \\x1b[1m,
    which covers only the body and closes at text end. Continuation lines drop
    the caret (blank column) but stay bold."""
    pal.set_theme("ansi")
    node = nv.UserMessageNode(text="line one\nline two", interactive=True)
    out = list(rt._render(node, depth=1, full=False, color=True, width=80))
    assert len(out) == 2
    assert out[0] == "  \x1b[33m>\x1b[0m \x1b[1mline one\x1b[0m"
    assert out[1] == "    \x1b[1mline two\x1b[0m"
    for line in out:
        assert "\x1b[7m" not in line                  # no reverse-video
        assert "\x1b[48;" not in line                 # no background band under ansi


def test_user_message_caret_with_subtle_bg_in_truecolor_theme() -> None:
    """The reprinted (scrolled-back) user turn carries a subtle entered-input
    background in a truecolor theme, opened AFTER the caret and its separating
    space: the `>` and the one separating space both sit outside the band; only
    the text sits on it, and it closes at text end (no full-width fill → clean
    copy/paste)."""
    pal.set_theme("dark")
    try:
        node = nv.UserMessageNode(text="hello", interactive=True)
        out = list(rt._render(node, depth=0, full=False, color=True, width=80))
    finally:
        pal.set_theme("ansi")
    assert len(out) == 1
    caret = "\x1b[38;2;255;154;60m>\x1b[0m"   # truecolor brand-orange caret
    bg = "\x1b[48;2;67;56;40m"                 # warm amber band (#433828) after the caret
    assert out[0] == caret + " " + bg + "hello\x1b[0m"   # space unpainted, band hugs the text


def test_user_message_multiline_continuation_carries_slate_no_caret() -> None:
    """A multi-line truecolor turn: the continuation line drops the caret (blank
    column in its place) but still carries the warm band, aligned under the body."""
    pal.set_theme("dark")
    try:
        node = nv.UserMessageNode(text="one\ntwo", interactive=True)
        out = list(rt._render(node, depth=0, full=False, color=True, width=80))
    finally:
        pal.set_theme("ansi")
    assert len(out) == 2
    caret = "\x1b[38;2;255;154;60m>\x1b[0m"
    bg = "\x1b[48;2;67;56;40m"
    assert out[0] == caret + " " + bg + "one\x1b[0m"   # caret + unpainted space, band at text
    assert out[1] == "  " + bg + "two\x1b[0m"          # blank caret col + unpainted space


def test_user_message_band_left_edge_hugs_text_both_lines() -> None:
    """The truecolor band opens at the FIRST character of the text on both the
    first line (after the `>` caret) and continuation lines (after the blank
    caret column): the one separating space stays present but sits OUTSIDE the
    band (unpainted), matching the already-tight right edge."""
    pal.set_theme("dark")
    try:
        node = nv.UserMessageNode(text="hi\nyo", interactive=True)
        out = list(rt._render(node, depth=0, full=False, color=True, width=80))
    finally:
        pal.set_theme("ansi")
    assert len(out) == 2
    caret = "\x1b[38;2;255;154;60m>\x1b[0m"
    bg = "\x1b[48;2;67;56;40m"
    assert out[0] == caret + " " + bg + "hi\x1b[0m"   # first line: caret + space, band at text
    assert out[1] == "  " + bg + "yo\x1b[0m"          # continuation: blank caret + space
    # the separating space is never painted (band never opens before a space)
    assert bg + " " not in out[0] and bg + " " not in out[1]


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

    def row(*, color=False, running=False, **kw):
        f = nv.SubagentFold(subagent_name="s", event_id="e", block_id="b",
                            nested=nv.NarrativeBuilder(), running=running)
        for k, v in kw.items():
            setattr(f, k, v)
        return subagent_status_row(f, depth=0, color=color, gutters=())

    # Clean success carries NO tag (it read as noise); only running + the three
    # abnormal terminal states + the generic error keep a tag. New format: the tag
    # is LAST and dot-separated (no parentheses). No leading bullet; the name is
    # indented two cols under the agent turn.
    assert row() == "  s"
    assert "(" not in row()
    assert row(running=True) == "  s · running"
    assert row(is_error=True) == "  s · failed"
    assert row(is_error=True, terminal_state="stalled") == "  s · stalled"
    assert row(is_error=True, terminal_state="interrupted") == "  s · interrupted"
    assert row(is_error=True, terminal_state="context_exhausted") \
        == "  s · context exhausted"
    # No parentheses survive on any tag.
    assert "(" not in row(running=True)
    assert "(" not in row(is_error=True, terminal_state="stalled")

    # Per-tag colors: running is subagent magenta; the four error/warning tags are
    # imp's brand gold (wordmark).
    sub_c = pal.NODE_COLORS["subagent"]
    assert pal.ansi(" · running", *sub_c) in row(color=True, running=True)
    assert pal.ansi(" · stalled", "wordmark") \
        in row(color=True, is_error=True, terminal_state="stalled")
    assert pal.ansi(" · interrupted", "wordmark") \
        in row(color=True, is_error=True, terminal_state="interrupted")
    assert pal.ansi(" · context exhausted", "wordmark") \
        in row(color=True, is_error=True, terminal_state="context_exhausted")
    assert pal.ansi(" · failed", "wordmark") in row(color=True, is_error=True)


def test_subagent_status_row_tag_follows_elapsed() -> None:
    # The abnormal tag renders LAST — after the frozen elapsed suffix, not before
    # it. This is the crux of the reformat (old layout put the tag mid-row).
    from imp.cli import narrative as nv
    from imp.cli.render_text import subagent_status_row

    fold = nv.SubagentFold(subagent_name="reviewer", event_id="e", block_id="b",
                           nested=nv.NarrativeBuilder(), running=False,
                           is_error=True, terminal_state="stalled",
                           start_ts="2026-01-01T00:00:00",
                           end_ts="2026-01-01T00:00:42")
    assert subagent_status_row(fold, depth=0, color=False) \
        == "  reviewer · 42s · stalled"


def test_subagent_status_row_suspended_tag() -> None:
    from imp.cli import narrative as nv
    from imp.cli.render_text import subagent_status_row

    def row(color=False, **kw):
        f = nv.SubagentFold(subagent_name="s", event_id="e", block_id="b",
                            nested=nv.NarrativeBuilder(), running=False)
        for k, v in kw.items():
            setattr(f, k, v)
        return subagent_status_row(f, depth=0, color=color, gutters=())

    assert row(is_error=True, terminal_state="suspended") == "  s · suspended"
    assert "(" not in row(is_error=True, terminal_state="suspended")
    assert pal.ansi(" · suspended", "wordmark") \
        in row(color=True, is_error=True, terminal_state="suspended")


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
    assert any("· stalled" in ln for ln in lines)


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


def _legacy_interrupt_events(*, kill_after_reply: bool):
    a = "step:p/s"

    def t(s):
        return f"2026-11-01T00:00:{s:02d}+00:00"

    def at(e, s):
        return {**e, "timestamp": t(s)}

    kill = at(_ev("k", "tool_killed", agent_id=a, id="", name="bash", signal=15), 20)
    reply = at(_ev("u", "user_message", agent_id=a, role="user",
                   content=[{"type": "text", "text": "ok, carry on"}]), 20)
    return [
        at(_ev("1", "step_started", step="s", kind="agent", phase="p",
               model="m", provider="claude"), 0),
        at(_ev("2", "agent_message", agent_id=a, provider="claude", model="m",
               stop_reason="tool_use", usage={},
               raw={"content": [{"type": "text", "text": "I'll run that command now."}]}), 1),
        at(_ev("3", "tool_call", agent_id=a, id="b1", name="bash",
               input={"command": "sleep 45 && echo finished"}), 1),
        at(_ev("4", "tool_result", agent_id=a, id="b1",
               content='{"returncode": -15, "stdout": ""}', error=False), 6),
        *([reply, kill] if kill_after_reply else [kill, reply]),
        at(_ev("6", "agent_message", agent_id=a, provider="claude", model="m",
               stop_reason="end_turn", usage={},
               raw={"content": [{"type": "text", "text": "The command was cut off."}]}), 22),
    ]


def test_legacy_empty_id_kill_after_reply_renders_no_phantom_recap() -> None:
    events = _legacy_interrupt_events(kill_after_reply=True)
    lean = _lines(events)
    reply = lean.index("> ok, carry on")
    prose = next(i for i, ln in enumerate(lean) if "The command was cut off." in ln)
    assert "  1 call · bash · 5s" in lean[:reply]
    assert not any(" call" in ln for ln in lean[reply + 1:prose])
    full = "\n".join(_lines(events, full=True))
    assert full.count("▾ tools") == 1
    assert "[killed]" not in full


def test_legacy_empty_id_kill_in_burst_does_not_inflate_count() -> None:
    lean = "\n".join(_lines(_legacy_interrupt_events(kill_after_reply=False)))
    assert "1 call · bash · 5s" in lean
    assert "2 calls" not in lean


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
    assert "paused (ctrl-c)" not in out
    assert "⏸" not in out


def test_manual_stop_reasons_draw_no_pause_node_but_keep_digest() -> None:
    for reason in ("ctrl-c", "ctrl-d", "sigterm", "sighup", "sigquit", "pause"):
        tree = nv.build_tree([
            _ev("1", "step_started", step="build", kind="agent", phase="implement",
                model="m", provider="anthropic"),
            _ev("2", "runner_warning", message="dropped block"),
            _ev("3", "step_completed", step="build", kind="agent",
                action={"verb": "pause"}, var_mutations={}),
            _ev("4", "workflow_paused", reason="interrupt", interrupt_reason=reason),
        ])
        out = "\n".join(render_tree(tree, full=False, color=False, width=80))
        assert f"paused ({reason})" not in out
        assert "⏸" not in out
        assert tree.root.status == "paused"
        assert "1 warning this run" in out


def test_non_interrupt_pause_reason_still_renders_line() -> None:
    tree = nv.build_tree([
        _ev("1", "step_started", step="build", kind="agent", phase="implement",
            model="m", provider="anthropic"),
        _ev("2", "workflow_paused", reason="max_loops"),
    ])
    out = "\n".join(render_tree(tree, full=False, color=False, width=200))
    assert f"paused — {_MAX_LOOPS_COPY}" in out


def test_context_window_pause_renders_plain_words_copy() -> None:
    node = nv.PauseNode(reason="context_window")
    plain = "\n".join(rt._render(node, depth=0, full=False, color=False, width=300))
    assert plain == f"paused — {_CONTEXT_WINDOW_COPY}"
    assert "context_window" not in plain


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
    node = nv.MergedNode(quest_id="2026-06-12-run-x", base="master", commit=_SHA_M)
    plain = "\n".join(rt._render(node, depth=0, full=False, color=False, width=80))
    assert plain == "Merged 2026-06-12-run-x into master as 83e4cf23."
    colored = "\n".join(rt._render(node, depth=0, full=False, color=True, width=80))
    assert "✓ Merged 2026-06-12-run-x into master as 83e4cf23." in colored
    assert pal._ANSI["yellow"] in colored
    assert pal._ANSI["green"] not in colored


def test_merged_node_renders_lifecycle_wordmark_in_all_themes() -> None:
    # the merge footer carries the theme's wordmark SGR (the warm accent, not green)
    node = nv.MergedNode(quest_id="2026-06-12-run-x", base="master", commit=_SHA_M)
    for theme in ("ansi", "dark", "light"):
        pal.set_theme(theme)
        try:
            colored = "\n".join(rt._render(node, depth=0, full=False, color=True, width=80))
            assert pal.sgr("wordmark", enabled=True) in colored
            assert pal._ANSI["green"] not in colored
        finally:
            pal.set_theme("ansi")


_SHA_A = "4091ab6d1f2e3d4c5b6a7988990011223344ffee"
_SHA_B = "832c04d7aabbccddeeff00112233445566778899"
_SHA_M = "83e4cf2345a1b2c3d4e5f60718293a4b5c6d7e8f"


def _footer(node, *, color=False):
    return list(rt._render(node, depth=0, full=False, color=color, width=80))


def test_merged_footer_lists_the_commits_indented_two_spaces() -> None:
    node = nv.MergedNode(
        quest_id="2026-06-12-run-x", base="master", commit=_SHA_M,
        commits=[{"sha": _SHA_A, "subject": "cli: derive the quest's commits"},
                 {"sha": _SHA_B, "subject": "cli: print the commit list"}])
    # the log's house style for a multi-line node: body indented two spaces
    assert _footer(node) == [
        "Merged 2026-06-12-run-x into master as 83e4cf23.",
        "  4091ab6d  cli: derive the quest's commits",
        "  832c04d7  cli: print the commit list",
    ]


def test_completed_footer_lists_the_same_body() -> None:
    node = nv.CompletedNode(quest_id="2026-06-12-run-x",
                            commits=[{"sha": _SHA_A, "subject": "only"}])
    assert _footer(node) == ["Completed 2026-06-12-run-x.", "  4091ab6d  only"]


def test_commit_line_colors_the_sha_and_leaves_the_subject_plain() -> None:
    node = nv.MergedNode(quest_id="q", base="b", commit=_SHA_M,
                         commits=[{"sha": _SHA_A, "subject": "plain subject"}])
    lines = _footer(node, color=True)
    assert lines[1] == "  " + pal.ansi("4091ab6d", "gray") + "  plain subject"
    # the footer's lifecycle color must not bleed onto the body
    assert pal.sgr("wordmark", enabled=True) in lines[0]
    assert pal.sgr("wordmark", enabled=True) not in lines[1]


def test_commit_subject_is_caret_escaped_in_the_footer() -> None:
    """A commit subject is content-origin text: it must not reach the terminal
    able to clear the screen or beep."""
    node = nv.MergedNode(quest_id="q", base="b", commit=_SHA_M,
                         commits=[{"sha": _SHA_A, "subject": "boom\x1b[2Jwipe\x07"}])
    lines = _footer(node)
    assert lines[1] == "  4091ab6d  boom^[[2Jwipe^G"
    assert "\x1b" not in lines[1]
    assert "\x07" not in lines[1]


def test_footers_with_no_commits_render_the_single_pre_change_line() -> None:
    """Absent and [] are one code path: both print the headline alone."""
    assert _footer(nv.MergedNode(quest_id="q", base="b", commit=_SHA_M)) == [
        "Merged q into b as 83e4cf23."]
    assert _footer(nv.MergedNode(quest_id="q", base="b", commit=_SHA_M, commits=[])) == [
        "Merged q into b as 83e4cf23."]
    assert _footer(nv.CompletedNode(quest_id="q")) == ["Completed q."]
    assert _footer(nv.CompletedNode(quest_id="q", commits=[])) == ["Completed q."]


def test_recorded_commits_reach_the_footer_through_build_tree() -> None:
    lines = _lines([
        _ev("0", "quest_created", id="runx01", title="Run X"),
        _ev("1", "quest_merged", base="master", commit="f" * 40,
            commits=[{"sha": _SHA_B, "subject": "test: cover both finish paths"}]),
    ])
    assert "Merged runx01 into master as ffffffff." in lines
    assert "  832c04d7  test: cover both finish paths" in lines


def test_complete_step_findings_hidden_lean_visible_full() -> None:
    events = [
        _ev("1", "step_started", step="rev", kind="agent", phase="review",
            model="m", provider="anthropic"),
        _ev("2", "agent_result", agent_id="step:review/rev",
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
        _ev("ao", "agent_result", agent_id="step:review/review",
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
    assert "· interrupted" in out
    assert "· failed" not in out


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
    assert "· interrupted" in out
    assert "· failed" not in out


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
    assert "· failed" in out          # the abnormal first close still reads · failed
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
        _ev("1", "quest_created", quest_id=1, title="x"),
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


def test_header_line_shows_step_label_else_raw_name() -> None:
    # A1: the header shows the step's OWN label (phase name rides the phase rule).
    labeled = nv.StepNode(name="code-reviewer", kind="agent", label="reviewing code")
    assert rt.header_line(labeled, depth=1, color=False) == "  reviewing code"
    # A2: a visible step with no label falls back — loudly — to its raw slug.
    unlabeled = nv.StepNode(name="commit-check", kind="script")
    assert rt.header_line(unlabeled, depth=1, color=False) == "  commit-check"


def test_header_line_appends_gray_resume_marker() -> None:
    node = nv.StepNode(name="code-reviewer", kind="agent",
                       label="reviewing code", model="opus", resumed=True)
    assert rt.header_line(node, depth=0, color=False) == "reviewing code (resume) · opus"
    colored = rt.header_line(node, depth=0, color=True)
    assert pal.ansi(" (resume)", "gray") in colored
    fresh = nv.StepNode(name="code-reviewer", kind="agent",
                        label="reviewing code", model="opus")
    assert "(resume)" not in rt.header_line(fresh, depth=0, color=False)
    # a resumed step with no model has no model tail (label shown, not raw name).
    no_model = nv.StepNode(name="commit-check", kind="script",
                           label="checking commit", resumed=True)
    assert rt.header_line(no_model, depth=0, color=False) == "checking commit (resume)"


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


def test_fence_spans_finds_backtick_tilde_and_unclosed_blocks() -> None:
    lines = ["a", "```", "x", "```", "b", "~~~~", "y", "~~~~", "c", "```", "z"]
    # (opening-delimiter index, exclusive end of content, exclusive end of block);
    # the last fence is unclosed and runs to end of text.
    assert rt._fence_spans(lines) == [
        rt._Fence(1, 3, 4), rt._Fence(5, 7, 8), rt._Fence(9, 11, 11)]


def test_fence_spans_ignores_indented_code_and_mismatched_closers() -> None:
    # A four-space indented code block is not a fence.
    assert rt._fence_spans(["    ```", "x", "    ```"]) == []
    # Three spaces still opens one.
    assert rt._fence_spans(["   ```", "x", "   ```"]) == [rt._Fence(0, 2, 3)]
    # A shorter run of the same char does not close a longer opener.
    assert rt._fence_spans(["````", "```", "x", "````"]) == [rt._Fence(0, 3, 4)]
    # A tilde run does not close a backtick fence.
    assert rt._fence_spans(["```", "~~~", "x"]) == [rt._Fence(0, 3, 3)]
    # A closer may be indented independently of the opener.
    assert rt._fence_spans(["```", "x", "   ```"]) == [rt._Fence(0, 2, 3)]
    # An info string means the line is not a closer, so this fence stays open.
    assert rt._fence_spans(["```", "```python", "x"]) == [rt._Fence(0, 3, 3)]


def test_fence_spans_rejects_a_backtick_opener_whose_info_string_has_a_backtick() -> None:
    # CommonMark forbids a backtick in a backtick fence's info string, so this is a
    # paragraph holding a code span. Treating it as an opener would shift every later
    # span and strand a real mockup outside a fence.
    assert rt._fence_spans(["```x``` is the flag", "", "prose"]) == []
    # An ordinary info string still opens one, and the rule is backtick-only.
    assert rt._fence_spans(["```python", "code", "```"]) == [rt._Fence(0, 2, 3)]
    assert rt._fence_spans(["~~~a`b", "x", "~~~"]) == [rt._Fence(0, 2, 3)]


def test_strip_sgr_drops_color_codes_and_keeps_every_other_escape() -> None:
    assert rt._strip_sgr("\x1b[1;33m▌ imp\x1b[0m done") == "▌ imp done"
    assert rt._strip_sgr("\x1b[38;2;10;20;200mtruecolor\x1b[m") == "truecolor"
    # Not SGR: cursor move, screen clear, OSC, BEL — untouched.
    assert rt._strip_sgr("\x1b[5A\x1b[2J\x1b]0;t\x07") == "\x1b[5A\x1b[2J\x1b]0;t\x07"


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
        _ev("3", "agent_message", agent_id="step:p/s", provider="anthropic", model="m",
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
    node = nv.RenameNode(title="my\x07change\x00")
    out = "\n".join(rt._render(node, depth=0, full=True, color=False, width=80))
    assert "\x07" not in out
    assert "\x00" not in out
    assert "^G" in out and "^@" in out
    assert "Set title to" in out             # imp's framing stays intact, unescaped


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
    sub.is_error = True   # §4: a non-dict outcome only renders on the error path
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
    # step_context no longer carries system_prompt; tools_schema escaping is tested here.
    node = nv.StepContextNode(payload={
        "provider": "anthropic", "model": "m", "max_tool_calls": 5,
        "tools_schema": [{"name": "bash\x1b"}],
    })
    out = "\n".join(rt._render_step_context(node, depth=0, full=True, color=False,
                                            width=80))
    assert "\x1b" not in out
    assert "^[" in out


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
    # An hour and up gains the hours unit and drops the seconds noise.
    assert rt.fmt_seconds_done(3600) == "1h 0m"
    assert rt.fmt_seconds_done(4536) == "1h 15m"
    assert rt.fmt_seconds_done(7205) == "2h 0m"
    # fmt_seconds itself is unchanged: the live clock still shows 0s.
    assert nv.fmt_seconds(0.4) == "0s"


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
        {"id": "e1", "agent_id": None, "type": "quest_created",
         "payload": {"handle": "h", "title": "t"}},
        {"id": "e2", "agent_id": None, "type": "user_message",
         "payload": {"role": "user", "subtype": "intent",
                     "content": [{"type": "text", "text": "add a flag"}]}},
    ])
    body = list(rt.render_tree(tree, full=False, color=False, width=80))
    assert any(ln == "> add a flag" for ln in body)   # depth-0, caret-marked human turn


_BT0 = datetime(2026, 6, 3, tzinfo=UTC)
_BA = "step:p/s"


def _bat(s: float) -> str:
    return (_BT0 + timedelta(seconds=s)).isoformat()


def _bstep() -> dict:
    return ev("s", "step_started", step="s", kind="agent", phase="p", model="m",
              provider="anthropic", timestamp=_bat(0))


def _bprose(eid: str, s: float) -> dict:
    return ev(eid, "agent_message", agent_id=_BA, provider="anthropic", model="m",
              stop_reason="tool_use", usage={},
              raw={"content": [{"type": "text", "text": f"prose {eid}"}]}, timestamp=_bat(s))


def _bcall(cid: str, name: str, s: float) -> dict:
    return ev(f"c-{cid}", "tool_call", agent_id=_BA, id=cid, name=name, input={},
              timestamp=_bat(s))


def _bresult(cid: str, s: float) -> dict:
    return ev(f"r-{cid}", "tool_result", agent_id=_BA, id=cid, content="ok", error=False,
              timestamp=_bat(s))


def _recaps(events: list[dict]) -> list[str]:
    return [ln.strip() for ln in _lines(events) if _re.match(r"\s*\d+ calls? · ", ln)]


def test_burst_recap_excludes_subagent_time_before_the_burst() -> None:
    # The burst opens after a 99s subagent; only the 5s bash call is timed.
    assert _recaps(subagent_then_bash_events()) == ["1 call · bash · 5s"]


def test_burst_recap_excludes_thinking_gap_between_calls() -> None:
    events = [_bstep(), _bprose("p1", 0),
              _bcall("a", "read_file", 1), _bresult("a", 3),
              ev("th", "thinking", agent_id=_BA, content="hmm", timestamp=_bat(10)),
              _bcall("b", "bash", 61), _bresult("b", 65),
              _bprose("p2", 70)]
    assert _recaps(events) == ["2 calls · read_file · bash · 6s"]


def test_burst_recap_counts_overlapping_calls_once() -> None:
    events = [_bstep(), _bprose("p1", 0),
              _bcall("a", "read_file", 1), _bcall("b", "grep", 1),
              _bresult("a", 4), _bresult("b", 6),
              _bprose("p2", 7)]
    assert _recaps(events) == ["2 calls · read_file · grep · 5s"]


def test_burst_recap_sub_second_tool_time_drops_duration() -> None:
    events = [_bstep(), _bprose("p1", 0),
              _bcall("a", "read_file", 1.0), _bresult("a", 1.3),
              _bcall("b", "grep", 1.4), _bresult("b", 1.6),
              _bcall("c", "read_file", 1.7), _bresult("c", 1.9),
              _bprose("p2", 30)]
    assert _recaps(events) == ["3 calls · read_file · grep"]


def test_burst_recap_skips_call_still_open_at_flush() -> None:
    events = [_bstep(), _bprose("p1", 0),
              _bcall("a", "bash", 1), _bresult("a", 4),
              _bcall("b", "read_file", 5),
              _bprose("p2", 20)]
    assert _recaps(events) == ["2 calls · bash · read_file · 3s"]


def test_burst_recap_with_no_completed_call_has_no_duration() -> None:
    events = [_bstep(), _bprose("p1", 0), _bcall("a", "read_file", 1), _bprose("p2", 20)]
    assert _recaps(events) == ["1 call · read_file"]


def test_orphan_tool_result_gets_zero_length_interval() -> None:
    tree = nv.build_tree([_bstep(), _bresult("zz", 5)])
    step = tree.root.children[0].children[0]
    (burst,) = [c for c in step.children if isinstance(c, nv.ToolBurstFold)]
    (call,) = burst.calls
    assert call.result_ts == call.call_ts == _bat(5)


def test_burst_recap_counts_killed_call_interval() -> None:
    events = [_bstep(), _bprose("p1", 0),
              _bcall("a", "bash", 1), _bresult("a", 31),
              ev("k", "tool_killed", agent_id=_BA, id="a", name="bash", timestamp=_bat(31)),
              _bprose("p2", 40)]
    assert _recaps(events) == ["1 call · bash · 30s"]


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
    node = nv.PauseNode(reason="max_loops")
    plain = "\n".join(rt._render(node, depth=0, full=False, color=False, width=200))
    assert plain == f"paused — {_MAX_LOOPS_COPY}"   # no glyph when color is off
    assert "max_loops" not in plain                 # raw token never reaches the user
    colored = "\n".join(rt._render(node, depth=0, full=False, color=True, width=200))
    assert f"⏸ paused — {_MAX_LOOPS_COPY}" in colored
    assert pal._ANSI["yellow"] in colored
    assert pal._ANSI["dim"] not in colored     # was dim metadata; no longer


def test_long_pause_copy_wraps_to_the_terminal_width() -> None:
    node = nv.PauseNode(reason="reauth_needed")
    lines = list(rt._render(node, depth=0, full=False, color=False, width=60))
    assert len(lines) > 1                        # the ~127-char copy no longer overruns
    assert all(len(ln) <= 60 for ln in lines)
    # With color, the glyph appears only on the first line; continuations carry a hang.
    colored = list(rt._render(node, depth=0, full=False, color=True, width=60))
    assert len(colored) > 1
    assert "⏸" in colored[0]
    assert "⏸" not in colored[1]
    # Continuation lines begin with a two-space hang (strip ANSI to measure).
    plain1 = _visible(colored[1])
    assert plain1.startswith("  ")


def test_pause_node_renders_lifecycle_wordmark_in_all_themes() -> None:
    node = nv.PauseNode(reason="max_loops")
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
                _ev("1", "quest_created", title="t"),
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
    # Forward-routed header: yellow " · <reason>" rides last, after the model.
    fwd = nv.StepNode(name="task-lead", kind="agent", label="implementing the plan",
                      model="opus", route_label="addressing review feedback")
    assert rt.header_line(fwd, depth=0, color=False) == \
        "implementing the plan · opus · addressing review feedback"
    # Resume header: " (resume)", then the model, then the reason — always last.
    res = nv.StepNode(name="task-lead", kind="agent", label="implementing the plan",
                      model="opus", resumed=True,
                      route_label="addressing review feedback")
    assert rt.header_line(res, depth=0, color=False) == \
        "implementing the plan (resume) · opus · addressing review feedback"
    # The reason segment is painted lifecycle (yellow), distinct from the step color.
    colored = rt.header_line(fwd, depth=0, color=True)
    assert pal.ansi(" · addressing review feedback",
                    *pal.NODE_COLORS["lifecycle"]) in colored
    # No route_label → label and model only.
    bare = nv.StepNode(name="task-lead", kind="agent", label="implementing the plan",
                       model="opus")
    assert rt.header_line(bare, depth=0, color=False) == "implementing the plan · opus"


def test_phase_end_rule_collapses_when_subsecond() -> None:
    # A sub-second phase drops the `· <elapsed>` suffix entirely: there must be no
    # dangling `· ` before the tail dashes.
    events = [
        {"id": "e0", "agent_id": None, "type": "quest_created", "payload": {"title": "t"}},
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


def test_header_line_prepends_phase_label_for_verb_step() -> None:
    step = nv.StepNode(name="finalize", kind="agent",
                       phase_label="implementation phase · plan 1/3",
                       label="finalizing")
    line = rt.header_line(step, depth=1, color=False)
    assert line == "  implementation phase · plan 1/3 · finalizing"


def test_phase_divider_uses_phase_label() -> None:
    phase = nv.PhaseNode(name="code", label="implementation phase · plan 1/3")
    assert rt.header_line(phase, depth=0, color=False) == (
        "── implementation phase · plan 1/3 ───")


def test_phase_breadcrumb_uses_phase_label() -> None:
    phase = nv.PhaseNode(name="code", label="implementation phase · plan 1/3")
    assert rt.phase_breadcrumb_line(phase, depth=0, color=False) == (
        "implementation phase · plan 1/3 complete")


def test_opening_intent_renders_at_root_before_step_header_and_narration() -> None:
    out = _lines([
        _ev("e1", "quest_created", title="add auth"),
        {"id": "e2", "agent_id": None, "type": "user_message",
         "payload": {"role": "user", "subtype": "intent",
                     "content": [{"type": "text", "text": "add a --json flag"}]}},
        _ev("s1", "step_started", step="spec", kind="agent", phase="design",
            model="m", provider="anthropic"),
        _ev("ll", "agent_message", agent_id="step:design/spec", provider="anthropic",
            model="m", stop_reason="end_turn", usage={},
            raw={"content": [{"type": "text", "text": "starting the work"}]}),
    ])
    joined = "\n".join(out)
    assert "> add a --json flag" in joined
    step_idx = next(i for i, ln in enumerate(out) if "spec" in ln)
    intent_idx = next(i for i, ln in enumerate(out) if "add a --json flag" in ln)
    narr_idx = next(i for i, ln in enumerate(out) if "starting the work" in ln)
    # Root placement: the opening intent renders at the top, ahead of the phase and
    # step headers; then the step header; then the agent's narration.
    assert intent_idx < step_idx < narr_idx


def test_breadcrumb_completion_includes_phase_label() -> None:
    bc = nv.BreadcrumbLine(
        step_name="finalize", kind="agent", elapsed_s=0.0,
        action={"verb": "continue"}, step_label="finalizing",
        phase_label="implementation phase · plan 1/3", show_complete=True)
    out = list(rt._render(bc, depth=1, full=False, color=False, width=80))
    assert out == ["  implementation phase · plan 1/3 · finalizing · complete"]


def test_breadcrumb_blocked_includes_phase_label() -> None:
    bc = nv.BreadcrumbLine(step_name="merge-to-base", elapsed_s=None,
                           action={"verb": "pause"}, extras=[],
                           kind="merge_and_exit_worktree",
                           phase_label="review phase · plan 1/1")
    line = "\n".join(rt._render(bc, depth=1, full=False, color=False, width=80))
    assert line == "  review phase · plan 1/1 · merge-to-base blocked"


def test_breadcrumb_interrupted_includes_phase_label() -> None:
    bc = nv.BreadcrumbLine(step_name="implement", kind="agent", elapsed_s=None,
                           action={"verb": "pause"}, extras=[], interrupted=True,
                           phase_label="implementation phase · task 3/3")
    line = "\n".join(rt._render(bc, depth=1, full=False, color=False, width=80))
    assert line == "  implementation phase · task 3/3 · implement interrupted"


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
            _ev("3", "agent_message", agent_id="step:p/s", provider="anthropic",
                model="m", stop_reason="end_turn", usage={},
                raw={"content": [{"type": "text", "text": long_line + "\n\n---\n\nafter"}]}),
        ]),
        full=False, color=False, width=10000, display_width=40))
    assert any(long_line in ln for ln in out)          # prose unwrapped
    assert any(ln.count("─") == 40 for ln in out)      # HR capped to display width
    assert not any(ln.count("─") > 40 for ln in out)


def test_step_header_prepends_phase_for_verb_step() -> None:
    node = nv.StepNode(name="plan-writer", kind="agent", label="writing plan",
                       phase_label="planning phase · plan 2/4", position_label=False)
    line = rt.header_line(node, depth=0, color=False)
    assert line.strip() == "planning phase · plan 2/4 · writing plan"


def test_step_header_prepends_phase_for_position_counter() -> None:
    node = nv.StepNode(name="task-lead", kind="agent", label="task 2/3",
                       phase_label="implementation phase · plan 1/1",
                       position_label=True)
    line = rt.header_line(node, depth=0, color=False)
    assert line.strip() == "implementation phase · plan 1/1 · task 2/3"


def test_step_header_position_label_without_phase_falls_to_label() -> None:
    from imp.cli import narrative as nv
    from imp.cli.render_text import header_line
    node = nv.StepNode(name="task-lead", kind="agent", label="task 2/3",
                       phase_label=None, position_label=True)
    line = header_line(node, depth=0, color=False)
    assert line.strip() == "task 2/3"


def test_header_line_prepends_phase_in_resumed_and_model_forms() -> None:
    # Non-resumed: phase · label · model — phase and label share one step-blue run.
    node = nv.StepNode(name="task-lead", kind="agent", label="task 3/3",
                       phase_label="implementation phase · plan 1/1",
                       model="claude", position_label=True)
    assert rt.header_line(node, depth=0, color=False) == \
        "implementation phase · plan 1/1 · task 3/3 · claude"
    # Resumed: the (resume) marker still rides right after the phase·label group.
    resumed = nv.StepNode(name="task-lead", kind="agent", label="task 3/3",
                          phase_label="implementation phase · plan 1/1",
                          model="claude", position_label=True, resumed=True)
    assert rt.header_line(resumed, depth=0, color=False) == \
        "implementation phase · plan 1/1 · task 3/3 (resume) · claude"
    # No phase_label → unchanged step-label-only header (fallback).
    bare = nv.StepNode(name="task-lead", kind="agent", label="task 3/3",
                       phase_label=None, position_label=True)
    assert rt.header_line(bare, depth=0, color=False) == "task 3/3"
    # The phase+label group is painted step-blue as one run (not the phase alone).
    colored = rt.header_line(node, depth=0, color=True)
    assert pal.ansi("implementation phase · plan 1/1 · task 3/3",
                    *pal.NODE_COLORS["step"]) in colored


def test_reauth_needed_pause_renders_plain_speak_copy() -> None:
    out = "\n".join(_lines([
        _ev("1", "workflow_paused", reason="reauth_needed"),
    ], width=200))
    assert "authentication failed" in out
    assert "the quest is paused and resumable" in out
    assert "re-login to the provider (codex login / claude login)" in out
    assert "then resume" in out
    # The raw internal token never reaches the user.
    assert "reauth_needed" not in out


def test_sandbox_spawn_failed_pause_renders_plain_speak_copy() -> None:
    out = "\n".join(_lines([
        _ev("1", "workflow_paused", reason="sandbox_spawn_failed"),
    ], width=200))
    assert "the OS sandbox could not start the step" in out
    assert "a sandbox-setup failure, not a gate result" in out
    assert "the quest is paused and resumable" in out
    assert "the warnings above name the cause" in out
    assert "transient" not in out          # five retries already disproved it
    # The raw internal token never reaches the user.
    assert "sandbox_spawn_failed" not in out


def test_other_pause_reasons_still_render_verbatim() -> None:
    out = "\n".join(_lines([
        _ev("1", "workflow_paused", reason="retry_ceiling"),
    ]))
    assert "paused (retry_ceiling)" in out


def test_result_less_complete_step_renders_closed_terminal() -> None:
    # Part A persists a result-less complete_step tail: the tool_call has no matching tool_result.
    events = [
        ev("1", "quest_created", quest_id=1, title="fix"),
        ev("2", "step_started", step="coder", kind="agent", phase="implement",
           model="m", provider="claude",
           timestamp="2026-07-01T00:00:00+00:00"),
        ev("3", "agent_message", agent_id="step:implement/coder",
           provider="claude", model="m", usage={},
           raw={"content": [{"type": "text", "text": "Implemented the fix."}]},
           timestamp="2026-07-01T00:00:01+00:00"),
        # The dangling `complete_step`: a tool_call with NO sibling tool_result.
        ev("4", "tool_call", agent_id="step:implement/coder", id="cs1",
           name="complete_step", input={"status": "Success", "summary": "did it"},
           timestamp="2026-07-01T00:00:02+00:00"),
        ev("5", "agent_result", agent_id="step:implement/coder",
           status="Success", summary="did it",
           timestamp="2026-07-01T00:00:03+00:00"),
        ev("6", "step_completed", step="coder", kind="agent",
           action={"verb": "continue"}, var_mutations={},
           timestamp="2026-07-01T00:00:04+00:00"),
    ]

    # Structural: closed terminal; the result-less call neither froze it nor became a subagent fold.
    tree = nv.build_tree(events)
    step = tree.root.children[0].children[0]
    assert step.closed is True
    assert step.end_ts == "2026-07-01T00:00:04+00:00"
    subfolds = [c for c in step.children if isinstance(c, nv.SubagentFold)]
    assert subfolds == []
    bursts = [c for c in step.children if isinstance(c, nv.ToolBurstFold)]
    assert len(bursts) == 1
    (call,) = bursts[0].calls
    assert call.name == "complete_step"
    assert call.result is None            # genuinely dangling (no phantom result)
    assert call.result_ts is None         # no result, so no timed interval

    # Lean render: closed terminal with step elapsed; the internal tool gets no recap.
    lean = "\n".join(_lines(events, full=False))
    assert "coder · complete · 4s" in lean
    assert "complete_step" not in lean

    # --full render: the call shows under the tools fold; same closed terminal with elapsed.
    full = "\n".join(_lines(events, full=True))
    assert "· complete_step" in full
    assert "coder · complete · status=Success · summary=did it · 4s" in full


def test_stale_awaiting_reply_disarmed_at_step_boundary_drops_next_kickoff() -> None:
    # D1: a step that SOLICITED (awaiting_user_input) but whose arm was never
    # consumed by a human reply — the run resumed with an engine_message/continuation
    # nudge — must not leave _awaiting_reply armed across the step boundary.
    # The NEXT step's begin-kickoff engine_message/agent_prompt must not leak as
    # a `> ...` human turn.
    events = [
        _ev("1", "step_started", step="a", kind="agent", phase="p", model="m"),
        _ev("2", "awaiting_user_input", agent_id="step:p/a", prompt="Continue?"),
        _ev("3", "engine_message", agent_id="step:p/a",
            role="user", subtype="continuation",
            content=[{"type": "text", "text": "continue"}]),
        _ev("4", "step_completed", step="a", kind="agent",
            action={"verb": "continue"}, var_mutations={}),
        _ev("5", "step_started", step="b", kind="agent", phase="p", model="m"),
        _ev("6", "step_context", agent_id="step:p/b"),
        _ev("7", "engine_message", agent_id="step:p/b",
            role="user", subtype="agent_prompt",
            content=[{"type": "text", "text": "INTERNAL STEP B KICKOFF"}]),
    ]
    tree = nv.build_tree(events)
    out = "\n".join(render_tree(tree, full=False, color=False, width=80))
    # Render side: the kickoff never appears (engine_message hidden in lean mode).
    assert "> INTERNAL STEP B KICKOFF" not in out
    assert "INTERNAL STEP B KICKOFF" not in out


def test_engine_continuation_renders_injected_message_in_full_only() -> None:
    # engine_message/continuation is invisible in lean, `continuation:` label under --full.
    events = [
        _ev("1", "step_started", step="a", kind="agent", phase="p", model="m"),
        _ev("2", "engine_message", agent_id="step:p/a",
            role="user", subtype="continuation",
            content=[{"type": "text", "text": "continue from where you left off"}]),
    ]
    tree = nv.build_tree(events)
    lean = "\n".join(render_tree(tree, full=False, color=False, width=80))
    full = "\n".join(render_tree(tree, full=True, color=False, width=80))
    assert "continue from where you left off" not in lean
    assert "continuation:" not in lean
    assert "continuation:" in full
    assert "continue from where you left off" in full


def test_continuation_note_renders_as_continuation_never_as_a_human_turn() -> None:
    # End-to-end over the real journaling: a ContinuationNote delivered with the
    # human=True every provider hard-codes must stay out of lean `imp log`, show
    # under --full behind the `continuation:` label, and NEVER carry the `>`
    # caret the renderer reserves for a genuine human turn.
    note_text = "The user has disabled the sandbox."
    elog = InMemoryEventLog()
    elog.append(agent_id=None, type="step_started",
                payload={"step": "a", "kind": "agent", "phase": "p", "model": "m"})
    deliver_continuation_reply(
        [], None, elog, "step:p/a", ContinuationNote(text=note_text), human=True)
    tree = nv.build_tree(list(elog.read()))
    lean = "\n".join(render_tree(tree, full=False, color=False, width=80))
    full = "\n".join(render_tree(tree, full=True, color=False, width=80))
    assert note_text not in lean
    assert "continuation:" not in lean
    assert "continuation:" in full
    assert note_text in full
    assert f"> {note_text}" not in full
    assert f"> {note_text}" not in lean


def test_begin_kickoff_never_replays_as_human_turn_from_events_jsonl(tmp_path) -> None:
    # D1 (persist + imp log replay): the same scenario persisted to a real
    # events.jsonl and replayed the way `imp log` reads it (line-delimited JSON).
    # The begin-kickoff is an engine_message/agent_prompt; replaying must never
    # render it as a `> ...` human turn.
    events = [
        {"id": "1", "agent_id": None, "type": "step_started",
         "payload": {"step": "a", "kind": "agent", "phase": "p", "model": "m"}},
        {"id": "2", "agent_id": "step:p/a", "type": "awaiting_user_input",
         "payload": {"prompt": "Continue?"}},
        {"id": "3", "agent_id": "step:p/a", "type": "engine_message",
         "payload": {"role": "user", "subtype": "continuation",
                     "content": [{"type": "text", "text": "continue"}]}},
        {"id": "4", "agent_id": None, "type": "step_completed",
         "payload": {"step": "a", "kind": "agent",
                     "action": {"verb": "continue"}, "var_mutations": {}}},
        {"id": "5", "agent_id": None, "type": "step_started",
         "payload": {"step": "b", "kind": "agent", "phase": "p", "model": "m"}},
        {"id": "6", "agent_id": "step:p/b", "type": "step_context",
         "payload": {}},
        {"id": "7", "agent_id": "step:p/b", "type": "engine_message",
         "payload": {"role": "user", "subtype": "agent_prompt",
                     "content": [{"type": "text",
                                  "text": "INTERNAL STEP B KICKOFF"}]}},
    ]
    log_path = tmp_path / "events.jsonl"
    log_path.write_text("".join(json.dumps(e) + "\n" for e in events))

    on_disk = [json.loads(ln) for ln in log_path.read_text().splitlines()
               if ln.strip()]
    # Persist side: the kickoff IS present on disk as an engine_message event.
    assert any(e["type"] == "engine_message"
               and e["payload"]["content"][0]["text"] == "INTERNAL STEP B KICKOFF"
               for e in on_disk)
    # Replay/render side: it never replays as a human `>` turn.
    tree = nv.build_tree(on_disk)
    out = "\n".join(render_tree(tree, full=False, color=False, width=80))
    assert "> INTERNAL STEP B KICKOFF" not in out
    assert "INTERNAL STEP B KICKOFF" not in out


def test_full_view_spaces_unlike_block_kinds() -> None:
    # --full must put exactly ONE blank line between adjacent unlike block kinds.
    # step_context, a thinking fold, an expanded tool burst, and prose turns used
    # to bunch with no separator; each adjacent pair now gets a single blank and
    # none is doubled. (spec acceptance #1)
    out = _lines([
        _ev("1", "step_started", step="s", kind="agent", phase="p",
            model="m", provider="anthropic"),
        _ev("2", "step_context", agent_id="step:p/s", provider="anthropic",
            model="claude", tools_schema=[{"name": "bash"}],
            params={}, max_tool_calls=50),
        _ev("3", "thinking", agent_id="step:p/s", content="deep thoughts"),
        _ev("4", "tool_call", agent_id="step:p/s", id="b1", name="bash",
            input={"command": "ls"}),
        _ev("5", "tool_result", agent_id="step:p/s", id="b1", content="ok", error=False),
        _ev("6", "agent_message", agent_id="step:p/s",
            raw={"content": [{"type": "text", "text": "First prose turn."}]}, usage={}),
        _ev("7", "agent_message", agent_id="step:p/s",
            raw={"content": [{"type": "text", "text": "Second prose turn."}]}, usage={}),
    ], full=True)

    def blank_above(marker: str) -> None:
        i = next(k for k, ln in enumerate(out) if marker in ln)
        assert i >= 2, f"{marker!r} found too early to check blanks"
        assert out[i - 1].strip() == "", f"{marker!r} must have one blank line above it"
        assert out[i - 2].strip() != "", f"{marker!r} must not have a doubled blank above it"

    blank_above("▾ thinking")          # step_context → thinking
    blank_above("▾ tools")             # thinking → expanded burst
    blank_above("First prose turn.")   # burst → prose
    blank_above("Second prose turn.")  # prose → prose


def test_full_view_whitespace_thinking_leaves_spacing_intact() -> None:
    # A whitespace-only thinking fold renders nothing in --full and must not create
    # a doubled or missing blank between the prose turns around it. (spec acceptance #1)
    out = _lines([
        _ev("1", "step_started", step="s", kind="agent", phase="p"),
        _ev("2", "agent_message", agent_id="a",
            raw={"content": [{"type": "text", "text": "First prose turn."}]}, usage={}),
        _ev("3", "thinking", agent_id="a", content="   "),   # whitespace-only → renders nothing
        _ev("4", "agent_message", agent_id="a",
            raw={"content": [{"type": "text", "text": "Second prose turn."}]}, usage={}),
    ], full=True)
    i = next(k for k, ln in enumerate(out) if "Second prose turn." in ln)
    assert out[i - 1].strip() == "", "one blank separates the prose turns"
    assert out[i - 2].strip() != "", "no doubled blank around the empty thinking fold"


def test_full_view_whitespace_injected_message_leaves_spacing_intact() -> None:
    # A whitespace-only non-interactive user_message renders nothing in --full and must
    # not create a doubled or missing blank between the prose turns around it.
    out = _lines([
        _ev("1", "step_started", step="s", kind="agent", phase="p"),
        _ev("2", "agent_message", agent_id="a",
            raw={"content": [{"type": "text", "text": "First prose turn."}]}, usage={}),
        _ev("3", "engine_message", agent_id="step:p/s",
            role="user", subtype="agent_prompt",
            content=[{"type": "text", "text": "   \n  "}]),  # whitespace-only → renders nothing
        _ev("4", "agent_message", agent_id="a",
            raw={"content": [{"type": "text", "text": "Second prose turn."}]}, usage={}),
    ], full=True)
    i = next(k for k, ln in enumerate(out) if "Second prose turn." in ln)
    assert out[i - 1].strip() == "", "one blank separates the prose turns"
    assert out[i - 2].strip() != "", "no doubled blank around the empty injected message"


def test_full_view_renders_injected_top_level_prompt() -> None:
    # In --full an engine_message/agent_prompt renders under an `agent prompt:` label
    # with its verbatim body visible. The lean view drops it entirely.
    events = [
        _ev("1", "step_started", step="s", kind="agent", phase="p",
            model="m", provider="anthropic"),
        _ev("2", "engine_message", agent_id="step:p/s",
            role="user", subtype="agent_prompt",
            content=[{"type": "text", "text": "REAL PROMPT: build the widget"}]),
    ]
    full = "\n".join(_lines(events, full=True))
    assert "agent prompt:" in full
    assert "REAL PROMPT: build the widget" in full

    lean = "\n".join(_lines(events))
    assert "agent prompt:" not in lean
    assert "REAL PROMPT: build the widget" not in lean


def test_full_view_renders_subagent_task_description_injected() -> None:
    # A subagent's first engine_message/agent_prompt is the parent's task_description to it.
    # It routes into the fold's nested tree (agent_id "sub:<event-id>") and, in
    # --full, renders under an `agent prompt:` label INSIDE the expanded fold.
    # The lean view drops it.
    events = [
        _ev("1", "step_started", step="s", kind="agent", phase="p",
            model="m", provider="anthropic"),
        _ev("tk1", "tool_call", agent_id="step:p/s", id="b1", name="sub_agent",
            input={"subagent_name": "explorer", "task_description": "go"}),
        _ev("um1", "engine_message", agent_id="sub:tk1",
            role="user", subtype="agent_prompt",
            content=[{"type": "text", "text": "map the auth module end to end"}]),
        _ev("su1", "agent_message", agent_id="sub:tk1", provider="anthropic",
            model="m", stop_reason="end_turn", usage={},
            raw={"content": [{"type": "text", "text": "found it"}]}),
        _ev("r1", "tool_result", agent_id="step:p/s", id="b1",
            content='{"status":"Success"}', error=False),
    ]
    full = "\n".join(_lines(events, full=True))
    assert "▾ explorer" in full
    assert "agent prompt:" in full
    assert "map the auth module end to end" in full

    lean = "\n".join(_lines(events))
    assert "map the auth module end to end" not in lean
    assert "agent prompt:" not in lean


def test_full_parallel_subagents_show_distinct_task_descriptions() -> None:
    # Acceptance #3: two concurrently-dispatched explorers with different
    # task_descriptions render as two folds whose expanded headers each carry
    # their own description; the lean rows carry none.
    events = [
        _ev("2", "step_started", step="s", kind="agent", phase="p",
            model="m", provider="anthropic"),
        _ev("tk1", "tool_call", agent_id="step:p/s", id="b1", name="sub_agent",
            input={"subagent_name": "explorer",
                   "task_description": "Map the auth module"}),
        _ev("tk2", "tool_call", agent_id="step:p/s", id="b2", name="sub_agent",
            input={"subagent_name": "explorer",
                   "task_description": "Map the billing module"}),
        _ev("su1", "agent_message", agent_id="sub:tk1", provider="anthropic", model="m",
            stop_reason="end_turn", usage={},
            raw={"content": [{"type": "text", "text": "found auth"}]}),
        _ev("su2", "agent_message", agent_id="sub:tk2", provider="anthropic", model="m",
            stop_reason="end_turn", usage={},
            raw={"content": [{"type": "text", "text": "found billing"}]}),
        _ev("r1", "tool_result", agent_id="step:p/s", id="b1",
            content='{"status":"Success"}', error=False),
        _ev("r2", "tool_result", agent_id="step:p/s", id="b2",
            content='{"status":"Success"}', error=False),
    ]
    full = "\n".join(_lines(events, full=True))
    assert "▾ explorer · Map the auth module" in full
    assert "▾ explorer · Map the billing module" in full

    lean = "\n".join(_lines(events, full=False))
    # Lean subagent_status_row is unchanged: the name row survives, the
    # task description does not.
    assert "  explorer" in lean
    assert "Map the auth module" not in lean
    assert "Map the billing module" not in lean


def test_full_subagent_header_clips_and_collapses_task_description() -> None:
    # Long descriptions clip to _SUBAGENT_TASK_CLIP chars + ellipsis; internal
    # whitespace (incl. newlines) collapses to single spaces.
    long_sub = nv.SubagentFold(
        subagent_name="explorer", event_id="e", block_id="b",
        nested=nv.NarrativeBuilder(), task_description="x" * 200)
    long_out = list(rt._render_subagent(long_sub, depth=0, full=True,
                                        color=False, width=80))
    assert long_out[0] == "▾ explorer · " + "x" * rt._SUBAGENT_TASK_CLIP + "…"

    multiline = nv.SubagentFold(
        subagent_name="explorer", event_id="e", block_id="b",
        nested=nv.NarrativeBuilder(), task_description="Map  the\nauth   module")
    ml_out = list(rt._render_subagent(multiline, depth=0, full=True,
                                      color=False, width=80))
    assert ml_out[0] == "▾ explorer · Map the auth module"


def test_subagent_outcome_plain_string_success_suppressed() -> None:
    # §4: a plain-string SUCCESS return is exactly the nested transcript's final
    # prose turn; the raw outcome block must not double-print it.
    assert list(rt.subagent_outcome_lines(
        _sub(outcome="found the auth module", is_error=False),
        depth=1, color=False, width=80)) == []


def test_full_subagent_plain_string_success_shown_once() -> None:
    # Acceptance #4 (end-to-end): in --full a plain-string success return equals
    # the subagent's final prose turn and appears exactly once — no raw outcome
    # echo beneath it.
    events = [
        _ev("2", "step_started", step="s", kind="agent", phase="p",
            model="m", provider="anthropic"),
        _ev("tk1", "tool_call", agent_id="step:p/s", id="b1", name="sub_agent",
            input={"subagent_name": "explorer",
                   "task_description": "Map the billing module"}),
        _ev("su1", "agent_message", agent_id="sub:tk1", provider="anthropic",
            model="m", stop_reason="end_turn", usage={},
            raw={"content": [{"type": "text", "text": "found the auth module"}]}),
        _ev("r1", "tool_result", agent_id="step:p/s", id="b1",
            content="found the auth module", error=False),
    ]
    full = "\n".join(_lines(events, full=True))
    assert full.count("found the auth module") == 1


def test_subagent_model_is_public() -> None:
    import imp.cli.render_text as rt
    assert callable(rt.subagent_model)
    assert not hasattr(rt, "_subagent_model")   # the private alias is gone


def test_header_line_paints_model_in_gray() -> None:
    node = nv.StepNode(name="plan-writer", kind="agent", label="writing plan", model="opus")
    colored = rt.header_line(node, depth=0, color=True)
    assert pal.ansi(" · opus", "gray") in colored                         # model dim gray (C3)
    assert pal.ansi("writing plan", *pal.NODE_COLORS["step"]) in colored  # label stays step-blue
    assert rt.header_line(node, depth=0, color=False) == "writing plan · opus"  # plain unchanged
    # a resumed step's model is gray too (not step-blue).
    resumed = nv.StepNode(name="code-reviewer", kind="agent", label="reviewing code",
                          model="opus", resumed=True)
    assert pal.ansi(" · opus", "gray") in rt.header_line(resumed, depth=0, color=True)


def test_subagent_status_row_paints_model_and_frozen_elapsed_gray() -> None:
    b = nv.NarrativeBuilder()
    b.add(ev("1", "quest_created", quest_id=1, title="x"))
    b.add(ev("2", "step_started", step="s", kind="agent", phase="p",
             model="m", provider="anthropic"))
    b.add(ev("t1", "tool_call", agent_id="step:p/s", id="blk", name="sub_agent",
             input={"subagent_name": "reviewer"}))
    b.add(ev("r1", "agent_message", agent_id="sub:t1", provider="anthropic", model="opus",
             stop_reason="end_turn", usage={},
             raw={"content": [{"type": "text", "text": "hi"}]}))
    fold = b.running_subagents()[0]
    running = rt.subagent_status_row(fold, depth=0, color=True)
    assert pal.ansi(" · opus", "gray") in running                          # model dim gray (C3)
    assert pal.ansi("  reviewer", *pal.NODE_COLORS["subagent"]) in running  # name stays magenta

    # A finished fold (empty nested → no model): the frozen run duration prints dim gray.
    done = nv.SubagentFold(subagent_name="reviewer", event_id="e", block_id="b",
                           nested=nv.NarrativeBuilder(), running=False,
                           start_ts="2026-01-01T00:00:00", end_ts="2026-01-01T00:00:42")
    finished = rt.subagent_status_row(done, depth=0, color=True)
    assert pal.ansi(" · 42s", "gray") in finished  # frozen elapsed dim gray
    assert rt.subagent_status_row(done, depth=0, color=False) == "  reviewer · 42s"


def _show_lines(events, cats):
    return list(render_tree(nv.build_tree(events), show=frozenset(cats),
                            color=False, width=80))


_RICH_EVENTS = [
    _ev("2", "step_started", step="s", kind="agent", phase="p",
        model="m", provider="anthropic"),
    _ev("3", "step_context", agent_id="step:p/s", provider="anthropic",
        model="claude", tools_schema=[{"name": "a"}], params={}, max_tool_calls=50),
    _ev("sp", "engine_message", agent_id="step:p/s",
        role="system", subtype="system_prompt",
        content=[{"type": "text", "text": "SECRETPROMPT"}]),
    _ev("4", "thinking", agent_id="step:p/s", content="deep thoughts here"),
    _ev("5", "tool_call", agent_id="step:p/s", id="b1", name="bash",
        input={"command": "ls -la"}),
    _ev("6", "tool_result", agent_id="step:p/s", id="b1", content="total 0",
        error=False),
    _ev("tk1", "tool_call", agent_id="step:p/s", id="b2", name="sub_agent",
        input={"subagent_name": "reviewer", "task_description": "go"}),
    _ev("su1", "agent_message", agent_id="sub:tk1", provider="anthropic",
        model="m", stop_reason="end_turn", usage={},
        raw={"content": [{"type": "text", "text": "verdict: approve"}]}),
    _ev("r1", "tool_result", agent_id="step:p/s", id="b2",
        content='{"status":"Success","summary":"ok"}', error=False),
]


def test_show_all_matches_full_and_empty_matches_lean() -> None:
    tree = nv.build_tree(_RICH_EVENTS)
    all_cats = list(render_tree(tree, show=frozenset(rt.LOG_CATEGORIES),
                                color=False, width=80))
    none_cats = list(render_tree(tree, show=frozenset(), color=False, width=80))
    assert all_cats == _lines(_RICH_EVENTS, full=True)
    assert none_cats == _lines(_RICH_EVENTS, full=False)


def test_show_thinking_reveals_only_thinking() -> None:
    out = "\n".join(_show_lines(_RICH_EVENTS, {"thinking"}))
    assert "▾ thinking" in out and "deep thoughts here" in out
    assert "▾ tools" not in out          # tools stay a lean recap
    assert "SECRETPROMPT" not in out     # prompts hidden
    assert "▾ reviewer" not in out       # subagent stays a lean status row


def test_show_tools_reveals_only_tools() -> None:
    out = "\n".join(_show_lines(_RICH_EVENTS, {"tools"}))
    assert "▾ tools" in out and "command=ls -la" in out and "total 0" in out
    assert "▾ thinking" not in out
    assert "SECRETPROMPT" not in out


def test_show_subagents_reveals_subagent_output_only() -> None:
    out = "\n".join(_show_lines(_RICH_EVENTS, {"subagents"}))
    assert "▾ reviewer" in out and "verdict: approve" in out
    assert "→ status=Success · summary=ok" in out
    assert "▾ thinking" not in out       # nested thinking not revealed
    assert "SECRETPROMPT" not in out


def test_show_prompts_reveals_step_context_only() -> None:
    out = "\n".join(_show_lines(_RICH_EVENTS, {"prompts"}))
    assert "SECRETPROMPT" in out         # system prompt dump
    assert "▾ thinking" not in out
    assert "▾ reviewer" not in out       # subagent stays a lean status row


def test_show_tiers_reveals_only_tiers() -> None:
    events = [
        _ev("e1", "quest_created", title="t", quest_id=1),
        _ev("e2", "tiers_resolved",
            tiers={"medium": {"provider": "anthropic", "model": "haiku",
                              "differ_tier": None, "differ_from": None}},
            steps={}),
    ]
    out = "\n".join(_show_lines(events, {"tiers"}))
    assert "tiers resolved:" in out
    assert "→ anthropic/haiku" in out
    out_no_tiers = "\n".join(_show_lines(events, {"thinking", "tools", "subagents", "prompts"}))
    assert "tiers resolved:" not in out_no_tiers
    assert "→ anthropic/haiku" not in out_no_tiers


def test_full_labels_each_engine_subtype_verbatim():
    out = "\n".join(_lines([
        _ev("1", "step_started", step="plan", kind="agent", phase="build",
            model="m", provider="anthropic"),
        _ev("e0", "engine_message", agent_id="step:build/plan",
            subtype="system_prompt", role="system",
            content=[{"type": "text", "text": "SYS " + "x" * 400}]),
        _ev("e1", "engine_message", agent_id="step:build/plan",
            subtype="agent_prompt", role="user",
            content=[{"type": "text", "text": "the body"}]),
        _ev("e2", "engine_message", agent_id="step:build/plan",
            subtype="continuation", role="user",
            content=[{"type": "text", "text": "keep going"}]),
        _ev("e3", "engine_message", agent_id="step:build/plan",
            subtype="completion", role="user",
            content=[{"type": "text", "text": "done?"}]),
    ], full=True))
    assert "system prompt:" in out
    assert "agent prompt:" in out
    assert "continuation:" in out
    assert "completion nudge:" in out
    assert "injected message:" not in out
    assert "x" * 40 in out
    # the 400-char body is under the system-prompt label, not a mislabeled block
    assert out.index("system prompt:") < out.index("x" * 40) < out.index("agent prompt:")


def test_lean_hides_engine_messages():
    out = "\n".join(_lines([
        _ev("1", "step_started", step="plan", kind="agent", phase="build",
            model="m", provider="anthropic"),
        _ev("e1", "engine_message", agent_id="step:build/plan",
            subtype="continuation", role="user",
            content=[{"type": "text", "text": "keep going"}]),
    ], full=False))
    assert "keep going" not in out
    assert "continuation:" not in out


def test_step_context_full_has_no_system_prompt_line():
    out = "\n".join(_lines([
        _ev("1", "step_started", step="plan", kind="agent", phase="build",
            model="m", provider="anthropic"),
        _ev("c1", "step_context", agent_id="step:build/plan",
            provider="anthropic", model="m",
            tools_schema=[{"name": "read_file"}], params={}, max_tool_calls=40),
    ], full=True))
    assert "system_prompt:" not in out
    assert "tools:" in out


def test_header_line_drops_route_suffix_when_equal_to_label() -> None:
    reason = "addressing spec review feedback"
    # Forward-routed: label == route_label → reason appears once, no trailing suffix.
    fwd = nv.StepNode(name="spec", kind="agent", label=reason,
                      model="claude-opus", route_label=reason)
    assert rt.header_line(fwd, depth=0, color=False) == \
        f"{reason} · claude-opus"
    # Resumed: same equality → reason once, ends at the model after " (resume)".
    res = nv.StepNode(name="spec", kind="agent", label=reason,
                      model="claude-opus", resumed=True, route_label=reason)
    assert rt.header_line(res, depth=0, color=False) == \
        f"{reason} (resume) · claude-opus"
    # Color on: the lifecycle-painted route suffix segment is absent in both forms.
    suffix = pal.ansi(f" · {reason}", *pal.NODE_COLORS["lifecycle"])
    assert suffix not in rt.header_line(fwd, depth=0, color=True)
    assert suffix not in rt.header_line(res, depth=0, color=True)
    # A differing route_label is still rendered as the trailing suffix (preserved).
    diff = nv.StepNode(name="task-lead", kind="agent", label="task 1/3",
                       model="claude-opus", route_label=reason)
    assert rt.header_line(diff, depth=0, color=False) == \
        f"task 1/3 · claude-opus · {reason}"
    # With a phase_label set, the header prefixes the phase and still drops the
    # duplicate route suffix when label == route_label.
    phased = nv.StepNode(name="spec", kind="agent", label=reason,
                         model="claude-opus", phase_label="spec phase",
                         route_label=reason)
    assert rt.header_line(phased, depth=0, color=False) == \
        f"spec phase · {reason} · claude-opus"


def test_other_pause_reasons_render_exactly_as_before() -> None:
    def line(reason: str) -> str:
        node = nv.PauseNode(reason=reason)
        return "\n".join(rt._render(node, depth=0, full=False, color=False, width=200))

    assert line("reauth_needed") == (
        "paused — authentication failed; the quest is paused and resumable; "
        "re-login to the provider (codex login / claude login), then resume")
    assert line("sandbox_spawn_failed") == (
        "paused — the OS sandbox could not start the step (a sandbox-setup "
        "failure, not a gate result); the quest is paused and resumable — the "
        "warnings above name the cause")
    assert line("script_blocked") == "paused (script_blocked)"


def test_subagent_provider_digs_the_provider_out_of_the_nested_step_context() -> None:
    b = nv.NarrativeBuilder()
    b.add(ev("1", "quest_created", quest_id=1, title="x"))
    b.add(ev("2", "step_started", step="s", kind="agent", phase="p",
             model="claude-opus-5", provider="anthropic"))
    b.add(ev("t1", "tool_call", agent_id="step:p/s", id="blk-t1",
             name="sub_agent", input={"subagent_name": "coder"},
             timestamp="2026-01-01T00:00:00+00:00"))
    # The subagent's own step_context carries provider+model — the same node
    # subagent_model walks. A subagent may run on a different backend than its
    # parent step, so the pair must come from the nested tree, not the parent.
    b.add(ev("c1", "step_context", agent_id="sub:t1", provider="openai",
             model="gpt-6.1-sol", tools_schema=[], params={},
             max_tool_calls=None))
    f = b.running_subagents()[0]
    assert rt.subagent_model(f) == "gpt-6.1-sol"
    assert rt.subagent_provider(f) == "openai"


def test_subagent_provider_is_none_without_a_resolvable_node() -> None:
    b = nv.NarrativeBuilder()
    b.add(ev("1", "quest_created", quest_id=1, title="x"))
    b.add(ev("2", "step_started", step="s", kind="agent", phase="p",
             model="claude-opus-5", provider="anthropic"))
    b.add(ev("t1", "tool_call", agent_id="step:p/s", id="blk-t1",
             name="sub_agent", input={"subagent_name": "coder"},
             timestamp="2026-01-01T00:00:00+00:00"))
    f = b.running_subagents()[0]
    assert rt.subagent_provider(f) is None


def test_subagent_provider_digs_the_provider_out_of_an_agent_text_node() -> None:
    b = nv.NarrativeBuilder()
    b.add(ev("1", "quest_created", quest_id=1, title="x"))
    b.add(ev("2", "step_started", step="s", kind="agent", phase="p",
             model="claude-opus-5", provider="anthropic"))
    b.add(ev("t1", "tool_call", agent_id="step:p/s", id="blk",
             name="sub_agent", input={"subagent_name": "coder"}))
    # agent_message with text content → AgentTextNode in the subagent's nested
    # narrative. No step_context, so the assertion can only pass via AgentTextNode.
    # OpenAI Responses wire format: text lives in output[].content[].
    b.add(ev("r1", "agent_message", agent_id="sub:t1", provider="openai",
             model="gpt-6.1-sol", stop_reason="completed", usage={},
             raw={"output": [{"type": "message",
                              "content": [{"type": "output_text", "text": "done"}]}]}))
    f = b.running_subagents()[0]
    # Confirm the fixture really produced an AgentTextNode, not a StepContextNode.
    assert any(isinstance(n, nv.AgentTextNode) for n in f.tree.root.children)
    assert rt.subagent_provider(f) == "openai"
    assert rt.subagent_model(f) == "gpt-6.1-sol"


def test_quota_pause_renders_plain_speak_copy() -> None:
    node = nv.PauseNode(reason="quota")
    line = "\n".join(rt._render(node, depth=0, full=False, color=False, width=200))
    assert line == (
        "paused — quota is exhausted; the quest is paused and resumable — wait "
        "for the limit to reset or add credits, then resume")
    # bare token fallback is gone
    out = "\n".join(_lines([_ev("1", "workflow_paused", reason="quota")], width=200))
    assert "paused (quota)" not in out


def test_detached_pause_renders_plain_speak_copy() -> None:
    node = nv.PauseNode(reason="detached")
    line = "\n".join(rt._render(node, depth=0, full=False, color=False, width=200))
    assert line == (
        "paused — the terminal running this quest went away; the quest is paused "
        "and resumable — resume to pick it up where it stopped")
    # the payload's interrupt_reason routes here, not the engine's `interrupt`
    out = "\n".join(_lines([
        _ev("1", "workflow_paused", reason="interrupt", interrupt_reason="detached"),
    ], width=200))
    assert "the terminal running this quest went away" in out
    assert "paused (detached)" not in out      # bare-token fallback is gone


def test_auth_terminal_pause_renders_the_reauth_copy() -> None:
    # kind must not disturb the shipped copy
    out = "\n".join(_lines([
        _ev("1", "workflow_paused", reason="reauth_needed",
            kind="AuthTerminalError"),
    ], width=200))
    assert "authentication failed" in out
    assert "kept failing" not in out   # auth_terminal pauses on the first failure
    assert "re-login to the provider (codex login / claude login)" in out
    assert "auth_terminal" not in out
    assert "AuthTerminalError" not in out


def test_caret_escape_prose_keeps_sgr_inside_a_fence_and_carets_outside() -> None:
    text = "hi \x1b[31mred\x1b[0m\n\n```\n\x1b[1;33m▌ imp\x1b[0m\n```\n"
    out = rt._caret_escape_prose(text)
    assert "hi ^[[31mred^[[0m" in out          # flowing prose: still absolute
    assert "\x1b[1;33m▌ imp\x1b[0m" in out      # inside the fence: verbatim


def test_caret_escape_prose_carets_non_sgr_controls_inside_a_fence() -> None:
    text = "```\n\x1b[31mR\x1b[0m \x07 \x1b[2J \x1b[5A \x1b]0;title\x07\n```\n"
    out = rt._caret_escape_prose(text)
    assert "\x1b[31mR\x1b[0m" in out            # SGR survives
    assert "^G" in out and "^[[2J" in out and "^[[5A" in out
    assert "^[]0;title^G" in out                # OSC title write, caret-escaped
    assert "\x07" not in out


def test_caret_escape_prose_treats_an_indented_code_block_as_prose() -> None:
    assert rt._caret_escape_prose("    \x1b[31mR\x1b[0m") == "    ^[[31mR^[[0m"


def test_caret_escape_prose_exempts_content_lines_only_not_the_delimiters() -> None:
    # The splice lifts content lines only, so SGR left raw on a delimiter (a colored
    # info string) would reach rich unlifted — the one thing the splice exists to
    # prevent. The exception covers exactly the lines the splice takes.
    assert (rt._caret_escape_prose("```\x1b[31m\n\x1b[32mbody\x1b[0m\n```\n")
            == "```^[[31m\n\x1b[32mbody\x1b[0m\n```\n")


def test_caret_escape_prose_exempts_an_unclosed_fence_to_end_of_text() -> None:
    out = rt._caret_escape_prose("p \x1b[31mq\x1b[0m\n\n```\n\x1b[32mA\x1b[0m\n\x1b[33mB\x1b[0m\n")
    assert "p ^[[31mq^[[0m" in out
    assert "\x1b[32mA\x1b[0m\n\x1b[33mB\x1b[0m" in out


def test_caret_escape_prose_handles_two_fences_with_prose_between() -> None:
    out = rt._caret_escape_prose(
        "```\n\x1b[31mA\x1b[0m\n```\n\nmid \x1b[35mm\x1b[0m\n\n```\n\x1b[32mB\x1b[0m\n```\n")
    assert "\x1b[31mA\x1b[0m" in out and "\x1b[32mB\x1b[0m" in out
    assert "mid ^[[35mm^[[0m" in out


# luge78's message #596, exactly as the model typed it: no ESC byte anywhere.
_BARE_MOCKUP = ("[38;2;255;154;60mwarning: [auth] retry 2 (1.0s) — another session "
                "rotated the OAuth token; retrying with the new one[0m")


def test_caret_escape_prose_arms_a_bare_sgr_code_inside_a_fence() -> None:
    out = rt._caret_escape_prose(f"Look:\n\n```\n{_BARE_MOCKUP}\n```\n")
    assert "\x1b[38;2;255;154;60mwarning: [auth] retry 2 (1.0s)" in out
    assert out.endswith("retrying with the new one\x1b[0m\n```\n")
    assert "[38;2" not in _visible(out)      # nothing bare left behind
    assert "[auth]" in out                   # a non-SGR bracket is not eaten


def test_caret_escape_prose_leaves_a_bare_code_in_flowing_prose_alone() -> None:
    text = f"note: {_BARE_MOCKUP}\n"
    assert rt._caret_escape_prose(text) == text


def test_caret_escape_prose_does_not_rearm_a_caret_escaped_code() -> None:
    text = "```\n^[[33mdefused^[[0m\n```\n"
    assert rt._caret_escape_prose(text) == text


def test_caret_escape_prose_does_not_double_a_real_escape_in_a_fence() -> None:
    text = "```\n\x1b[33mreal\x1b[0m\n```\n"
    assert rt._caret_escape_prose(text) == text
    assert "\x1b\x1b" not in rt._caret_escape_prose(text)


def test_caret_escape_prose_leaves_backslash_escape_spellings_alone() -> None:
    # A fence carries source as often as a screen; these are string literals.
    body = r'print("\x1b[31m"); print("\x1B[31m"); print("\033[31m"); print("\e[31m")'
    text = f"```\n{body}\n```\n"
    assert rt._caret_escape_prose(text) == text


def test_caret_escape_prose_leaves_non_sgr_bracket_text_alone() -> None:
    text = "```\n[m [hello] [3, 4] [2026-09-05] arr[0] mid[1;33\n```\n"
    assert rt._caret_escape_prose(text) == text


def test_caret_escape_prose_leaves_an_imp_model_id_alone() -> None:
    # `claude-opus-4-8[1m]` (catalog.py:49) collides with the SGR shape, and an agent
    # pasting `imp model` output into a fence is this feature's own use case. The
    # `]` lookahead is what keeps the id intact.
    text = "```\nhigh: claude-opus-4-8[1m] · window 1M\n```\n"
    assert rt._caret_escape_prose(text) == text


def test_caret_escape_prose_does_not_arm_a_fence_delimiter_line() -> None:
    # Delimiters are not eligible: the splice lifts content lines only, so a code
    # armed on the info string would reach rich unlifted.
    assert (rt._caret_escape_prose("```[31m\n[32mbody[0m\n```\n")
            == "```[31m\n\x1b[32mbody\x1b[0m\n```\n")


# 83 raw chars, 55 visible columns (57 with the 2-col hang indent): at width 70
# rich counts the escape bytes as visible and wraps it before "summary".
_STATUS_LINE = ("\x1b[1;33m▌ imp\x1b[0m \x1b[36mplanning\x1b[0m · "
                "\x1b[2mstep 3 of 7 · writing the plan summary\x1b[0m")


def test_colored_fence_renders_on_one_line_at_visible_width() -> None:
    lines = rt._render_markdown_block(f"Look:\n\n```\n{_STATUS_LINE}\n```\n",
                                      70, 0, color=True)
    hits = [ln for ln in lines if "planning" in _visible(ln)]
    assert len(hits) == 1, lines          # 83 raw chars, 57 visible cols: no wrap
    assert _visible(hits[0]) == "  ▌ imp planning · step 3 of 7 · writing the plan summary"
    assert "\x1b[1;33m" in hits[0] and "\x1b[36m" in hits[0]


def test_every_line_of_a_colored_fence_ends_with_a_reset() -> None:
    lines = rt._render_markdown_block("Look:\n\n```\n\x1b[31mone\n\x1b[32mtwo\n```\n",
                                      70, 0, color=True)
    body = [ln for ln in lines if _visible(ln).strip() in ("one", "two")]
    assert len(body) == 2, lines
    assert all(ln.endswith("\x1b[0m") for ln in body)   # no color bleeds past the block


def test_color_off_strips_sgr_from_a_colored_fence() -> None:
    out = "\n".join(rt._render_markdown_block(f"Look:\n\n```\n{_STATUS_LINE}\n```\n",
                                              70, 0, color=False))
    assert "\x1b" not in out
    assert "▌ imp planning · step 3 of 7 · writing the plan summary" in out


def test_two_colored_fences_both_splice_in_source_order() -> None:
    text = ("one:\n\n```\n\x1b[31mAAA\x1b[0m\n```\n\nmid\n\n"
            "```\n\x1b[32mBBB\x1b[0m\n```\n\ntail\n")
    visible = [_visible(ln).strip()
               for ln in rt._render_markdown_block(text, 70, 0, color=True)]
    assert [v for v in visible if v] == ["• one:", "AAA", "mid", "BBB", "tail"]


def test_content_carrying_the_slot_unit_renders_faithfully() -> None:
    # The splice replaces any rendered line holding a slot key, so a slot key must be
    # unforgeable by content — and the lift must not "sanitize" the text to get there,
    # which would both destroy a codepoint and desync the two fence scans.
    text = (f"before {rt._SLOT_UNIT}0{rt._SLOT_UNIT} after\n\n"
            "```\n\x1b[31mCOLOR\x1b[0m\n```\n\ntail\n")
    visible = [_visible(ln).strip()
               for ln in rt._render_markdown_block(text, 70, 0, color=True)]
    assert [v for v in visible if v] == [
        f"• before {rt._SLOT_UNIT}0{rt._SLOT_UNIT} after", "COLOR", "tail"]


def test_a_private_use_glyph_in_a_mockup_survives() -> None:
    # U+E000 is the first Private Use codepoint — where terminal icon fonts (Nerd
    # Fonts) live, so a prompt mockup plausibly carries one. It is content, not chrome.
    lines = rt._render_markdown_block(
        "Look:\n\n```\n\x1b[36m zsh\x1b[0m ~/src\n```\n", 70, 0, color=True)
    assert any(" zsh" in ln for ln in lines), lines


def test_both_fence_scans_see_the_same_text() -> None:
    # Adversarial: deleting the slot unit from "``<unit>`" would complete a ``` run and
    # close the fence early, so a lift that "sanitized" the text would scan different
    # spans than the escape pass did — stranding the colored "B" outside a fence and
    # handing rich a raw escape. That surfaces as an ESC byte in color-off output,
    # which AC 6 forbids.
    text = (f"```\n\x1b[31mA\x1b[0m\n``{rt._SLOT_UNIT}`\n"
            "\x1b[32mB\x1b[0m\n```\n")
    assert "\x1b" not in "\n".join(
        rt._render_markdown_block(rt._caret_escape_prose(text), 70, 0, color=False))


def test_colored_span_ranges_starts_the_sample_at_the_first_bare_code() -> None:
    # The sample runs from the FIRST bare code to the end of the interior, so leading
    # uncolored text stays real text and keeps rich's cyan.
    ln = "Shows `load [1;33m78%[0m` now."
    (span,) = rt._colored_span_ranges([ln])
    assert span.line == 0
    assert ln[span.start:span.end] == "[1;33m78%[0m"


def test_colored_span_ranges_skips_spans_with_no_bare_code_and_fence_bodies() -> None:
    assert rt._colored_span_ranges(["a `--full` b"]) == []
    assert rt._colored_span_ranges(["a `\x1b[31mred\x1b[0m` b"]) == []      # a real ESC
    assert rt._colored_span_ranges(["a `^[[31mred^[[0m` b"]) == []          # already defused
    # A fence body is the fence lift's business, not the span lift's.
    assert rt._colored_span_ranges(["```", "inline `[1;33m78%[0m` here", "```"]) == []


def test_colored_span_ranges_skips_multiline_and_backtick_interior_spans() -> None:
    assert rt._colored_span_ranges(["a `[1;33m78%", "[0m` b"]) == []
    assert rt._colored_span_ranges(["a ``[1;33m78`%[0m`` b"]) == []


def test_colored_span_ranges_skips_single_backtick_nested_in_double_backtick_span() -> None:
    # A single-backtick pair that is interior to a double-backtick span must not be
    # treated as an eligible span — the outer span's content is literal by the spec.
    assert rt._colored_span_ranges(["a ``x `[31my[0m` z`` b"]) == []


def test_nested_single_backtick_in_double_backtick_span_renders_codes_literally() -> None:
    # End-to-end: the bare codes appear as visible text, no real ESC sequence emitted.
    out = rt._markdown_to_ansi("a ``x `[31my[0m` z`` b", 60, True)
    assert "[31m" in rt._strip_sgr(out)
    assert "\x1b[31m" not in out


def test_colored_span_ranges_skips_a_zero_visible_width_sample() -> None:
    # An all-codes interior would lift to an empty run: the code span collapses and no
    # post-render check can catch it, so it is ineligible up front.
    assert rt._colored_span_ranges(["Write `[1;33m`, `[0m` — and imp restores it."]) == []


def test_colored_span_ranges_returns_every_span_in_source_order() -> None:
    lines = ["A `[31mone[0m` and `[32mtwo[0m`.", "B `[33mthree[0m` end."]
    assert [(s.line, lines[s.line][s.start:s.end])
            for s in rt._colored_span_ranges(lines)] == [
        (0, "[31mone[0m"), (0, "[32mtwo[0m"), (1, "[33mthree[0m")]


def test_span_cells_measures_visible_cells_not_bytes() -> None:
    assert rt._span_cells("[1;33m78%[0m") == 3
    assert rt._span_cells("[1;33m世界[0m") == 4      # wide glyphs are two cells each
    assert rt._span_cells("[0m") == 0


def test_restore_bare_sgr_in_spans_arms_only_the_sample_region() -> None:
    armed = rt._restore_bare_sgr_in_spans("The gauge reads `load [1;33m78%[0m` amber.")
    # Arming starts at the FIRST bare code, so the leading `load ` stays plain text.
    assert armed == "The gauge reads `load \x1b[1;33m78%\x1b[0m` amber."
    # What the export does next: the codes vanish, the span keeps its visible text.
    assert rt._strip_sgr(armed) == "The gauge reads `load 78%` amber."


def test_restore_bare_sgr_in_spans_arms_every_span_on_one_line() -> None:
    # Right-to-left rewriting: arming the first span must not shift the second's offsets.
    assert rt._restore_bare_sgr_in_spans("a `[1;33m78%[0m` and `[32mok[0m` z") == (
        "a `\x1b[1;33m78%\x1b[0m` and `\x1b[32mok\x1b[0m` z")


def test_restore_bare_sgr_in_spans_leaves_every_ineligible_span_byte_for_byte() -> None:
    # An armed all-codes span would strip to empty backticks, deleting the codes the
    # sentence is about; a fence body is `_restore_bare_sgr`'s job; the rest are not
    # spans this pass handles, and a model id is not a code at all.
    for src in ("Write `[1;33m`, `[0m` — and imp restores it.",
                "```\ninline `[1;33m78%[0m` here\n```",
                "a `[1;33m78%\n[0m` b",
                "a `\x1b[31mred\x1b[0m` b",
                "high: `claude-opus-4-8[1m]` ok"):
        assert rt._restore_bare_sgr_in_spans(src) == src


def test_lift_colored_spans_swaps_the_sample_for_a_width_faithful_run() -> None:
    text, lifted = rt._lift_colored_spans("The gauge reads `[1;33m78%[0m` amber.")
    assert text == "The gauge reads `" + rt._SPAN_UNIT * 3 + "` amber."
    assert lifted == [(rt._SPAN_UNIT * 3, "\x1b[1;33m78%\x1b[0m\x1b[0m")]


def test_lift_colored_spans_is_skipped_when_content_carries_the_sentinel() -> None:
    # An order-based splice cannot tell a content-origin run from ours, so the whole
    # render gives up the lift rather than risk a wrong-but-intact match.
    src = "Odd " + rt._SPAN_UNIT + " char and `[1;33m78%[0m` here."
    assert rt._lift_colored_spans(src) == (src, [])


def test_lift_colored_spans_records_multiple_samples_in_source_order() -> None:
    text, lifted = rt._lift_colored_spans("A `[31mone[0m` and B `[32mtwo[0m` end.")
    assert text == ("A `" + rt._SPAN_UNIT * 3 + "` and B `"
                    + rt._SPAN_UNIT * 3 + "` end.")
    assert [s for _, s in lifted] == ["\x1b[31mone\x1b[0m\x1b[0m",
                                      "\x1b[32mtwo\x1b[0m\x1b[0m"]


def test_lift_colored_spans_skips_a_sample_carrying_a_fence_slot_unit() -> None:
    # `_slot_prefix` picks the shortest `_SLOT_UNIT` run absent from the text it is
    # handed, and it is handed the POST-span-lift text — so a U+FFFF hidden inside a
    # lifted sample would be invisible to it, and the spliced sample would FORGE a fence
    # slot key. Leaving such a span alone keeps every U+FFFF in view of `_slot_prefix`.
    src = "a `[31m" + rt._SLOT_UNIT + "0" + rt._SLOT_UNIT + "[0m` b"
    assert rt._lift_colored_spans(src) == (src, [])
    # Per-span, not per-render: a clean span beside it still lifts.
    text, lifted = rt._lift_colored_spans(
        "a `[31m" + rt._SLOT_UNIT + "[0m` and b `[32mok[0m` c")
    assert [s for _, s in lifted] == ["\x1b[32mok\x1b[0m\x1b[0m"]
    assert rt._SLOT_UNIT in text        # left in place, so `_slot_prefix` still sees it


def test_splice_lifted_spans_restores_each_sample_at_its_run() -> None:
    lifted = [(rt._SPAN_UNIT * 3, "\x1b[1;33m78%\x1b[0m"),
              (rt._SPAN_UNIT * 2, "\x1b[32mok\x1b[0m")]
    rendered = f"a {rt._SPAN_UNIT * 3} b {rt._SPAN_UNIT * 2} c"
    assert rt._splice_lifted_spans(rendered, lifted) == \
        "a \x1b[1;33m78%\x1b[0m b \x1b[32mok\x1b[0m c"


def test_splice_lifted_spans_refuses_a_folded_welded_or_lost_run() -> None:
    lifted = [(rt._SPAN_UNIT * 4, "\x1b[1;33mWARN\x1b[0m")]
    u = rt._SPAN_UNIT
    assert rt._splice_lifted_spans(f"a {u * 2}\n{u * 2} b", lifted) is None   # folded
    assert rt._splice_lifted_spans(f"a {u * 5} b", lifted) is None            # welded
    assert rt._splice_lifted_spans("a b", lifted) is None                     # lost


def test_inline_span_renders_the_sample_in_real_color() -> None:
    out = rt._markdown_to_ansi("The gauge reads `[1;33m78%[0m` amber.", 40, True)
    assert "\x1b[1;33m78%\x1b[0m" in out            # a real sequence around the text
    assert "[1;33m" not in _visible(out)            # nothing literal left on screen


def test_inline_span_leading_text_keeps_richs_cyan() -> None:
    # The placeholder covers only from the first code onward, so "load " is still real
    # text: rich styles it `markdown.code` cyan and can break after it.
    out = rt._markdown_to_ansi("Shows `load [1;33m78%[0m` now.", 40, True)
    assert "\x1b[36mload \x1b[1;33m78%\x1b[0m" in out
    # And it keeps its break opportunity: at width 32 rich breaks between "load" and
    # the sample. Were "load " swallowed into the placeholder, the run would offer no
    # internal break and the whole "load 78%" would move to the second line — so this
    # discriminates, where the same check at width 40 renders identically either way.
    wrapped = rt._markdown_to_ansi(
        "A sentence long enough that `load [1;33m78%[0m` lands here.", 32, True)
    assert [_visible(ln).rstrip() for ln in wrapped.rstrip("\n").split("\n")] == [
        "A sentence long enough that load",
        "78% lands here."]


def test_text_after_the_last_code_rides_inside_the_sample() -> None:
    out = rt._markdown_to_ansi("Shows `[1;33m78%[0m done` now.", 40, True)
    assert "\x1b[1;33m78%\x1b[0m done\x1b[0m" in out
    assert "\x1b[36mdone" not in out                # not rich's cyan — the sample owns it
    # Inside the placeholder, so unlike "load " above it never breaks away.
    wrapped = rt._markdown_to_ansi("A sentence long enough that `[1;33m78%[0m done`"
                                   " lands at the edge.", 40, True)
    assert any("78% done" in _visible(ln) for ln in wrapped.split("\n"))
    assert max(len(_visible(ln)) for ln in wrapped.split("\n")) <= 40


def test_a_paragraph_mixing_spans_wraps_at_visible_width() -> None:
    src = "Use `--full` to see `[1;33m78%[0m` and more text here to wrap the line nicely."
    out = rt._markdown_to_ansi(src, 40, True)
    assert "\x1b[36m--full\x1b[0m" in out           # the plain span is still cyan
    assert max(len(_visible(ln)) for ln in out.split("\n")) <= 40


def test_color_off_wrapping_is_byte_identical_to_the_codes_removed() -> None:
    src = "The gauge reads `[1;33m78%[0m` amber and then more words to force a wrap."
    assert (rt._markdown_to_ansi(src, 40, False)
            == rt._markdown_to_ansi(src.replace("[1;33m", "").replace("[0m", ""),
                                    40, False))


def test_color_off_wrapping_stays_identical_to_the_codes_removed_for_any_sample() -> None:
    for src in ("The state `[1;33mWARN: high[0m` matters a lot here and wraps somewhere later.",
                "x `[1;33m" + "W" * 60 + "[0m` y"):
        assert (rt._markdown_to_ansi(src, 30, False)
                == rt._markdown_to_ansi(src.replace("[1;33m", "").replace("[0m", ""), 30, False))


def test_a_sample_with_a_space_is_never_split_across_lines() -> None:
    # A placeholder run offers rich no internal break opportunity, so a colored sample
    # is kept whole rather than wrapped mid-sequence.
    src = "The state `[1;33mWARN: high[0m` matters a lot here and wraps somewhere later."
    out = rt._markdown_to_ansi(src, 40, True)
    assert any("WARN: high" in _visible(ln) for ln in out.split("\n"))
    assert max(len(_visible(ln)) for ln in out.split("\n")) <= 40


def test_multiple_spans_splice_back_in_source_order_each_ending_with_a_reset() -> None:
    out = rt._markdown_to_ansi("A `[31mone[0m` and B `[32mtwo[0m` end.", 60, True)
    assert _re.findall(r"\x1b\[3([12])m([a-z]+)\x1b\[0m", out) == [("1", "one"),
                                                                   ("2", "two")]


def test_color_off_renders_the_visible_sample_with_no_escape() -> None:
    out = rt._markdown_to_ansi("The gauge reads `[1;33m78%[0m` amber.", 40, False)
    assert "78%" in out
    assert "\x1b" not in out and "1;33" not in out


def test_color_off_strips_a_sample_too_wide_for_the_line() -> None:
    # 58 cells at width 58: the delimiters alone push the unbreakable token over.
    for n, width in ((60, 40), (58, 58)):
        out = rt._markdown_to_ansi("x `[1;33m" + "W" * n + "[0m` y", width, False)
        assert "[1;33m" not in out and "[0m" not in out
        assert "W" * n in "".join(ln.strip() for ln in out.split("\n"))


def test_color_off_strips_codes_when_content_carries_a_sentinel() -> None:
    span_unit = rt._markdown_to_ansi(
        "Odd " + rt._SPAN_UNIT + " char and `[1;33m78%[0m` here.", 44, False)
    assert "`78%`" in span_unit and rt._SPAN_UNIT in span_unit
    slot_unit = rt._markdown_to_ansi("a `[31m" + rt._SLOT_UNIT + "x[0m` b", 44, False)
    assert "[31m" not in slot_unit and "[0m" not in slot_unit
    assert rt._SLOT_UNIT in slot_unit


def test_color_off_slot_unit_in_a_span_never_forges_a_fence_slot_key() -> None:
    src = ("a `[31m" + rt._SLOT_UNIT + "0" + rt._SLOT_UNIT + "[0m` b\n\n"
           "```\n[32mmock[0m\n```\n")
    out = rt._markdown_to_ansi(rt._caret_escape_prose(src), 60, False)
    assert out.count("mock") == 1
    assert rt._SLOT_UNIT in out and "[31m" not in out


def test_an_oversize_sample_falls_back_to_a_literal_render() -> None:
    # Wider than the render width: rich folds the run, the splice finds no anchor, and
    # the whole render is redone with the lift off.
    out = rt._markdown_to_ansi("x `[1;33m" + "W" * 60 + "[0m` y", 40, True)
    assert "[1;33m" in _visible(out)
    assert max(len(_visible(ln)) for ln in out.split("\n")) <= 40
    assert rt._SPAN_UNIT not in out and rt._SLOT_UNIT not in out


def test_a_short_sample_welded_into_a_long_token_falls_back() -> None:
    # A genuinely distinct mechanism from the oversize case: this sample is only 4 cells
    # at width 40, but it sits inside an unbreakable 80-column token, so rich folds the
    # run 3+1 and the splice loses its anchor. (Punctuation alone does not weld — rich
    # breaks before a `(`, so `see (`…`), ok` splices fine until the sample itself
    # exceeds the width, which is just the oversize case again.)
    out = rt._markdown_to_ansi(
        "see " + "x" * 36 + "(`[1;33mWWWW[0m`)" + "y" * 36 + " ok", 40, True)
    assert "\x1b[1;33m" not in out                  # not colored
    # The fallback render breaks the literal code across the fold too, so compare
    # against the lines rejoined.
    assert "[1;33mWWWW[0m" in "".join(_visible(ln).strip() for ln in out.split("\n"))
    assert max(len(_visible(ln)) for ln in out.split("\n")) <= 40
    assert rt._SPAN_UNIT not in out and rt._SLOT_UNIT not in out


def test_a_slot_unit_in_a_span_never_forges_a_fence_slot_key() -> None:
    # End-to-end guard for the Task 2 skip: without it the spliced sample reproduces the
    # fence slot key, `_splice_lifted_fences` matches it on the prose line, and the prose
    # line is REPLACED by the fence body — which is then emitted a second time at the
    # real slot. Silent content loss plus duplicated output.
    src = ("a `[31m" + rt._SLOT_UNIT + "0" + rt._SLOT_UNIT + "[0m` b\n\n"
           "```\n[32mmock[0m\n```\n")
    out = rt._markdown_to_ansi(rt._caret_escape_prose(src), 60, True)
    assert out.count("\x1b[32mmock\x1b[0m") == 1    # fence body emitted exactly once
    assert rt._SLOT_UNIT in _visible(out)           # the prose line survives, verbatim
    assert "\x1b[31m" not in out                    # that span stays literal


def test_an_indented_code_block_is_prose_for_the_span_lift() -> None:
    # `_caret_escape_prose` already treats a four-space indented block as prose, not a
    # fence (`test_caret_escape_prose_treats_an_indented_code_block_as_prose`), so an
    # eligible span on such a line is armed like any other prose span. `_fence_spans`
    # stays the one definition of "inside a fence" and models fences only.
    out = rt._markdown_to_ansi("text:\n\n    inline `[1;33m78%[0m` here\n", 60, True)
    assert "`\x1b[1;33m78%\x1b[0m" in out           # backticks literal, sample colored


def test_agent_prose_inline_span_reaches_the_screen_in_color() -> None:
    out = "\n".join(_agent_lines("The gauge reads `[1;33m78%[0m` amber.", color=True))
    assert "\x1b[1;33m78%\x1b[0m" in out
    assert "^[[1;33m" not in out                    # nor caret noise


def test_literal_text_node_span_is_not_armed_or_colored() -> None:
    # Hook-script stdout is content-origin and escaped absolutely, so it gets neither
    # fence nor span color — the asymmetry of one without the other has no explanation.
    node = nv.LiteralTextNode(
        text="hook says `[1;33m78%[0m` here.\n\n```\n[1;33mmock[0m\n```\n")
    out = "\n".join(rt._render(node, depth=0, full=False, color=True, width=80))
    assert "[1;33m78%[0m" in _visible(out)          # the span: literal, uncolored
    # The fence half of the same guarantee: `arm_spans` gates only the span branch, and
    # the fence lift stays inert here because `_caret_escape` leaves it no real ESC.
    assert "[1;33mmock[0m" in _visible(out)
    assert "\x1b[1;33m" not in out                  # nothing armed anywhere on this path


def test_literal_text_node_span_stays_literal_with_color_off() -> None:
    # Hook-script stdout keeps what it printed: the color-off strip is for armed prose only.
    node = nv.LiteralTextNode(text="hook says `[1;33m78%[0m` here.")
    out = "\n".join(rt._render(node, depth=0, full=False, color=False, width=80))
    assert "[1;33m78%[0m" in out


def test_a_real_escape_inside_a_span_stays_caret_escaped() -> None:
    out = rt._markdown_to_ansi(
        rt._caret_escape_prose("A span `\x1b[31mred\x1b[0m` here."), 40, True)
    assert "^[[31mred^[[0m" in out
    assert "\x1b[31m" not in out


def test_non_sgr_control_bytes_inside_an_eligible_span_stay_in_caret_notation() -> None:
    src = "A span `[1;33m78%\x07\x1b[5A\x1b]0;t\x07[0m` here."
    out = rt._markdown_to_ansi(rt._caret_escape_prose(src), 60, True)
    assert "\x1b[1;33m78%^G^[[5A^[]0;t^G\x1b[0m" in out
    assert "\x07" not in out


def test_a_span_split_across_lines_renders_literally() -> None:
    out = rt._markdown_to_ansi("A span `[1;33m78%\n[0m` here.", 40, True)
    assert "[1;33m" in _visible(out)
    assert "\x1b[1;33m" not in out


def test_a_span_whose_interior_holds_a_backtick_renders_literally() -> None:
    out = rt._markdown_to_ansi("A span ``[1;33m78`%[0m`` here.", 44, True)
    assert "[1;33m" in _visible(out)
    assert "\x1b[1;33m" not in out


def test_content_carrying_the_span_unit_disables_the_lift_for_the_render() -> None:
    out = rt._markdown_to_ansi(
        "Odd " + rt._SPAN_UNIT + " char and `[1;33m78%[0m` here.", 44, True)
    assert rt._SPAN_UNIT in _visible(out)           # the codepoint is content, kept
    assert "[1;33m" in _visible(out)
    assert "\x1b[1;33m" not in out


def test_an_all_codes_span_still_renders_as_two_cyan_spans() -> None:
    # The ordinary way prose discusses SGR. Lifting it would collapse the two spans into
    # one and drop both codes from the output entirely.
    src = "Write each SGR code without it — `[1;33m`, `[0m` — and imp restores it."
    out = rt._markdown_to_ansi(src, 76, True)
    assert out.count("\x1b[36m") == 2
    assert "[1;33m" in _visible(out) and "[0m" in _visible(out)
    assert "\x1b[36m\x1b[0m" not in out             # no empty code span


def test_a_colored_span_inside_a_fence_is_left_to_the_fence_path() -> None:
    src = "Look:\n\n```\ninline `[1;33m78%[0m` here\n```\n"
    out = rt._markdown_to_ansi(rt._caret_escape_prose(src), 60, True)
    assert "inline `\x1b[1;33m78%\x1b[0m` here" in out   # backticks intact, fence colored


def test_a_colored_fence_and_a_colored_span_both_render() -> None:
    src = "a `[31mx[0m` b\n\n```\n[32mmock[0m\n```\n"
    out = rt._markdown_to_ansi(rt._caret_escape_prose(src), 60, True)
    assert "\x1b[31mx\x1b[0m" in out and "\x1b[32mmock\x1b[0m" in out
    assert rt._SLOT_UNIT not in out and rt._SPAN_UNIT not in out


def _agent_lines(text: str, *, color: bool, width: int = 80) -> list[str]:
    tree = nv.build_tree([
        _ev("2", "step_started", step="s", kind="agent", phase="p",
            model="m", provider="anthropic"),
        _ev("3", "agent_message", agent_id="step:p/s", provider="anthropic", model="m",
            stop_reason="end_turn", usage={},
            raw={"content": [{"type": "text", "text": text}]}),
    ])
    return list(render_tree(tree, full=True, color=color, width=width))


def test_agent_prose_fence_reaches_the_screen_in_color() -> None:
    out = "\n".join(_agent_lines(
        "Look:\n\n```\n\x1b[1;33m▌ imp\x1b[0m done\n```\n", color=True))
    assert "\x1b[1;33m▌ imp\x1b[0m done" in out
    assert "^[[1;33m" not in out


def test_agent_prose_carets_every_control_byte_that_is_not_fenced_sgr() -> None:
    out = "\n".join(_agent_lines(
        "Look:\n\n```\n\x1b[31mred\x1b[0m \x07 \x1b[2J \x1b[5A \x1b]0;title\x07\n```\n\n"
        "and \x1b[32mgreen\x1b[0m in prose\n", color=True))
    assert "\x1b[31mred\x1b[0m" in out                    # fenced SGR survives
    assert "^G" in out and "^[[2J" in out and "^[[5A" in out
    assert "^[]0;title^G" in out
    assert "\x07" not in out
    assert "^[[32mgreen^[[0m" in out                      # outside a fence: caret
    assert "\x1b[32m" not in out


def test_agent_prose_tilde_fence_matches_backtick_and_indented_code_carets() -> None:
    tilde = _agent_lines(f"Look:\n\n~~~\n{_STATUS_LINE}\n~~~\n", color=True, width=70)
    hits = [ln for ln in tilde if "planning" in _visible(ln)]
    assert len(hits) == 1                                 # same width accounting
    assert "\x1b[1;33m▌ imp\x1b[0m" in hits[0]
    indented = "\n".join(_agent_lines("Look:\n\n    \x1b[31mred\x1b[0m\n", color=True))
    assert "^[[31mred^[[0m" in indented                   # indented block: no exception


def test_agent_prose_with_color_off_emits_no_escape_at_all() -> None:
    out = "\n".join(_agent_lines(f"Look:\n\n```\n{_STATUS_LINE}\n```\n", color=False))
    assert "\x1b" not in out
    assert "▌ imp planning · step 3 of 7 · writing the plan summary" in out


def test_agent_prose_bare_sgr_mockup_reaches_the_screen_in_color() -> None:
    out = "\n".join(_agent_lines(f"Look:\n\n```\n{_BARE_MOCKUP}\n```\n",
                                 color=True, width=120))
    assert "\x1b[38;2;255;154;60m" in out            # a real, armed sequence
    assert "[38;2" not in _visible(out)              # and no bare code on screen
    assert "^[[" not in out                          # nor caret noise
    assert "warning: [auth] retry 2 (1.0s)" in _visible(out)


def test_agent_prose_bare_sgr_mockup_with_color_off_strips_the_codes() -> None:
    out = "\n".join(_agent_lines(f"Look:\n\n```\n{_BARE_MOCKUP}\n```\n",
                                 color=False, width=120))
    assert "\x1b" not in out
    assert "38;2" not in out
    assert "^[[" not in out
    assert "warning: [auth] retry 2 (1.0s)" in out


def test_a_leading_code_span_does_not_strand_a_later_mockup() -> None:
    # "```x``` explained" is a paragraph, not a fence opener. If it were treated as
    # one, every later span shifts and the real mockup below renders as caret noise.
    out = "\n".join(_agent_lines(
        "```code``` explained\n\nHere:\n\n```\n\x1b[1;33m▌ imp\x1b[0m planning\n```\n",
        color=True))
    assert "\x1b[1;33m▌ imp\x1b[0m planning" in out
    assert "^[[" not in out
    assert "explained" in _visible(out)          # the paragraph's text is not swallowed


def test_literal_text_node_colored_fence_stays_caret_escaped() -> None:
    node = nv.LiteralTextNode(text="Look:\n\n```\n\x1b[1;33m▌ imp\x1b[0m\n```\n")
    out = "\n".join(rt._render(node, depth=0, full=False, color=True, width=80))
    assert "^[[1;33m" in out                              # literal text keeps absolute escaping
    assert "\x1b[1;33m" not in out


def test_conventions_doc_records_the_narrowed_caret_rule() -> None:
    text = Path("docs/internals/conventions.md").read_text()
    flat = " ".join(text.split())
    assert "## Color in agent prose" in text
    assert ("survives verbatim inside a fenced code block and inside a single-line "
            "inline code span") in flat
    # The doc must record that a model-typed bare CSI is armed, not merely that a
    # real ESC survives — and that a defused `^[` is exempt from the arming.
    assert "a bare `[1;33m` in either place has the escape restored first" in flat
    assert "never after a `^[`" in flat
    assert "any real escape outside a fence, still renders as `^[`" in flat
    assert "the `--md`/`--html` exports strip SGR entirely" in flat
    # The section names the primitive; a rename must not strand the reader.
    assert hasattr(rt, "_caret_escape")


def test_lifted_fence_is_not_dropped_when_slot_key_wraps() -> None:
    # A prose run of 25 U+FFFF nonchars makes the slot key 53 chars wide;
    # at width 40 rich wraps the placeholder line so no single rendered line
    # holds the whole key. The fallback must append the content at the end
    # rather than silently drop it.
    long_run = rt._SLOT_UNIT * 25
    text = f"intro {long_run} done\n\n```\n\x1b[1;33mmock\x1b[0m\n```\n"
    out = rt._markdown_to_ansi(text, 40, True)
    assert "\x1b[1;33mmock\x1b[0m" in out


def test_crlf_fence_keeps_sgr_and_has_no_bare_cr() -> None:
    # Lines that bypass rich must have non-SGR control bytes neutralized.
    # A CRLF agent message keeps \r on lifted lines; that bare CR rewrites
    # the TTY line when it reaches the terminal.
    out = rt._markdown_to_ansi(
        "intro\r\n```\r\n\x1b[1;33mmock\x1b[0m\r\n```\r\nafter\r\n", 40, True)
    assert "\x1b[1;33mmock\x1b[0m" in out
    assert "\r" not in out


def test_breadcrumb_renders_the_severity_tally_after_complete() -> None:
    # First pass: the tally, and no round segment.
    bc = nv.BreadcrumbLine(step_name="plan-reviewer", step_label="reviewing plan",
                           elapsed_s=72.0, action={"verb": "continue"},
                           round=1, counts=(1, 3, 2))
    assert "\n".join(rt._render(bc, depth=1, full=False, color=False, width=80)) \
        == "  reviewing plan · complete · 1 critical, 3 major, 2 minor · 1m 12s"


def test_breadcrumb_renders_the_round_before_the_tally_from_round_two() -> None:
    bc2 = nv.BreadcrumbLine(step_name="plan-reviewer", step_label="reviewing plan",
                            elapsed_s=58.0, action={"verb": "continue"},
                            round=2, counts=(0, 2, 1))
    assert "\n".join(rt._render(bc2, depth=1, full=False, color=False, width=80)) \
        == "  reviewing plan · complete · round 2 · 2 major, 1 minor · 58s"
    bc3 = nv.BreadcrumbLine(step_name="plan-reviewer", step_label="reviewing plan",
                            elapsed_s=44.0, action={"verb": "continue"},
                            round=3, counts=(0, 0, 1))
    assert "\n".join(rt._render(bc3, depth=1, full=False, color=False, width=80)) \
        == "  reviewing plan · complete · round 3 · 1 minor · 44s"


def test_breadcrumb_renders_an_all_zero_tally_as_clean() -> None:
    bc = nv.BreadcrumbLine(step_name="plan-reviewer", step_label="reviewing plan",
                           elapsed_s=31.0, action={"verb": "continue"},
                           round=4, counts=(0, 0, 0))
    assert "\n".join(rt._render(bc, depth=1, full=False, color=False, width=80)) \
        == "  reviewing plan · complete · round 4 · clean · 31s"


def test_the_segments_attach_after_the_interrupted_variant_too() -> None:
    # The leading part is `<name> interrupted` on this branch, so the placement
    # rule ("after the name part") must not assume the word `complete`.
    bc = nv.BreadcrumbLine(step_name="coder", step_label="writing code",
                           elapsed_s=9.0, action={"verb": "continue"},
                           round=2, interrupted=True)
    assert "\n".join(rt._render(bc, depth=1, full=False, color=False, width=80)) \
        == "  writing code interrupted · round 2 · 9s"


def test_full_view_shows_the_tally_and_not_the_raw_count_extras() -> None:
    out = "\n".join(_lines([
        ev("1", "quest_created", title="t"),
        ev("2", "step_started", step="plan-reviewer", kind="agent", phase="planning",
           label="reviewing plan", round=2, timestamp="2026-06-10T00:00:00"),
        ev("3", "agent_result", agent_id="step:planning/plan-reviewer",
           status="Success", critical=1, major=3, minor=2),
        ev("4", "step_completed", step="plan-reviewer", kind="agent",
           action={"verb": "continue"}, timestamp="2026-06-10T00:01:12"),
    ], full=True))
    assert ("reviewing plan · complete · round 2 · 1 critical, 3 major, 2 minor"
            " · status=Success · 1m 12s") in out
    assert "critical=1" not in out
    assert "major=3" not in out
    assert "minor=2" not in out


def test_collapsed_line_carries_the_round_and_is_unchanged_at_round_one() -> None:
    node = nv.StepNode(name="verify", kind="script", label="running tests",
                       phase_label="implementation phase · plan 2/6",
                       show_label=True, closed=True, round=2,
                       start_ts="2026-06-10T00:00:00", end_ts="2026-06-10T00:00:12")
    assert rt.collapsed_step_line(node, depth=1, color=False) == \
        "  implementation phase · plan 2/6 · running tests · complete · round 2 · 12s"
    node.round = 1
    assert rt.collapsed_step_line(node, depth=1, color=False) == \
        "  implementation phase · plan 2/6 · running tests · complete · 12s"


def test_the_verify_bounce_marks_verify_and_leaves_task_lead_unmarked() -> None:
    # loop_counts keys on the step that ISSUES the backward edge, so the round
    # lands on the next `verify` — and `verify` is body-less, so it must surface
    # on the collapsed line, which is what imp log actually prints for it.
    out = "\n".join(_lines([
        ev("1", "quest_created", title="t"),
        ev("2", "step_started", step="verify", kind="script", phase="implement",
           label="running tests", round=1, timestamp="2026-06-10T00:00:00"),
        ev("3", "step_completed", step="verify", kind="script",
           action={"verb": "goto", "target": "task-lead", "resume": True,
                   "rework": True},
           timestamp="2026-06-10T00:00:12"),
        ev("4", "step_started", step="task-lead", kind="agent", phase="implement",
           label="implementing", round=1, timestamp="2026-06-10T00:00:12"),
        ev("5", "step_completed", step="task-lead", kind="agent",
           action={"verb": "continue"}, timestamp="2026-06-10T00:00:20"),
        ev("6", "step_started", step="verify", kind="script", phase="implement",
           label="running tests", round=2, timestamp="2026-06-10T00:00:20"),
        ev("7", "step_completed", step="verify", kind="script",
           action={"verb": "continue"}, timestamp="2026-06-10T00:00:32"),
    ]))
    assert [ln.strip() for ln in out.splitlines() if "running tests" in ln] == [
        "running tests · complete · 12s",
        "running tests · complete · round 2 · 12s"]
    assert [ln.strip() for ln in out.splitlines() if "implementing" in ln] == [
        "implementing", "implementing · complete · 8s"]


def test_the_round_resets_per_plan_and_is_never_derived_from_entry_count() -> None:
    # Six entries of the same step: four under plan 1, two under plan 2. The
    # fifth reads no round at all — a renderer that counted entries would say
    # `round 5` there, which is the heuristic this quest rejects.
    events = [ev("0", "quest_created", title="t")]
    for i, (plan, rnd) in enumerate(
            [(1, 1), (1, 2), (1, 3), (1, 4), (2, 1), (2, 2)], start=1):
        events.append(ev(f"s{i}", "step_started", step="plan-reviewer", kind="agent",
                         phase="planning", label="reviewing plan",
                         phase_label=f"planning phase · plan {plan}/2", round=rnd,
                         timestamp="2026-06-10T00:00:00"))
        events.append(ev(f"c{i}", "step_completed", step="plan-reviewer", kind="agent",
                         action={"verb": "continue"},
                         timestamp="2026-06-10T00:00:05"))
    out = "\n".join(_lines(events))
    assert [ln.strip() for ln in out.splitlines()
            if "reviewing plan · complete" in ln] == [
        "planning phase · plan 1/2 · reviewing plan · complete · 5s",
        "planning phase · plan 1/2 · reviewing plan · complete · round 2 · 5s",
        "planning phase · plan 1/2 · reviewing plan · complete · round 3 · 5s",
        "planning phase · plan 1/2 · reviewing plan · complete · round 4 · 5s",
        "planning phase · plan 2/2 · reviewing plan · complete · 5s",
        "planning phase · plan 2/2 · reviewing plan · complete · round 2 · 5s"]
    assert "round 5" not in out


def test_step_header_node_renders_like_a_step_header() -> None:
    node = nv.StepHeaderNode(label="task 1/2", resumed=True,
                             phase_label="implementation phase · plan 1/1",
                             model="opus", route_label="fixing test failures")
    assert list(rt._render(node, depth=0, color=False, width=80)) == [
        "implementation phase · plan 1/1 · task 1/2 (resume) · opus · fixing test failures"]
    colored = next(rt._render(node, depth=0, color=True, width=80))
    assert pal.ansi(" (resume)", "gray") in colored
    # A route reason equal to the label is not repeated as a suffix.
    same = nv.StepHeaderNode(label="task 2/2", resumed=False, phase_label=None,
                             model=None, route_label="task 2/2")
    assert list(rt._render(same, depth=0, color=False, width=80)) == ["task 2/2"]


def test_step_header_node_is_spaced_like_a_header() -> None:
    header = nv.StepHeaderNode(label="task 2/2", resumed=False, phase_label=None,
                               model=None, route_label=None)
    children = [nv.OutcomeLine(action={"verb": "continue"}), header,
                nv.LiteralTextNode(text="after", color_role="lifecycle")]
    assert list(rt._render_children(children, depth=0, color=False, width=80)) == [
        "→ continue", "", "task 2/2", "", "after"]


def test_continuation_resume_header_carries_its_route_reason() -> None:
    lines = _lines([
        ev("1", "quest_created", quest_id=1, title="t"),
        task_lead_started("2", model="opus"),
        agent_text("3", "before"),
        ev("4", "workflow_paused", reason="interrupt", interrupt_reason="ctrl-c"),
        ev("5", "workflow_resumed"),
        task_lead_started("6", model="opus", resumed=True, restarted=False,
                          route_label="fixing test failures"),
        agent_text("7", "after"),
    ])
    assert lines == [
        "", "── implementation phase · plan 1/1 ───", "",
        "implementation phase · plan 1/1 · task 1/2 · opus", "",
        "• before", "",
        "implementation phase · plan 1/1 · task 1/2 (resume) · opus · fixing test failures",
        "", "• after"]


def test_hibernated_your_turn_replies_show_no_resume() -> None:
    lines = _lines([
        ev("1", "quest_created", quest_id=1, title="t"),
        task_lead_started("2"),
        agent_text("3", "question one?"),
        ev("4", "workflow_paused", reason="awaiting_user"),
        ev("5", "workflow_resumed"),
        task_lead_started("6", resumed=True, restarted=False),
        agent_text("7", "answer one, question two?"),
        ev("8", "workflow_paused", reason="awaiting_user"),
        ev("9", "workflow_resumed"),
        task_lead_started("10", resumed=True, restarted=False),
        agent_text("11", "answer two"),
    ])
    assert "• answer two" in lines
    assert not any("(resume)" in ln for ln in lines)
    assert [ln for ln in lines if "· task " in ln] == [
        "implementation phase · plan 1/1 · task 1/2 · m"]


def test_resume_and_position_headers_each_head_their_own_segment() -> None:
    assert _lines(interrupted_task_lead_events()) == [
        "", "── implementation phase · plan 1/1 ───", "",
        "implementation phase · plan 1/1 · task 1/2 · m", "",
        "• first segment", "",
        "implementation phase · plan 1/1 · task 1/2 (resume) · m", "",
        "• second segment", "",
        "implementation phase · plan 1/1 · task 2/2 · m", "",
        "• third segment", "",
        "implementation phase · plan 1/1 · task 2/2 · complete"]


def test_verb_push_keeps_the_entry_label_in_the_header() -> None:
    lines = _lines([
        ev("1", "quest_created", quest_id=1, title="x"),
        ev("2", "step_started", step="plan-writer", kind="agent", phase="plan",
           model="m", provider="anthropic", label="writing plan",
           phase_label="planning phase · plan 2/4"),
        ev("3", "step_label_updated", agent_id="step:plan/plan-writer",
           label="addressing plan review feedback"),
        agent_text("4", "done", agent_id="step:plan/plan-writer"),
        ev("5", "step_completed", step="plan-writer", kind="agent",
           action={"verb": "continue"}, var_mutations={}),
    ])
    assert "planning phase · plan 2/4 · writing plan · m" in lines
    # The breadcrumb still carries the most recent label.
    assert "planning phase · plan 2/4 · addressing plan review feedback · complete" in lines


def test_reopen_restart_reseeds_the_own_header() -> None:
    lines = _lines([
        ev("1", "quest_created", quest_id=1, title="t"),
        task_lead_started("2"),
        agent_text("3", "first segment"),
        ev("4", "step_label_updated", agent_id="step:code/task-lead", label="task 2/2"),
        ev("5", "workflow_paused", reason="interrupt", interrupt_reason="ctrl-c"),
        ev("6", "workflow_resumed"),
        task_lead_started("7", label="task 2/2", resumed=True, restarted=True),
        agent_text("8", "again"),
    ])
    assert lines == [
        "", "── implementation phase · plan 1/1 ───", "",
        "implementation phase · plan 1/1 · task 2/2 (resume) · m", "",
        "• again"]


@pytest.mark.parametrize("name", INTERNAL_TOOLS)
def test_burst_summary_times_only_visible_calls(name: str) -> None:
    burst = nv.ToolBurstFold(calls=[
        nv.ToolCall(call_id="1", name="write_file", input={},
                    call_ts=_bat(0), result_ts=_bat(10)),
        nv.ToolCall(call_id="2", name=name, input={},
                    call_ts=_bat(10), result_ts=_bat(20)),
        nv.ToolCall(call_id="3", name="bash", input={},
                    call_ts=_bat(20), result_ts=_bat(25)),
    ])
    # The internal call's 10s interval is neither counted nor timed.
    assert rt._burst_summary(burst) == "2 calls · write_file · bash · 15s"


@pytest.mark.parametrize("name", INTERNAL_TOOLS)
def test_burst_summary_internal_only_burst_is_none(name: str) -> None:
    burst = nv.ToolBurstFold(calls=[
        nv.ToolCall(call_id="1", name=name, input={}, call_ts=_bat(0), result_ts=_bat(10))])
    assert rt._burst_summary(burst) is None


def test_burst_summary_counts_work_tools_and_not_the_omit_set() -> None:
    burst = nv.ToolBurstFold(calls=[
        nv.ToolCall(call_id=str(i), name=name, input={})
        for i, name in enumerate(("bash_output", "bash_kill", "load_skill", "tool",
                                  "update_step_label"))
    ])
    assert rt._burst_summary(burst) == "2 calls · bash_output · bash_kill"


def test_terminal_tools_views_keep_complete_step_lines() -> None:
    events = [
        _ev("1", "step_started", step="rev", kind="agent", phase="review",
            model="m", provider="anthropic"),
        _ev("2", "tool_call", agent_id="step:review/rev", id="b1", name="complete_step",
            input={"status": "Success", "findings": "line one\nline two"}),
        _ev("3", "tool_result", agent_id="step:review/rev", id="b1", content="ok",
            error=False),
        _ev("4", "agent_result", agent_id="step:review/rev", status="Success",
            findings="line one\nline two"),
        _ev("5", "step_completed", step="rev", kind="agent",
            action={"verb": "continue"}, var_mutations={}),
    ]
    full = "\n".join(_lines(events, full=True))
    shown = "\n".join(render_tree(nv.build_tree(events), show=frozenset({"tools"}),
                                  color=False, width=80))
    assert "· complete_step  status=Success" in full
    assert "▾ complete_step" in full
    assert "· complete_step  status=Success" in shown


_MD_TABLE = "| a | b |\n|---|---|\n| c | d |"
_MD_SAMPLE = (
    "# one\n\n## two\n\n### three\n\n#### four\n\n##### five\n\n###### six\n\n"
    "Prose with `code`, a [site](https://example.com) and <kbd>K</kbd>.\n\n"
    "1. first\n2. second\n\n- bullet\n\n"
    f"{_MD_TABLE}\n\n> quoted\n\n---\n\n"
    "```python\n# note\ndef f():\n    return 1\n```\n\n```\nplain\n```\n")
_SGR_PARAMS_RE = _re.compile(r"\x1b\[([0-9;]*)m")


def _md_under(theme: str, text: str, *, width: int = 80, color: bool = True) -> str:
    pal.set_theme(theme)
    try:
        return rt._markdown_to_ansi(text, width, color, display_width=24)
    finally:
        pal.set_theme("ansi")


def _named_color_codes(ansi: str) -> list[int]:
    named: list[int] = []
    for match in _SGR_PARAMS_RE.finditer(ansi):
        params = [int(p or 0) for p in match.group(1).split(";")]
        i = 0
        while i < len(params):
            if params[i] in (38, 48):
                i += 5 if params[i + 1] == 2 else 3
                continue
            if params[i] in (*range(30, 38), *range(90, 98),
                             *range(40, 48), *range(100, 108)):
                named.append(params[i])
            i += 1
    return named


def test_named_color_codes_skips_truecolor_operands() -> None:
    assert _named_color_codes(
        "\x1b[36m\x1b[1;97;49m\x1b[100m\x1b[4;38;2;30;68;126m\x1b[48;2;97;40;100m"
    ) == [36, 97, 100]


def test_light_markdown_emits_each_palette_escape() -> None:
    for source, escape in (
        ("`code`", "\x1b[38;2;42;115;112mcode\x1b[0m"),
        ("# one", "\x1b[1;38;2;42;115;112mone\x1b[0m"),
        ("## one", "\x1b[1;38;2;42;115;112mone\x1b[0m"),
        ("### one", "\x1b[1;38;2;42;115;112mone\x1b[0m"),
        ("#### one", "\x1b[1;38;2;42;115;112mone\x1b[0m"),
        ("##### one", "\x1b[1;38;2;42;115;112mone\x1b[0m"),
        ("###### one", "\x1b[1;38;2;42;115;112mone\x1b[0m"),
        ("1. first", "\x1b[38;2;42;115;112m 1 \x1b[0m"),
        (_MD_TABLE, "\x1b[38;2;42;115;112ma\x1b[0m"),
        (_MD_TABLE, "\x1b[38;2;42;115;112m ──── \x1b[0m"),
        ("[site](https://example.com)", "\x1b[4;38;2;30;68;126msite\x1b[0m"),
        ("> quoted", "\x1b[38;2;122;73;150mquoted\x1b[0m"),
        ("press <kbd>K</kbd> now", "\x1b[1;38;2;154;106;42mK\x1b[0m"),
        ("---", "\x1b[38;2;102;106;102m" + "─" * 24 + "\x1b[0m"),
    ):
        assert escape in _md_under("light", source), source


def test_light_markdown_emits_no_named_colors() -> None:
    out = _md_under("light", _MD_SAMPLE)
    assert _named_color_codes(out) == []
    assert "38;2;169;13;145" in out       # xcode keyword
    assert "38;2;23;117;0" in out         # xcode comment
    assert "97;49" not in out             # monokai's near-invisible plain text


_MD_DIFF = "```diff\n@@ -1 +1 @@\n context\n-removed\n+added\n```\n"


def test_light_diff_fence_colors_from_the_palette() -> None:
    out = _md_under("light", _MD_DIFF)
    assert "\x1b[38;2;168;59;56;49m-removed\x1b[0m" in out
    assert "\x1b[38;2;85;111;34;49m+added\x1b[0m" in out
    assert "\x1b[38;2;102;106;102;49m@@ -1 +1 @@\x1b[0m" in out
    assert _named_color_codes(out) == []


def test_dark_and_ansi_diff_fences_keep_monokai() -> None:
    for theme in ("dark", "ansi"):
        out = _md_under(theme, _MD_DIFF)
        assert "\x1b[91;49m-removed\x1b[0m" in out
        assert "\x1b[92;49m+added\x1b[0m" in out


def test_dark_and_ansi_markdown_keep_the_16_color_look() -> None:
    for theme in ("dark", "ansi"):
        out = _md_under(theme, _MD_SAMPLE)
        assert "\x1b[36mcode\x1b[0m" in out
        assert "\x1b[1;36mone\x1b[0m" in out
        assert "\x1b[90m" + "─" * 24 + "\x1b[0m" in out
        assert "\x1b[97;49mplain\x1b[0m" in out
        assert "38;2" not in out


def test_rule_color_follows_the_theme_at_one_width() -> None:
    for theme, escape in (("light", "\x1b[38;2;102;106;102m"), ("ansi", "\x1b[90m"),
                          ("light", "\x1b[38;2;102;106;102m")):
        assert escape + "─" * 24 in _md_under(theme, "---")


def test_light_markdown_color_off_has_no_escapes() -> None:
    assert "\x1b" not in _md_under("light", _MD_SAMPLE, color=False)


def test_light_markdown_keeps_model_typed_sgr_verbatim() -> None:
    span = _md_under("light", "Shows `[1;33m78%[0m` now.")
    assert "\x1b[1;33m78%\x1b[0m" in span
    fence = _md_under("light", rt._caret_escape_prose("Look:\n\n```\n[1;33mwarn[0m\n```\n"))
    assert "\x1b[1;33mwarn\x1b[0m" in fence


def test_light_markdown_wraps_where_ansi_does() -> None:
    para = ("Wrap this paragraph with `inline code`, **bold words** and enough "
            "ordinary text that it spans several lines.")
    light = _visible(_md_under("light", para, width=30))
    assert light == _visible(_md_under("ansi", para, width=30))
    assert light.count("\n") >= 3


_TITLE = "map ledger time totals"


def test_display_subagent_title_collapses_clips_and_escapes() -> None:
    assert rt.display_subagent_title("  map   the\nledger ") == "map the ledger"
    fifty = "abcdefghij" * 5
    assert rt.display_subagent_title(fifty) == fifty[:40] + "…"
    assert rt.display_subagent_title("x" * 40) == "x" * 40
    assert rt.display_subagent_title("map\x1b[31mred") == "map^[[31mred"
    # Clip before escaping: escaping first would cut the `^[` pair to a bare `^`.
    assert rt.display_subagent_title("a" * 39 + "\x1b[31m") == "a" * 39 + "^[…"
    assert rt.display_subagent_title(None) is None
    assert rt.display_subagent_title(" \n ") is None


def test_subagent_fold_carries_dispatch_title() -> None:
    def fold(title):
        b = nv.NarrativeBuilder()
        for e in titled_subagent_events(title, finished=False):
            b.add(e)
        return b.running_subagents()[0]

    assert fold(_TITLE).title == _TITLE
    assert fold(None).title is None


def test_titled_subagent_status_row() -> None:
    def rows(title, *, color=False):
        tree = nv.build_tree(titled_subagent_events(title))
        return [ln for ln in render_tree(tree, color=color, width=80) if "explorer" in ln]

    assert rows(_TITLE) == [f"  explorer · {_TITLE} · claude-sonnet-5-5 · 55s"]
    assert rows(None) == ["  explorer · claude-sonnet-5-5 · 55s"]
    # Title sits in default fg between the magenta name and the gray model.
    sub_c = pal.NODE_COLORS["subagent"]
    assert (pal.ansi("  explorer", *sub_c) + f" · {_TITLE}"
            + pal.ansi(" · claude-sonnet-5-5", "gray")) in rows(_TITLE, color=True)[0]
    escaped = rows("map\x1b[31mred")
    assert escaped == ["  explorer · map^[[31mred · claude-sonnet-5-5 · 55s"]
    assert "\x1b" not in escaped[0]


def test_full_subagent_header_shows_title_instead_of_brief() -> None:
    def full(title, *, color=False):
        tree = nv.build_tree(titled_subagent_events(title))
        return list(render_tree(tree, full=True, color=color, width=80))

    titled = full(_TITLE)
    assert f"▾ explorer · {_TITLE}" in titled
    assert "▾ explorer · go" not in titled
    assert "▾ explorer · go" in full(None)               # untitled keeps the brief clip
    header = pal.ansi("▾ explorer", *pal.NODE_COLORS["subagent"]) + f" · {_TITLE}"
    assert header in full(_TITLE, color=True)
    escaped = full("map\x1b[31mred")
    assert "▾ explorer · map^[[31mred" in escaped
    assert not any("\x1b" in ln for ln in escaped)
