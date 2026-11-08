# tests/test_narrative.py
"""Tests for the narrative-tree model (src/imp/cli/narrative.py)."""
from __future__ import annotations

import json

import pytest

from imp.cli import narrative as nv
from tests._event_fixtures import (
    INTERNAL_TOOLS,
    TASK_LEAD_AID,
    agent_text,
    ev,
    task_lead_started,
)


def test_node_dataclasses_construct_and_default_collapsible() -> None:
    # Non-collapsible structural nodes.
    wf = nv.WorkflowNode()
    assert wf.children == []
    assert wf.status == "running"
    assert wf.collapsible is False
    assert nv.PhaseNode(name="design").collapsible is False
    assert nv.StepNode(name="s").collapsible is False
    assert nv.AgentTextNode(payload={}).collapsible is False
    assert nv.UserMessageNode(text="hi").collapsible is False
    assert nv.ErrorNode(message="boom").collapsible is False
    assert nv.OutcomeLine(action="continue").collapsible is False
    assert nv.FallbackLine(event_type="x", payload={}).collapsible is False
    # Collapsible folds.
    assert nv.ThinkingFold(content="t").collapsible is True
    assert nv.ToolBurstFold().collapsible is True
    assert nv.StepContextNode(payload={}).collapsible is True


def test_build_tree_empty_yields_root_only() -> None:
    tree = nv.build_tree([])
    assert isinstance(tree.root, nv.WorkflowNode)
    assert tree.root.children == []


def test_quest_created_sets_id_and_title() -> None:
    tree = nv.build_tree([
        {"id": "e1", "agent_id": None, "type": "quest_created",
         "payload": {"id": "abc123", "title": "add auth"}},
    ])
    assert tree.root.id == "abc123"
    assert tree.root.title == "add auth"


def test_malformed_events_become_error_nodes_and_never_abort() -> None:
    tree = nv.build_tree([
        {"id": "e1", "agent_id": None, "type": "quest_created",
         "payload": {"quest_id": 1, "title": "ok"}},
        None,                                   # not a dict
        {"agent_id": None, "payload": {}},      # missing "type"
        {"id": "e4", "type": "step_started",
         "payload": {"step": "spec", "kind": "agent", "phase": "design"}},
    ])
    # The two malformed items become ErrorNode children at root scope; the
    # build continues and the (derived) phase still opens.
    errs = [c for c in tree.root.children if isinstance(c, nv.ErrorNode)]
    assert len(errs) == 2
    assert any(isinstance(c, nv.PhaseNode) for c in tree.root.children)


def _ev(eid, etype, agent_id=None, **payload):
    return {"id": eid, "agent_id": agent_id, "type": etype, "payload": payload}


def test_phase_derived_from_step_started_payload() -> None:
    tree = nv.build_tree([
        _ev("1", "quest_created", quest_id=1, title="x"),
        _ev("2", "step_started", step="spec", kind="agent", phase="design",
            model="m", provider="anthropic"),
        _ev("3", "step_completed", step="spec", kind="agent",
            action={"verb": "continue"}, var_mutations={}),
    ])
    # A PhaseNode named "design" is DERIVED from step_started.payload.phase, with
    # the spec step nested under it.
    phases = [c for c in tree.root.children if isinstance(c, nv.PhaseNode)]
    assert len(phases) == 1
    phase = phases[0]
    assert phase.name == "design"
    steps = [c for c in phase.children if isinstance(c, nv.StepNode)]
    assert len(steps) == 1
    step = steps[0]
    assert step.name == "spec"
    assert step.kind == "agent"
    assert step.closed is True
    assert isinstance(step.outcome, nv.OutcomeLine)
    assert step.outcome.action == {"verb": "continue"}


def test_consecutive_steps_in_same_phase_share_one_phase_node() -> None:
    tree = nv.build_tree([
        _ev("1", "step_started", step="spec", kind="agent", phase="design"),
        _ev("2", "step_completed", step="spec", kind="agent",
            action={"verb": "continue"}, var_mutations={}),
        _ev("3", "step_started", step="lint", kind="script", phase="design"),
        _ev("4", "step_completed", step="lint", kind="script",
            action={"verb": "continue"}, var_mutations={}),
    ])
    phases = [c for c in tree.root.children if isinstance(c, nv.PhaseNode)]
    assert len(phases) == 1                       # same phase → one node
    assert [s.name for s in phases[0].children
            if isinstance(s, nv.StepNode)] == ["spec", "lint"]


def test_phase_change_derives_a_new_phase_node() -> None:
    tree = nv.build_tree([
        _ev("1", "step_started", step="spec", kind="agent", phase="design"),
        _ev("2", "step_completed", step="spec", kind="agent",
            action={"verb": "continue"}, var_mutations={}),
        _ev("3", "step_started", step="build", kind="agent", phase="implement"),
    ])
    names = [c.name for c in tree.root.children if isinstance(c, nv.PhaseNode)]
    assert names == ["design", "implement"]


def test_open_step_is_a_valid_terminal_state() -> None:
    # User-pause path emits no step_completed (design §5.1). The step stays open.
    tree = nv.build_tree([
        _ev("2", "step_started", step="spec", kind="agent", phase="design",
            model="m", provider="anthropic"),
        _ev("3", "workflow_paused", reason="ctrl-c"),
    ])
    phase = tree.root.children[0]
    step = phase.children[0]
    assert step.closed is False
    assert step.outcome is None
    assert tree.root.status == "paused"


def test_user_message_text_extracted_inline_without_heavy_import() -> None:
    import sys
    sys.modules.pop("imp.cli.render_helpers", None)
    tree = nv.build_tree([
        _ev("2", "step_started", step="s", kind="agent", phase="p",
            model="m", provider="anthropic"),
        _ev("3", "user_message", agent_id="step:p/s", role="user",
            content=[{"type": "text", "text": "go ahead"},
                     {"type": "text", "text": " now"}]),
    ])
    step = tree.root.children[0].children[0]
    um = [c for c in step.children if isinstance(c, nv.UserMessageNode)]
    assert len(um) == 1
    assert um[0].text == "go ahead now"
    # user_message must NOT trigger the provider parse chain (render_helpers
    # must remain unloaded — langsmith pre-loads pydantic so we check the
    # direct gate, not the transitive dependency).
    assert "imp.cli.render_helpers" not in sys.modules


def test_thinking_and_step_context_become_collapsible_folds() -> None:
    tree = nv.build_tree([
        _ev("2", "step_started", step="s", kind="agent", phase="p",
            model="m", provider="anthropic"),
        _ev("2e", "engine_message", agent_id="step:p/s",
            role="user", subtype="system_prompt",
            content=[{"type": "text", "text": "SYS"}]),
        _ev("3", "step_context", agent_id="step:p/s", provider="anthropic",
            model="m", tools_schema=[{"name": "a"}],
            params={"max_tokens": 8192}, max_tool_calls=50),
        _ev("4", "thinking", agent_id="step:p/s", content="let me think hard"),
    ])
    step = tree.root.children[0].children[0]
    kinds = [type(c).__name__ for c in step.children]
    assert "StepContextNode" in kinds
    assert "ThinkingFold" in kinds
    eng = next(c for c in step.children if isinstance(c, nv.EngineMessageNode))
    assert eng.text == "SYS" and eng.subtype == "system_prompt"
    think = next(c for c in step.children if isinstance(c, nv.ThinkingFold))
    assert think.content == "let me think hard"


def test_agent_text_node_holds_payload_and_parses_lazily() -> None:
    tree = nv.build_tree([
        _ev("2", "step_started", step="s", kind="agent", phase="p",
            model="m", provider="anthropic"),
        _ev("3", "agent_message", agent_id="step:p/s", provider="anthropic",
            model="m", stop_reason="end_turn",
            usage={"input_tokens": 1, "output_tokens": 1},
            raw={"content": [{"type": "text", "text": "hello world"}],
                 "stop_reason": "end_turn",
                 "usage": {"input_tokens": 1, "output_tokens": 1}}),
    ])
    step = tree.root.children[0].children[0]
    at = next(c for c in step.children if isinstance(c, nv.AgentTextNode))
    assert at.payload["model"] == "m"
    # .text triggers the lazy parse chain and returns the assistant prose.
    assert at.text == "hello world"


def _step_events(*content):
    return [_ev("s", "step_started", step="s", kind="agent", phase="p",
                model="m", provider="anthropic"),
            *content,
            _ev("c", "step_completed", step="s", kind="agent",
                action={"verb": "continue"}, var_mutations={})]


def _only_step(tree):
    return tree.root.children[0].children[0]


def test_consecutive_non_task_tools_form_one_burst() -> None:
    tree = nv.build_tree(_step_events(
        _ev("1", "tool_call", agent_id="step:p/s", id="b1", name="bash",
            input={"command": "ls"}),
        _ev("2", "tool_result", agent_id="step:p/s", id="b1", content="ok",
            error=False),
        _ev("3", "tool_call", agent_id="step:p/s", id="b2", name="bash",
            input={"command": "pwd"}),
        _ev("4", "tool_result", agent_id="step:p/s", id="b2", content="/",
            error=False),
    ))
    step = _only_step(tree)
    bursts = [c for c in step.children if isinstance(c, nv.ToolBurstFold)]
    assert len(bursts) == 1
    calls = bursts[0].calls
    assert [c.name for c in calls] == ["bash", "bash"]
    assert calls[0].result == "ok" and calls[1].result == "/"


def test_non_tool_event_flushes_burst_into_two() -> None:
    tree = nv.build_tree(_step_events(
        _ev("1", "tool_call", agent_id="step:p/s", id="b1", name="bash", input={}),
        _ev("2", "tool_result", agent_id="step:p/s", id="b1", content="ok", error=False),
        _ev("3", "agent_message", agent_id="step:p/s", provider="anthropic", model="m",
            stop_reason="tool_use", usage={},
            raw={"content": [{"type": "text", "text": "thinking out loud"}]}),
        _ev("4", "tool_call", agent_id="step:p/s", id="b2", name="grep", input={}),
        _ev("5", "tool_result", agent_id="step:p/s", id="b2", content="hit", error=False),
    ))
    step = _only_step(tree)
    bursts = [c for c in step.children if isinstance(c, nv.ToolBurstFold)]
    assert len(bursts) == 2
    assert [c.name for c in bursts[0].calls] == ["bash"]
    assert [c.name for c in bursts[1].calls] == ["grep"]


def test_tool_killed_marks_the_matching_call_in_burst() -> None:
    tree = nv.build_tree(_step_events(
        _ev("1", "tool_call", agent_id="step:p/s", id="b1", name="bash",
            input={"command": "sleep 999"}),
        _ev("2", "tool_killed", agent_id="step:p/s", id="b1", name="bash"),
        _ev("3", "tool_result", agent_id="step:p/s", id="b1",
            content="terminated", error=True),
    ))
    step = _only_step(tree)
    burst = next(c for c in step.children if isinstance(c, nv.ToolBurstFold))
    assert burst.calls[0].killed is True


def test_unmatched_tool_killed_after_reply_opens_no_burst() -> None:
    # Legacy empty-id kill drained after the user's reply: no call, no new burst.
    tree = nv.build_tree(_step_events(
        _ev("1", "tool_call", agent_id="step:p/s", id="b1", name="bash",
            input={"command": "sleep 45"}),
        _ev("2", "tool_result", agent_id="step:p/s", id="b1",
            content='{"returncode": -15}', error=False),
        _ev("3", "user_message", agent_id="step:p/s", role="user",
            content=[{"type": "text", "text": "carry on"}]),
        _ev("4", "tool_killed", agent_id="step:p/s", id="", name="bash"),
    ))
    step = _only_step(tree)
    kinds = [type(c).__name__ for c in step.children
             if isinstance(c, (nv.ToolBurstFold, nv.UserMessageNode))]
    assert kinds == ["ToolBurstFold", "UserMessageNode"]
    burst = next(c for c in step.children if isinstance(c, nv.ToolBurstFold))
    assert [c.call_id for c in burst.calls] == ["b1"]


def test_unmatched_tool_killed_in_open_burst_adds_no_call() -> None:
    tree = nv.build_tree(_step_events(
        _ev("1", "tool_call", agent_id="step:p/s", id="b1", name="bash",
            input={"command": "sleep 45"}),
        _ev("2", "tool_result", agent_id="step:p/s", id="b1",
            content='{"returncode": -15}', error=False),
        _ev("3", "tool_killed", agent_id="step:p/s", id="", name="bash"),
    ))
    step = _only_step(tree)
    bursts = [c for c in step.children if isinstance(c, nv.ToolBurstFold)]
    assert len(bursts) == 1
    assert [c.call_id for c in bursts[0].calls] == ["b1"]
    assert bursts[0].calls[0].killed is False


def test_read_task_grep_splits_into_three_nodes() -> None:
    # design §5.3 edge (a): a run [read_file, sub_agent, grep] yields
    # ToolBurstFold(read_file), SubagentFold, ToolBurstFold(grep).
    tree = nv.build_tree(_step_events(
        _ev("1", "tool_call", agent_id="step:p/s", id="b1", name="read_file",
            input={"path": "x"}),
        _ev("2", "tool_result", agent_id="step:p/s", id="b1", content="data", error=False),
        # task tool_call: EVENT id "tk1", tool_use block id "b2".
        _ev("tk1", "tool_call", agent_id="step:p/s", id="b2", name="sub_agent",
            input={"subagent_name": "reviewer", "task_description": "review it"}),
        _ev("3", "tool_result", agent_id="step:p/s", id="b2",
            content='{"status":"Success"}', error=False),
        _ev("4", "tool_call", agent_id="step:p/s", id="b3", name="grep",
            input={"pattern": "foo"}),
        _ev("5", "tool_result", agent_id="step:p/s", id="b3", content="hit", error=False),
    ))
    step = _only_step(tree)
    kinds = [type(c).__name__ for c in step.children
             if isinstance(c, (nv.ToolBurstFold, nv.SubagentFold))]
    assert kinds == ["ToolBurstFold", "SubagentFold", "ToolBurstFold"]
    sub = next(c for c in step.children if isinstance(c, nv.SubagentFold))
    assert sub.subagent_name == "reviewer"
    assert sub.running is False            # closed by the task tool_result
    assert sub.event_id == "tk1"
    assert sub.block_id == "b2"


def test_interleaved_sub_events_route_into_fold_without_breaking_burst() -> None:
    # design §5.3 edge (b): the subagent's sub: events sit BETWEEN the parent's
    # task tool_call and its tool_result. They must build the nested tree and
    # must NOT corrupt the parent's surrounding content.
    tree = nv.build_tree(_step_events(
        _ev("tk1", "tool_call", agent_id="step:p/s", id="b1", name="sub_agent",
            input={"subagent_name": "reviewer", "task_description": "go"}),
        # subagent's own stream (agent_id = sub:tk1):
        _ev("s1", "step_context", agent_id="sub:tk1", provider="anthropic",
            model="m", tools_schema=[], params={}, max_tool_calls=50),
        _ev("s2", "agent_message", agent_id="sub:tk1", provider="anthropic", model="m",
            stop_reason="end_turn", usage={"input_tokens": 7, "output_tokens": 2},
            raw={"content": [{"type": "text", "text": "subagent says hi"}]}),
        # parent's task tool_result closes the fold:
        _ev("r1", "tool_result", agent_id="step:p/s", id="b1",
            content='{"status":"Success"}', error=False),
        # parent content after the subagent — must attach to the step, not the fold:
        _ev("a1", "agent_message", agent_id="step:p/s", provider="anthropic", model="m",
            stop_reason="end_turn", usage={"input_tokens": 3, "output_tokens": 1},
            raw={"content": [{"type": "text", "text": "parent resumes"}]}),
    ))
    step = _only_step(tree)
    sub = next(c for c in step.children if isinstance(c, nv.SubagentFold))
    # Nested tree has the subagent's content (no phase/step — subagents emit none).
    nested_kinds = [type(c).__name__ for c in sub.tree.root.children]
    assert "StepContextNode" in nested_kinds and "AgentTextNode" in nested_kinds
    assert sub.event_count == 2            # the two sub: events
    assert sub.running is False
    # The parent text after the fold is a sibling of the fold, not inside it.
    after = [c for c in step.children if isinstance(c, nv.AgentTextNode)]
    assert len(after) == 1 and after[0].text == "parent resumes"


def test_subagent_running_until_task_result_arrives() -> None:
    # Fold stays open mid-step when no tool_result has arrived yet...
    b = nv.NarrativeBuilder()
    b.add(_ev("st", "step_started", step="s", kind="agent", phase="p",
              model="m", provider="anthropic"))
    b.add(_ev("tk1", "tool_call", agent_id="step:p/s", id="b1", name="sub_agent",
              input={"subagent_name": "reviewer", "task_description": "go"}))
    b.add(_ev("s1", "agent_message", agent_id="sub:tk1", provider="anthropic", model="m",
              stop_reason="end_turn", usage={}, raw={"content": []}))
    step = b._step_stack[-1]
    sub = next(c for c in step.children if isinstance(c, nv.SubagentFold))
    assert sub.running is True
    assert sub.outcome is None
    # ...but the backstop at step_completed closes it rather than leaving it running forever.
    b.add(_ev("sc", "step_completed", step="s", kind="agent",
              action={"verb": "continue"}, var_mutations={}))
    assert sub.running is False


def test_error_event_becomes_error_node() -> None:
    # `api` is a Pause category (NOT backoff-retry), so its hard error line is
    # untouched by the suppression — this test still pins the generic
    # error->ErrorNode mapping. Auth/network/rate_limit suppression is covered by
    # tests/test_narrative_retry_quiet.py.
    tree = nv.build_tree([
        _ev("1", "step_started", step="s", kind="agent", phase="p"),
        _ev("2", "error", category="api", message="API Error: 500"),
    ])
    step = tree.root.children[0].children[0]
    errs = [c for c in step.children if isinstance(c, nv.ErrorNode)]
    assert len(errs) == 1
    assert errs[0].category == "api"
    assert errs[0].message == "API Error: 500"
    assert errs[0].is_warning is False


def test_runner_warning_is_styled_non_terminal_error_node() -> None:
    tree = nv.build_tree([
        _ev("2", "step_started", step="s", kind="agent", phase="p",
            model="m", provider="anthropic"),
        _ev("3", "runner_warning", agent_id="step:p/s",
            message="dropped a malformed block"),
    ])
    step = tree.root.children[0].children[0]
    warns = [c for c in step.children if isinstance(c, nv.ErrorNode)]
    assert warns and warns[0].is_warning is True


def test_unknown_and_legacy_types_fall_through_to_fallback_line() -> None:
    # Legacy types the engine no longer emits (design §5.1) get no structural
    # meaning — they fall through, never crash, never drop.
    tree = nv.build_tree([
        _ev("1", "step_started", step="s", kind="agent", phase="p"),
        _ev("2", "phase_exhausted", phase="p"),         # legacy
        _ev("3", "never_heard_of_this", foo="bar"),     # unknown
    ])
    step = tree.root.children[0].children[0]
    fbs = [c for c in step.children if isinstance(c, nv.FallbackLine)]
    assert {f.event_type for f in fbs} == {"phase_exhausted", "never_heard_of_this"}
    assert fbs[1].payload == {"foo": "bar"}


INTERLEAVED_LOG = [
    _ev("c", "quest_created", quest_id=1, title="parity"),
    _ev("s", "step_started", step="build", kind="agent", phase="impl",
        model="m", provider="anthropic"),
    _ev("b1c", "tool_call", agent_id="step:impl/build", id="b1", name="read_file",
        input={"path": "x"}),
    _ev("b1r", "tool_result", agent_id="step:impl/build", id="b1", content="data", error=False),
    _ev("tk1", "tool_call", agent_id="step:impl/build", id="b2", name="sub_agent",
        input={"subagent_name": "reviewer", "task_description": "review"}),
    _ev("su1", "step_context", agent_id="sub:tk1", provider="anthropic",
        model="m", tools_schema=[], params={}, max_tool_calls=50),
    _ev("su2", "agent_message", agent_id="sub:tk1", provider="anthropic", model="m",
        stop_reason="end_turn", usage={"input_tokens": 4, "output_tokens": 1},
        raw={"content": [{"type": "text", "text": "looks good"}]}),
    _ev("tkr", "tool_result", agent_id="step:impl/build", id="b2",
        content='{"status":"Success"}', error=False),
    _ev("g1c", "tool_call", agent_id="step:impl/build", id="b3", name="grep", input={}),
    _ev("g1r", "tool_result", agent_id="step:impl/build", id="b3", content="hit", error=False),
    _ev("done", "step_completed", step="build", kind="agent",
        action={"verb": "continue"}, var_mutations={}),
]


def test_incremental_equals_batch_on_interleaved_log() -> None:
    batch = nv.build_tree(INTERLEAVED_LOG)
    b = nv.NarrativeBuilder()
    for e in INTERLEAVED_LOG:
        b.add(e)
    incremental = b.tree
    # Compare structure after the terminal event (design §5.4): open-node and
    # `· running` transients can't differ here because the log is complete.
    assert nv.structural_repr(batch.root) == nv.structural_repr(incremental.root)


def test_parity_holds_with_open_step_when_paused() -> None:
    # A live-then-paused log has an open StepNode; a batch replay of the same
    # log shows it open too. Both are correct (design §5.1 / §11).
    log = [*INTERLEAVED_LOG[:-1], _ev("wp", "workflow_paused", reason="ctrl-c")]
    batch = nv.structural_repr(nv.build_tree(log).root)
    b = nv.NarrativeBuilder()
    for e in log:
        b.add(e)
    # The trailing leave marker is batch-only by design.
    assert batch["children"][-1] == {"t": "LeaveMarkerNode", "reason": "ctrl-c"}
    del batch["children"][-1]
    assert batch == nv.structural_repr(b.tree.root)


def test_resume_reentry_reuses_phase_and_resets_step() -> None:
    seed = [
        _ev("c", "quest_created", quest_id=1, title="t"),
        _ev("s1", "step_started", step="spec", kind="agent", phase="design",
            model="m", provider="anthropic"),
        _ev("s1c", "step_completed", step="spec", kind="agent",
            action={"verb": "continue"}, var_mutations={}),
        _ev("s2", "step_started", step="build", kind="agent", phase="design",
            model="m", provider="anthropic"),
        _ev("ll1", "agent_message", agent_id="step:design/build", provider="anthropic",
            model="m", stop_reason="end_turn", usage={},
            raw={"content": [{"type": "text", "text": "partial attempt"}]}),
        _ev("wp", "workflow_paused", reason="ctrl-c"),
        _ev("wr", "workflow_resumed"),
    ]
    live = [
        # re-emitted on resume — same phase + step → re-enters, does not duplicate
        _ev("s2b", "step_started", step="build", kind="agent", phase="design",
            model="m", provider="anthropic", restarted=True),
        _ev("ll2", "agent_message", agent_id="step:design/build", provider="anthropic",
            model="m", stop_reason="end_turn", usage={},
            raw={"content": [{"type": "text", "text": "fresh complete"}]}),
        _ev("s2c", "step_completed", step="build", kind="agent",
            action={"verb": "continue"}, var_mutations={}),
    ]
    tree = nv.build_tree(seed + live)
    phases = [c for c in tree.root.children if isinstance(c, nv.PhaseNode)]
    assert len(phases) == 1                              # design NOT duplicated
    steps = [c for c in phases[0].children if isinstance(c, nv.StepNode)]
    assert [s.name for s in steps] == ["spec", "build"]  # build NOT duplicated
    build = steps[1]
    texts = [c.text for c in build.children if isinstance(c, nv.AgentTextNode)]
    assert texts == ["fresh complete"]                   # partial replaced by re-run
    assert build.closed is True
    assert build.outcome.action == {"verb": "continue"}


def test_goto_loop_keeps_separate_phase_iterations() -> None:
    # No workflow_resumed marker → a goto that re-enters design after it has been
    # left open keeps two faithful design iterations. The intervening "implement"
    # step closes the first design phase scope, so the second design step_started
    # derives a fresh PhaseNode.
    events = [
        _ev("s1", "step_started", step="spec", kind="agent", phase="design"),
        _ev("s1c", "step_completed", step="spec", kind="agent",
            action={"verb": "goto", "target": "implement"}, var_mutations={}),
        _ev("s2", "step_started", step="build", kind="agent", phase="implement"),
        _ev("s2c", "step_completed", step="build", kind="agent",
            action={"verb": "goto", "target": "design"}, var_mutations={}),
        _ev("s3", "step_started", step="spec", kind="agent", phase="design"),
        _ev("s3c", "step_completed", step="spec", kind="agent",
            action={"verb": "continue"}, var_mutations={}),
    ]
    tree = nv.build_tree(events)
    names = [c.name for c in tree.root.children if isinstance(c, nv.PhaseNode)]
    assert names == ["design", "implement", "design"]   # both design iterations


def test_resume_into_a_new_phase_does_not_reuse() -> None:
    # Interrupted phase advanced; resume re-emits a step in a DIFFERENT phase.
    seed = [
        _ev("s1", "step_started", step="spec", kind="agent", phase="design"),
        _ev("s1c", "step_completed", step="spec", kind="agent",
            action={"verb": "continue"}, var_mutations={}),
        _ev("wp", "workflow_paused", reason="ctrl-c"),
        _ev("wr", "workflow_resumed"),
    ]
    live = [_ev("s2", "step_started", step="build", kind="agent", phase="implement")]
    tree = nv.build_tree(seed + live)
    names = [c.name for c in tree.root.children if isinstance(c, nv.PhaseNode)]
    assert names == ["design", "implement"]              # implement is genuinely new


def test_current_activity_reports_open_burst() -> None:
    b = nv.NarrativeBuilder()
    for e in [
        ev("1", "quest_created", quest_id=1, title="x"),
        ev("3", "step_started", step="spec", kind="agent", phase="design",
           model="m", provider="anthropic"),
        ev("4", "tool_call", agent_id="step:design/spec", id="b1",
           name="read_file", input={"path": "config.py"}),
    ]:
        b.add(e)
    assert b.current_burst is not None
    act = b.current_activity()
    assert act.detail == "1 call · read_file config.py"
    assert act.verb == "thinking"
    assert act.position is None


def test_current_activity_idle_gap_between_nodes() -> None:
    b = nv.NarrativeBuilder()
    for e in [
        ev("1", "quest_created", quest_id=1, title="x"),
        ev("3", "step_started", step="spec", kind="agent", phase="design",
           model="m", provider="anthropic"),
    ]:
        b.add(e)
    assert b.current_burst is None
    assert b.current_activity() == nv.Activity(
        verb="thinking", model="m", provider="anthropic", kind="agent")


def test_current_activity_carries_agent_step_kind() -> None:
    b = nv.NarrativeBuilder()
    b.add(ev("1", "quest_created", quest_id=1, title="x"))
    b.add(ev("2", "step_started", step="spec", kind="agent", phase="design",
             model="m", provider="anthropic"))
    act = b.current_activity()
    assert act.kind == "agent"
    assert act.lane_wait is False


def test_current_activity_carries_script_step_kind() -> None:
    b = nv.NarrativeBuilder()
    b.add(ev("1", "quest_created", quest_id=1, title="x"))
    b.add(ev("2", "step_started", step="verify", kind="script", phase="implement",
             label="running tests"))
    act = b.current_activity()
    assert act.kind == "script"
    assert act.lane_wait is False


def test_current_activity_carries_kind_while_a_subagent_runs() -> None:
    b = nv.NarrativeBuilder()
    for e in [
        ev("1", "quest_created", quest_id=1, title="x"),
        ev("2", "step_started", step="spec", kind="agent", phase="design",
           model="m", provider="anthropic"),
        ev("3", "tool_call", agent_id="step:design/spec", id="t1",
           name="sub_agent", input={"subagent_name": "coder"}, label="writing code"),
    ]:
        b.add(e)
    act = b.current_activity()
    assert act.verb == "writing code"       # running-subagent path
    assert act.kind == "agent"              # the parent step's kind is carried through
    assert act.lane_wait is False


def test_current_activity_none_when_no_step() -> None:
    b = nv.NarrativeBuilder()
    b.add(ev("1", "quest_created", quest_id=1, title="x"))
    assert b.current_activity() is None


def test_current_activity_agent_step_with_thinking_child_emits_no_verb() -> None:
    b = nv.NarrativeBuilder()
    for e in [
        ev("1", "quest_created", quest_id=1, title="x"),
        ev("3", "step_started", step="spec", kind="agent", phase="design",
           model="m", provider="anthropic"),
        ev("4", "thinking", agent_id="step:design/spec", content="reasoning..."),
    ]:
        b.add(e)
    assert b.current_activity() == nv.Activity(
        verb="thinking", model="m", provider="anthropic", kind="agent")


def test_subagent_current_activity_includes_nested_detail() -> None:
    b = nv.NarrativeBuilder()
    for e in [
        ev("1", "quest_created", quest_id=1, title="x"),
        ev("3", "step_started", step="spec", kind="agent", phase="design",
           model="m", provider="anthropic"),
        ev("4", "tool_call", agent_id="step:design/spec", id="t1",
           name="sub_agent", input={"subagent_name": "coder"}, label="writing code"),
        ev("5", "tool_call", agent_id="sub:4", id="n1",
           name="edit_file", input={"path": "executor.py"}),
    ]:
        b.add(e)
    act = b.current_activity()
    assert act is not None
    assert act.verb == "writing code"                   # sourced from the event's label
    assert act.detail is not None and "executor.py" in act.detail


def test_subagent_activity_uses_label_when_present() -> None:
    b = nv.NarrativeBuilder()
    for e in [
        ev("1", "quest_created", quest_id=1, title="x"),
        ev("3", "step_started", step="spec", kind="agent", phase="design",
           model="m", provider="anthropic"),
        _ev("4", "tool_call", agent_id="step:design/spec", id="t1",
            name="sub_agent", input={"subagent_name": "explorer"},
            label="exploring codebase"),
        ev("5", "tool_call", agent_id="sub:4", id="n1",
           name="edit_file", input={"path": "executor.py"}),
    ]:
        b.add(e)
    act = b.current_activity()
    assert act is not None
    assert act.verb == "exploring codebase"
    assert "executor.py" in (act.detail or "")
    assert "task →" not in (act.verb or "")
    assert "subagent(" not in (act.verb or "")


def test_subagent_without_label_falls_back_to_generic_name() -> None:
    b = nv.NarrativeBuilder()
    for e in [
        ev("1", "quest_created", quest_id=1, title="x"),
        ev("3", "step_started", step="spec", kind="agent", phase="design",
           model="m", provider="anthropic"),
        _ev("4", "tool_call", agent_id="step:design/spec", id="t1",
            name="sub_agent", input={"subagent_name": "explorer"}),
    ]:
        b.add(e)
    act = b.current_activity()
    assert act is not None
    assert act.verb == "subagent(explorer)"              # no label -> generic fallback
    assert "task →" not in (act.verb or "")


def test_subagent_activity_unmapped_role_falls_back_to_subagent_name() -> None:
    b = nv.NarrativeBuilder()
    for e in [
        ev("1", "quest_created", quest_id=1, title="x"),
        ev("3", "step_started", step="spec", kind="agent", phase="design",
           model="m", provider="anthropic"),
        _ev("4", "tool_call", agent_id="step:design/spec", id="t1",
            name="sub_agent", input={"subagent_name": "mystery"}),
    ]:
        b.add(e)
    act = b.current_activity()
    assert act is not None
    assert act.verb == "subagent(mystery)"               # no role verb -> generic fallback


def test_summarize_result_flattens_text_block_list_in_burst() -> None:
    # A tool_result whose content is a provider content-block list must render
    # as the underlying text, never as a Python repr of the list.
    tree = nv.build_tree(_step_events(
        _ev("1", "tool_call", agent_id="step:p/s", id="b1", name="bash", input={}),
        _ev("2", "tool_result", agent_id="step:p/s", id="b1",
            content=[{"type": "text", "text": "ok, go ahead"}], error=False),
    ))
    step = _only_step(tree)
    burst = next(c for c in step.children if isinstance(c, nv.ToolBurstFold))
    assert burst.calls[0].result == "ok, go ahead"
    assert "[{'" not in burst.calls[0].result


def test_summarize_result_mixed_blocks_fuse_with_no_separator() -> None:
    # Consecutive blocks fuse with NO separator: "see" + [image] -> "see[image]".
    # This is intentional and must not be "fixed" into a "\n" join.
    tree = nv.build_tree(_step_events(
        _ev("1", "tool_call", agent_id="step:p/s", id="b1", name="bash", input={}),
        _ev("2", "tool_result", agent_id="step:p/s", id="b1",
            content=[{"type": "text", "text": "see"}, {"type": "image"}],
            error=False),
    ))
    step = _only_step(tree)
    burst = next(c for c in step.children if isinstance(c, nv.ToolBurstFold))
    assert burst.calls[0].result == "see[image]"


def test_summarize_result_non_text_only_block_list_renders_type_tag() -> None:
    tree = nv.build_tree(_step_events(
        _ev("1", "tool_call", agent_id="step:p/s", id="b1", name="bash", input={}),
        _ev("2", "tool_result", agent_id="step:p/s", id="b1",
            content=[{"type": "tool_reference", "tool_name": "x"}], error=False),
    ))
    step = _only_step(tree)
    burst = next(c for c in step.children if isinstance(c, nv.ToolBurstFold))
    assert burst.calls[0].result == "[tool_reference]"


def test_summarize_result_empty_list_is_empty_string() -> None:
    tree = nv.build_tree(_step_events(
        _ev("1", "tool_call", agent_id="step:p/s", id="b1", name="bash", input={}),
        _ev("2", "tool_result", agent_id="step:p/s", id="b1", content=[], error=False),
    ))
    step = _only_step(tree)
    burst = next(c for c in step.children if isinstance(c, nv.ToolBurstFold))
    assert burst.calls[0].result == ""


def test_summarize_result_flattens_subagent_fold_outcome() -> None:
    # The parent's task tool_result closes the subagent fold; its content can be
    # a block list and must flatten into the fold outcome, not a repr.
    tree = nv.build_tree(_step_events(
        _ev("tk1", "tool_call", agent_id="step:p/s", id="b1", name="sub_agent",
            input={"subagent_name": "reviewer", "task_description": "go"}),
        _ev("r1", "tool_result", agent_id="step:p/s", id="b1",
            content=[{"type": "text", "text": "subagent done"}], error=False),
    ))
    step = _only_step(tree)
    sub = next(c for c in step.children if isinstance(c, nv.SubagentFold))
    assert sub.running is False
    assert sub.outcome == "subagent done"
    assert "[{'" not in sub.outcome


def test_task_result_present_canonical_close_carries_outcome_no_orphan() -> None:
    # Present+matching (the anthropic/openai/codex shape AND post-Part-1
    # claude): a `task` tool_call + tool_result sharing the same non-empty
    # block id, with parent prose AFTER the result. The fold must close via the
    # canonical _h_tool_result path carrying the outcome (is_error/glyph), NOT via
    # resume-close, and there must be NO orphan "tool" burst line.
    tree = nv.build_tree(_step_events(
        _ev("tk1", "tool_call", agent_id="step:p/s", id="b1", name="sub_agent",
            input={"subagent_name": "reviewer", "task_description": "go"}),
        _ev("s1", "step_context", agent_id="sub:tk1", provider="anthropic",
            model="m", tools_schema=[], params={},
            max_tool_calls=50),
        _ev("r1", "tool_result", agent_id="step:p/s", id="b1",
            content="RuntimeError: boom", error=True),
        _ev("a1", "agent_message", agent_id="step:p/s", provider="anthropic", model="m",
            stop_reason="end_turn", usage={}, raw={"content": [
                {"type": "text", "text": "parent resumes"}]}),
    ))
    step = _only_step(tree)
    sub = next(c for c in step.children if isinstance(c, nv.SubagentFold))
    assert sub.running is False
    assert sub.outcome == "RuntimeError: boom"   # outcome-carrying canonical close
    assert sub.is_error is True                  # glyph data preserved
    # No orphan "tool" line: nothing in any burst names itself "tool".
    bursts = [c for c in step.children if isinstance(c, nv.ToolBurstFold)]
    assert all(call.name != "tool" for burst in bursts for call in burst.calls)
    # Parent prose after the fold is a sibling, not swallowed.
    after = [c for c in step.children if isinstance(c, nv.AgentTextNode)]
    assert len(after) == 1 and after[0].text == "parent resumes"


@pytest.mark.parametrize("block_id", ["", "toolu_minted"])
def test_task_result_missing_salvaged_by_resume_close(block_id: str) -> None:
    # Missing-result salvage: a `task` tool_call (parametrized over the empty-id
    # legacy log AND a minted real id) opens a fold; subagent events stream; the
    # PARENT resumes (same agent_id, here an llm_response) with NO task
    # tool_result ever arriving. The resume-close fallback must still close the
    # fold so the live status advances off the subagent.
    b = nv.NarrativeBuilder()
    b.add(_ev("st", "step_started", step="s", kind="agent", phase="p",
              model="m", provider="anthropic"))
    b.add(_ev("tk1", "tool_call", agent_id="step:p/s", id=block_id, name="sub_agent",
              input={"subagent_name": "reviewer", "task_description": "go"}))
    b.add(_ev("s1", "step_context", agent_id="sub:tk1", provider="anthropic",
              model="m", tools_schema=[], params={},
              max_tool_calls=50))
    step = b._step_stack[-1]
    sub = next(c for c in step.children if isinstance(c, nv.SubagentFold))
    assert sub.running is True
    # current_activity sits on the running subagent before the parent resumes.
    assert "subagent(reviewer)" in (b.current_activity().verb or "")
    # Parent resumes (same agent_id) with NO task tool_result — resume-close fires.
    b.add(_ev("a1", "agent_message", agent_id="step:p/s", provider="anthropic",
              model="m", stop_reason="end_turn", usage={}, raw={"content": [
                  {"type": "text", "text": "parent resumes"}]}))
    assert sub.running is False
    # Status line advanced off the subagent (no running fold left).
    act = b.current_activity()
    assert act is None or "subagent(reviewer)" not in (act.verb or "")


def test_legacy_unstamped_codex_fold_salvaged_by_resume_close() -> None:
    # Today's codex writer stamps id_ns="block" and _resume_close_folds skips
    # block-tracked folds entirely. This fixture pins the LEGACY unstamped path:
    # no id_ns on the tool_call, so the only signal that control returned is the
    # parent agent_message carrying codex's turn-end stop_reason=None — resume-
    # close must still close the fold without waiting for the step_completed backstop.
    b = nv.NarrativeBuilder()
    b.add(_ev("st", "step_started", step="s", kind="agent", phase="p",
              model="m", provider="codex"))
    b.add(_ev("tk1", "tool_call", agent_id="step:p/s", id="b1", name="sub_agent",
              input={"subagent_name": "reviewer", "task_description": "go"}))
    b.add(_ev("s1", "step_context", agent_id="sub:tk1", provider="codex",
              model="m", tools_schema=[], params={},
              max_tool_calls=50))
    step = b._step_stack[-1]
    sub = next(c for c in step.children if isinstance(c, nv.SubagentFold))
    assert sub.running is True
    assert "subagent(reviewer)" in (b.current_activity().verb or "")
    b.add(_ev("a1", "agent_message", agent_id="step:p/s", provider="codex",
              model="m", stop_reason=None, usage={}, raw={"content": [
                  {"type": "text", "text": "parent resumes"}]}))
    assert sub.running is False
    act2 = b.current_activity()
    assert act2 is None or "subagent(reviewer)" not in (act2.verb or "")


def test_claude_midstream_fragment_does_not_salvage_close() -> None:
    # claude emits stop_reason=None on mid-stream fragments — value-identical
    # to codex's turn-end None. The provider gate must keep claude fragments
    # mid-turn: a fragment must NOT salvage-close an open task fold (it closes
    # later via its canonical task tool_result or the step_completed backstop).
    b = nv.NarrativeBuilder()
    b.add(_ev("st", "step_started", step="s", kind="agent", phase="p",
              model="m", provider="claude"))
    b.add(_ev("tk1", "tool_call", agent_id="step:p/s", id="b1", name="sub_agent",
              input={"subagent_name": "reviewer", "task_description": "go"}))
    b.add(_ev("s1", "step_context", agent_id="sub:tk1", provider="claude",
              model="m", tools_schema=[], params={},
              max_tool_calls=50))
    step = b._step_stack[-1]
    sub = next(c for c in step.children if isinstance(c, nv.SubagentFold))
    assert sub.running is True
    b.add(_ev("a1", "agent_message", agent_id="step:p/s", provider="claude",
              model="m", stop_reason=None, usage={}, raw={"content": [
                  {"type": "text", "text": "still streaming"}]}))
    assert sub.running is True


def test_task_result_missing_salvaged_by_resume_close_openai() -> None:
    # OpenAI stores the Responses status under the stop_reason key. A terminal
    # status ("completed") on a parent llm_response after a task fold opened with
    # NO task tool_result means control returned, so resume-close must fire.
    b = nv.NarrativeBuilder()
    b.add(_ev("st", "step_started", step="s", kind="agent", phase="p",
              model="m", provider="openai"))
    b.add(_ev("tk1", "tool_call", agent_id="step:p/s", id="b1", name="sub_agent",
              input={"subagent_name": "reviewer", "task_description": "go"}))
    b.add(_ev("s1", "step_context", agent_id="sub:tk1", provider="openai",
              model="m", tools_schema=[], params={},
              max_tool_calls=50))
    step = b._step_stack[-1]
    sub = next(c for c in step.children if isinstance(c, nv.SubagentFold))
    assert sub.running is True
    assert "subagent(reviewer)" in (b.current_activity().verb or "")
    b.add(_ev("a1", "agent_message", agent_id="step:p/s", provider="openai",
              model="m", stop_reason="completed", usage={}, raw={"content": [
                  {"type": "text", "text": "parent resumes"}]}))
    assert sub.running is False
    act3 = b.current_activity()
    assert act3 is None or "subagent(reviewer)" not in (act3.verb or "")


def test_block_tracked_fold_survives_parents_own_tool_call() -> None:
    # Regression (ecui5x): a backgrounded claude `task` dispatch is stamped
    # id_ns="block" on the CALL (matching its result), so awaits_result=True and
    # the fold is shielded from resume-close. The parent then runs its OWN
    # non-task tool_call (a web_search) while the subagent is still in flight —
    # that event is not a task call, not a matching result, and not a mid-turn
    # llm_response, so the resume-close fallback WOULD fire and freeze a bogus
    # near-instant end_ts. The block-tracked guard must keep the fold open until
    # its real result lands, so the rendered duration is the true runtime, not
    # the gap to the parent's next action.
    b = nv.NarrativeBuilder()
    b.add(_ev("st", "step_started", step="s", kind="agent", phase="p",
              model="m", provider="claude"))
    b.add({**_ev("tk1", "tool_call", agent_id="step:p/s", id="blk", name="sub_agent",
                 input={"subagent_name": "reviewer", "task_description": "go"},
                 id_ns="block"),
           "timestamp": "2026-06-23T16:14:14+00:00"})
    b.add(_ev("s1", "step_context", agent_id="sub:tk1", provider="claude",
              model="m", tools_schema=[], params={},
              max_tool_calls=50))
    step = b._step_stack[-1]
    sub = next(c for c in step.children if isinstance(c, nv.SubagentFold))
    assert sub.running is True
    assert sub.awaits_result is True
    # Parent runs its OWN tool while the subagent is backgrounded: this is the
    # exact event that used to resume-close the unstamped fold. It must NOT now.
    b.add({**_ev("ws", "tool_call", agent_id="step:p/s", id="ws1",
                 name="web_search", input={"query": "prior art"}),
           "timestamp": "2026-06-23T16:14:23+00:00"})
    b.add(_ev("wsr", "tool_result", agent_id="step:p/s", id="ws1",
              content="hits", error=False))
    assert sub.running is True            # survived the parent's own tool call
    # The real block-keyed result lands minutes later and closes the fold with
    # the TRUE end_ts — not the 16:14:23 web_search gap.
    b.add({**_ev("tkr", "tool_result", agent_id="step:p/s", id="blk",
                 content='{"status":"Success"}', error=False, id_ns="block"),
           "timestamp": "2026-06-23T16:17:52+00:00"})
    assert sub.running is False
    assert sub.end_ts == "2026-06-23T16:17:52+00:00"
    assert nv.elapsed_seconds(sub.start_ts, sub.end_ts) == pytest.approx(218.0)


def test_openai_non_terminal_status_does_not_salvage_close() -> None:
    # A non-terminal Responses status ("in_progress") is NOT a turn-end; it must
    # leave the fold open (later closed by the canonical result or step_completed).
    b = nv.NarrativeBuilder()
    b.add(_ev("st", "step_started", step="s", kind="agent", phase="p",
              model="m", provider="openai"))
    b.add(_ev("tk1", "tool_call", agent_id="step:p/s", id="b1", name="sub_agent",
              input={"subagent_name": "reviewer", "task_description": "go"}))
    b.add(_ev("s1", "step_context", agent_id="sub:tk1", provider="openai",
              model="m", tools_schema=[], params={},
              max_tool_calls=50))
    step = b._step_stack[-1]
    sub = next(c for c in step.children if isinstance(c, nv.SubagentFold))
    assert sub.running is True
    b.add(_ev("a1", "agent_message", agent_id="step:p/s", provider="openai",
              model="m", stop_reason="in_progress", usage={}, raw={"content": [
                  {"type": "text", "text": "more to come"}]}))
    assert sub.running is True


def test_anthropic_tool_use_midturn_does_not_salvage_close() -> None:
    # Guard: an Anthropic mid-turn stop_reason="tool_use" llm_response must stay
    # mid-turn (no premature salvage-close), unchanged by the helper rewire.
    b = nv.NarrativeBuilder()
    b.add(_ev("st", "step_started", step="s", kind="agent", phase="p",
              model="m", provider="anthropic"))
    b.add(_ev("tk1", "tool_call", agent_id="step:p/s", id="b1", name="sub_agent",
              input={"subagent_name": "reviewer", "task_description": "go"}))
    b.add(_ev("s1", "step_context", agent_id="sub:tk1", provider="anthropic",
              model="m", tools_schema=[], params={},
              max_tool_calls=50))
    step = b._step_stack[-1]
    sub = next(c for c in step.children if isinstance(c, nv.SubagentFold))
    assert sub.running is True
    b.add(_ev("a1", "agent_message", agent_id="step:p/s", provider="anthropic",
              model="m", stop_reason="tool_use", usage={}, raw={"content": [
                  {"type": "tool_use", "id": "b2", "name": "bash", "input": {}}]}))
    assert sub.running is True


def test_step_started_stashes_start_ts_normal_and_resume() -> None:
    # Normal path: start_ts captured from the event's top-level timestamp.
    e1 = _ev("1", "step_started", step="spec", kind="agent", phase="design")
    e1["timestamp"] = "2026-05-30T10:00:00+00:00"
    b = nv.NarrativeBuilder()
    b.add(_ev("0", "quest_created", quest_id=1, title="x"))
    b.add(e1)
    step = b._step_stack[-1]
    assert step.start_ts == "2026-05-30T10:00:00+00:00"
    assert step.agent_result is None

    # Resume re-entry path reuses the open StepNode and must restamp start_ts.
    b.add(_ev("2", "workflow_resumed"))
    e3 = _ev("3", "step_started", step="spec", kind="agent", phase="design",
             restarted=True)
    e3["timestamp"] = "2026-05-30T10:05:00+00:00"
    b.add(e3)
    reused = b._step_stack[-1]
    assert reused is step                      # same node reused
    assert reused.start_ts == "2026-05-30T10:05:00+00:00"


def test_agent_result_stashes_args_without_emitting_prose_node() -> None:
    b = nv.NarrativeBuilder()
    b.add(_ev("0", "quest_created", quest_id=1, title="x"))
    b.add(_ev("1", "step_started", step="coder", kind="agent", phase="build"))
    # agent_output fires before step_completed; its long free-text field is
    # stashed for the breadcrumb/--full view, NEVER promoted to a prose block.
    b.add(_ev("2", "agent_result", agent_id="step:build/coder",
              summary="A long summary. " * 10, status="Success",
              plan_path="docs/plan.md"))
    step = b._step_stack[-1]
    assert step.agent_result["status"] == "Success"
    assert step.agent_result["summary"].startswith("A long summary.")
    lits = [c for c in step.children if isinstance(c, nv.LiteralTextNode)]
    assert lits == []          # output fields are not prose blocks (E1)


def test_agent_result_skips_whitespace_only_prose_field() -> None:
    # A whitespace-only multi-line field passes _is_prose_field (it contains a
    # newline) but must NOT promote to _render_output_args output — the v.strip()
    # guard in _render_output_args skips empty/whitespace values.
    b = nv.NarrativeBuilder()
    b.add(_ev("0", "quest_created", quest_id=1, title="x"))
    b.add(_ev("1", "step_started", step="coder", kind="agent", phase="build"))
    b.add(_ev("2", "agent_result", agent_id="step:build/coder",
              notes="   \n   ", status="Success"))
    step = b._step_stack[-1]
    lits = [c for c in step.children if isinstance(c, nv.LiteralTextNode)]
    assert lits == []


def test_structural_repr_covers_new_node_types() -> None:
    lit = nv.LiteralTextNode(text="hi")
    bc = nv.BreadcrumbLine(step_name="s", elapsed_s=1.0,
                           action={"verb": "continue"}, extras=["status=Success"])
    assert nv.structural_repr(lit) == {"t": "LiteralTextNode", "text": "hi"}
    assert nv.structural_repr(bc) == {
        "t": "BreadcrumbLine", "step_name": "s", "elapsed_s": 1.0,
        "action": {"verb": "continue"}, "extras": ["status=Success"]}
    completed = nv.CompletedNode(quest_id="abc123",
                                 commits=[{"sha": "f" * 40, "subject": "ship it"}])
    assert nv.structural_repr(completed) == {
        "t": "CompletedNode", "quest_id": "abc123",
        "commits": [{"sha": "f" * 40, "subject": "ship it"}]}
    merged = nv.MergedNode(quest_id="abc123", base="master", commit="e" * 40)
    assert nv.structural_repr(merged) == {
        "t": "MergedNode", "quest_id": "abc123", "base": "master", "commit": "e" * 40,
        "commits": []}


def test_current_activity_collapses_multiline_bash_command() -> None:
    b = nv.NarrativeBuilder()
    b.add(_ev("0", "quest_created", quest_id=1, title="x"))
    b.add(_ev("1", "step_started", step="coder", kind="agent", phase="build"))
    cmd = "python3 -c 'import sys\nprint(sys.version)\n" + "x" * 80 + "'"
    b.add(_ev("2", "tool_call", agent_id="step:build/coder", id="t1",
              name="bash", input={"command": cmd}))
    act = b.current_activity()
    assert act.detail is not None
    assert "\n" not in act.detail                    # single line only
    assert "…" in act.detail                         # truncated
    assert len(act.detail) < 120                      # not the whole script
    assert "bash " in act.detail                     # name + sanitized arg present


def test_step_completed_emits_merged_breadcrumb_and_preserves_outcome() -> None:
    b = nv.NarrativeBuilder()
    b.add(_ev("0", "quest_created", quest_id=1, title="x"))
    s = _ev("1", "step_started", step="coder", kind="agent", phase="build")
    s["timestamp"] = "2026-05-30T10:00:00+00:00"
    b.add(s)
    b.add(_ev("2", "agent_result", agent_id="step:build/coder",
              status="Success", plan_path="docs/plan.md"))
    c = _ev("3", "step_completed", step="coder", kind="agent",
            action={"verb": "continue"}, var_mutations={})
    c["timestamp"] = "2026-05-30T10:00:42+00:00"
    b.add(c)

    phase = b.tree.root.children[0]
    step = phase.children[0]
    # outcome field preserved (existing tests assert on it)
    assert isinstance(step.outcome, nv.OutcomeLine)
    assert step.outcome.action == {"verb": "continue"}
    # exactly one breadcrumb child, no standalone OutcomeLine child
    bcs = [c for c in step.children if isinstance(c, nv.BreadcrumbLine)]
    assert len(bcs) == 1
    assert not any(isinstance(c, nv.OutcomeLine) for c in step.children)
    bc = bcs[0]
    assert bc.step_name == "coder"
    assert bc.elapsed_s == 42.0
    assert "status=Success" in bc.extras
    assert "plan_path=docs/plan.md" in bc.extras


def test_step_node_takes_the_round_from_the_payload_defaulting_to_one() -> None:
    # A pre-upgrade engine still appending un-stamped rows is the one state no
    # migration reaches, so the seam's default is what keeps such a row renderable.
    tree = nv.build_tree([
        _ev("1", "step_started", step="plan-reviewer", kind="agent",
            phase="planning", round=3),
        _ev("2", "step_completed", step="plan-reviewer", kind="agent",
            action={"verb": "continue"}),
        _ev("3", "step_started", step="plan-writer", kind="agent", phase="planning"),
    ])
    steps = [c for c in tree.root.children[0].children
             if isinstance(c, nv.StepNode)]
    assert [(s.name, s.round) for s in steps] == [
        ("plan-reviewer", 3), ("plan-writer", 1)]


def test_breadcrumb_carries_the_round_and_the_reviewer_counts() -> None:
    b = nv.NarrativeBuilder()
    b.add(_ev("1", "step_started", step="plan-reviewer", kind="agent",
              phase="planning", round=2))
    b.add(_ev("2", "agent_result", agent_id="step:planning/plan-reviewer",
              status="Success", critical=0, major=2, minor=1))
    b.add(_ev("3", "step_completed", step="plan-reviewer", kind="agent",
              action={"verb": "continue"}))
    step = b.tree.root.children[0].children[0]
    bc = next(c for c in step.children if isinstance(c, nv.BreadcrumbLine))
    assert (bc.round, bc.counts) == (2, (0, 2, 1))
    # The counts render as a tally, so they must not ALSO fold into the --full extras.
    assert bc.extras == ["status=Success"]


def test_a_step_reporting_no_full_triple_carries_no_tally() -> None:
    b = nv.NarrativeBuilder()
    b.add(_ev("1", "step_started", step="coder", kind="agent", phase="build"))
    b.add(_ev("2", "agent_result", agent_id="step:build/coder",
              status="Success", major=1))
    b.add(_ev("3", "step_completed", step="coder", kind="agent",
              action={"verb": "continue"}))
    step = b.tree.root.children[0].children[0]
    bc = next(c for c in step.children if isinstance(c, nv.BreadcrumbLine))
    assert (bc.round, bc.counts) == (1, None)
    assert bc.extras == ["status=Success"]     # suppression is unconditional


def test_phase_change_closes_the_outgoing_phase() -> None:
    # A phase-name change must close the prior phase so the live cursor can pop
    # it; the new (current) phase stays open during the run.
    tree = nv.build_tree([
        _ev("1", "step_started", step="spec", kind="agent", phase="design"),
        _ev("2", "step_completed", step="spec", kind="agent",
            action={"verb": "continue"}, var_mutations={}),
        _ev("3", "step_started", step="build", kind="agent", phase="implement"),
    ])
    phases = [c for c in tree.root.children if isinstance(c, nv.PhaseNode)]
    assert [p.name for p in phases] == ["design", "implement"]
    assert phases[0].closed is True     # outgoing phase closed on the name change
    assert phases[1].closed is False    # current phase still open during the run


def test_same_phase_call_leaves_current_phase_open() -> None:
    # Consecutive steps in the SAME phase must not close it.
    tree = nv.build_tree([
        _ev("1", "step_started", step="spec", kind="agent", phase="design"),
        _ev("2", "step_completed", step="spec", kind="agent",
            action={"verb": "continue"}, var_mutations={}),
        _ev("3", "step_started", step="lint", kind="script", phase="design"),
    ])
    phases = [c for c in tree.root.children if isinstance(c, nv.PhaseNode)]
    assert len(phases) == 1
    assert phases[0].closed is False


def test_empty_prose_agent_message_yields_no_agent_text_node() -> None:
    # A thinking-only turn (no text block) renders to empty prose. It must NOT
    # produce an AgentTextNode — that would render as an indent-only blank line.
    tree = nv.build_tree([
        _ev("1", "step_started", step="spec", kind="agent", phase="design"),
        _ev("2", "thinking", agent_id="a", content="hmm"),
        _ev("3", "agent_message", agent_id="a",
            raw={"content": [{"type": "thinking", "thinking": "hmm"}]}, usage={}),
        _ev("4", "agent_message", agent_id="a",
            raw={"content": [{"type": "text", "text": "real prose"}]}, usage={}),
    ])
    # Collect AgentTextNodes anywhere under the tree (they live under the StepNode).
    def _agent_texts(node):
        found = []
        if isinstance(node, nv.AgentTextNode):
            found.append(node)
        for c in getattr(node, "children", []):
            found.extend(_agent_texts(c))
        return found

    nodes = _agent_texts(tree.root)
    assert len(nodes) == 1                       # only the real-prose turn
    assert nodes[0].text == "real prose"


def test_empty_agent_message_between_tools_keeps_burst_open() -> None:
    # §8: an empty-text llm_response (lone tool_use block, no prose) is a
    # tool-use-turn carrier and must NOT flush an open burst — otherwise
    # consecutive tool_calls each start a fresh burst with count 1.
    b = nv.NarrativeBuilder()
    b.add({"type": "step_started", "agent_id": None,
           "payload": {"step": "s", "kind": "agent", "phase": "p"}, "id": "e1",
           "timestamp": "2026-06-03T00:00:00+00:00"})
    b.add({"type": "tool_call", "agent_id": None,
           "payload": {"name": "bash", "id": "t1", "input": {"command": "ls"}}, "id": "e2"})
    # tool-use-turn carrier: an llm_response whose only block is a tool_use (no text)
    b.add({"type": "agent_message", "agent_id": None,
           "payload": {"raw": {"content": [{"type": "tool_use", "id": "t2",
                                            "name": "grep", "input": {}}]}}, "id": "e3"})
    b.add({"type": "tool_call", "agent_id": None,
           "payload": {"name": "grep", "id": "t2", "input": {"pattern": "x"}}, "id": "e4"})
    burst = b.current_burst
    assert burst is not None and len(burst.calls) == 2


def test_real_prose_flushes_the_burst() -> None:
    # §8: a real prose llm_response (with non-empty text) MUST flush the burst.
    b = nv.NarrativeBuilder()
    b.add({"type": "step_started", "agent_id": None,
           "payload": {"step": "s", "kind": "agent", "phase": "p"}, "id": "e1",
           "timestamp": "2026-06-03T00:00:00+00:00"})
    b.add({"type": "tool_call", "agent_id": None,
           "payload": {"name": "bash", "id": "t1", "input": {}}, "id": "e2"})
    b.add({"type": "agent_message", "agent_id": None,
           "payload": {"raw": {"content": [{"type": "text", "text": "done."}]}}, "id": "e3"})
    assert b.current_burst is None


def test_task_result_with_matching_id_closes_fold_carrying_outcome() -> None:
    # New contract (replaces the old §11 mismatched-id heuristic, removed when
    # Part 1 made claude mint a real block id matching the call+result).
    # The task tool_call and its tool_result share the SAME non-empty block id;
    # the fold closes via _h_tool_result's block-id match, carrying the outcome.
    b = nv.NarrativeBuilder()
    b.add({"type": "step_started", "agent_id": None,
           "payload": {"step": "task-lead", "kind": "agent", "phase": "implement"},
           "id": "e1", "timestamp": "2026-06-03T00:00:00+00:00"})
    b.add({"type": "tool_call", "agent_id": "step:implement/task-lead",
           "payload": {"name": "sub_agent", "id": "toolu_minted",
                       "input": {"subagent_name": "coder", "task_description": "go"}},
           "id": "e2"})
    # Navigate: root -> PhaseNode -> StepNode -> children (SubagentFold lives here)
    step_children = b.tree.root.children[0].children[0].children
    folds = [c for c in step_children if isinstance(c, nv.SubagentFold)]
    assert folds and folds[0].running is True
    # closing task tool_result carries the SAME id — block-id match closes it.
    b.add({"type": "tool_result", "agent_id": "step:implement/task-lead",
           "payload": {"id": "toolu_minted", "content": "ok"}, "id": "e3"})
    assert folds[0].running is False
    assert folds[0].outcome == "ok"   # _summarize_result("ok") returns "ok" verbatim
    assert folds[0].is_error is False
    # No orphan "tool" burst line: the canonical close consumed the result, so no
    # stray ToolBurstFold was created at step scope.
    bursts = [c for c in step_children if isinstance(c, nv.ToolBurstFold)]
    assert bursts == []


def test_phase_elapsed_from_first_start_to_last_end() -> None:
    b = nv.NarrativeBuilder()
    b.add({"type": "step_started", "agent_id": None,
           "payload": {"step": "s1", "kind": "branch", "phase": "spec"},
           "id": "a", "timestamp": "2026-06-03T00:00:00+00:00"})
    b.add({"type": "step_completed", "agent_id": None,
           "payload": {"step": "s1", "kind": "branch", "action": {"verb": "continue"}},
           "id": "b", "timestamp": "2026-06-03T00:00:10+00:00"})
    phase = b.tree.root.children[0]
    assert isinstance(phase, nv.PhaseNode)
    assert nv.phase_elapsed(phase) == 10.0


def test_phase_elapsed_includes_queue_wait_when_test_lane_step_is_first() -> None:
    # Regression: _h_test_lane_acquired must NOT overwrite start_ts or phase_elapsed
    # under-counts by the entire queue wait.  t0=start, t1=acquired (+5s), t2=done (+5s).
    b = nv.NarrativeBuilder()
    b.add(ev("a", "step_started", step="s1", kind="script", phase="build",
             timestamp="2026-06-03T00:00:00+00:00"))
    b.add(ev("b", "test_lane_acquired", step="s1",
             timestamp="2026-06-03T00:00:05+00:00"))
    b.add(ev("c", "step_completed", step="s1", kind="script",
             action={"verb": "continue"}, var_mutations={},
             timestamp="2026-06-03T00:00:10+00:00"))
    phase = b.tree.root.children[0]
    assert isinstance(phase, nv.PhaseNode)
    # phase_elapsed must span the whole wall-clock window (queue wait included).
    assert nv.phase_elapsed(phase) == 10.0
    step = phase.children[0]
    assert isinstance(step, nv.StepNode)
    crumb = next(
        c for c in step.children
        if isinstance(c, nv.BreadcrumbLine) and c.elapsed_s is not None
    )
    # Breadcrumb elapsed must show only the post-acquisition run time (5 s).
    assert crumb.elapsed_s == 5.0


def test_subagent_fold_terminal_state_defaults_none():
    fold = nv.SubagentFold(subagent_name="x", event_id="e", block_id="b",
                           nested=nv.NarrativeBuilder())
    assert fold.terminal_state is None


def test_close_subagent_derives_stalled_from_real_failure_message():
    # SEAM: feed the EXACT content the provider builds from dispatch_task's
    # RuntimeError. The provider formats a raised exception as
    # f"{type(e).__name__}: {e}", so the content is the string below.
    exc = RuntimeError("subagent 'reviewer' failed (config) [stalled]")
    content = f"{type(exc).__name__}: {exc}"   # the real provider formatting
    fold = nv.SubagentFold(subagent_name="reviewer", event_id="e", block_id="b",
                           nested=nv.NarrativeBuilder())
    nv._close_subagent_fold(fold, {"content": content, "error": True})
    assert fold.terminal_state == "stalled"
    assert fold.is_error is True


def test_close_subagent_derives_suspended_from_handback_message():
    # SEAM: the exact content the provider builds from dispatch_task's suspend
    # RuntimeError (f"{type(e).__name__}: {e}"). The bracketed [suspended] marker
    # in the is_error content must classify the fold as terminal_state="suspended".
    exc = RuntimeError("subagent 'reviewer' was suspended before finishing — "
                       "its provider or retry budget ran out. [suspended]")
    content = f"{type(exc).__name__}: {exc}"
    fold = nv.SubagentFold(subagent_name="reviewer", event_id="e", block_id="b",
                           nested=nv.NarrativeBuilder())
    nv._close_subagent_fold(fold, {"content": content, "error": True})
    assert fold.terminal_state == "suspended"
    assert fold.is_error is True


def test_close_subagent_prefers_explicit_kind():
    fold = nv.SubagentFold(subagent_name="x", event_id="e", block_id="b",
                           nested=nv.NarrativeBuilder())
    nv._close_subagent_fold(fold, {"content": "whatever", "error": True,
                                   "kind": "stalled"})
    assert fold.terminal_state == "stalled"


def test_close_subagent_plain_failure_has_no_terminal_state():
    fold = nv.SubagentFold(subagent_name="x", event_id="e", block_id="b",
                           nested=nv.NarrativeBuilder())
    nv._close_subagent_fold(
        fold,
        {"content": "RuntimeError: subagent 'x' failed (config)", "error": True})
    assert fold.terminal_state is None


def test_close_subagent_success_with_marker_text_is_not_terminal_state():
    # A SUCCESSFUL subagent whose output text happens to contain "[stalled]"
    # must NOT be mislabeled — the marker scan is gated on is_error.
    fold = nv.SubagentFold(subagent_name="x", event_id="e", block_id="b",
                           nested=nv.NarrativeBuilder())
    nv._close_subagent_fold(
        fold,
        {"content": "finding: the build looked [stalled] for a while",
         "error": False})
    assert fold.terminal_state is None
    assert fold.is_error is False


@pytest.mark.parametrize(("payload", "stop"), [
    pytest.param({"content": "RuntimeError: subagent 'x' failed (config) [stalled]",
                  "error": True}, "stalled", id="stalled-marker"),
    pytest.param({"content": "subagent 'x' was interrupted before finishing — "
                             "the run was paused. [interrupted]",
                  "error": True}, "interrupted", id="interrupted-marker"),
    pytest.param({"content": "subagent 'x' reached its context limit and stopped "
                             "before finishing. [context_exhausted]",
                  "error": True}, "context_exhausted", id="context-exhausted-marker"),
    pytest.param({"content": [{"type": "text",
                               "text": "its provider or retry budget ran out. [suspended]"}],
                  "error": True}, "suspended", id="suspended-marker-in-block-list"),
    pytest.param({"content": "whatever", "error": True, "kind": "interrupted"},
                 "interrupted", id="explicit-kind"),
    pytest.param({"content": "RuntimeError: subagent 'x' failed (config)", "error": True},
                 "failed", id="plain-failure"),
    pytest.param({"content": "finding: the build looked [stalled] for a while",
                  "error": False}, None, id="clean-result-with-marker-text"),
    pytest.param({"content": '{"status":"Success"}'}, None, id="clean"),
])
def test_subagent_result_stop(payload, stop):
    assert nv.subagent_result_stop(payload) == stop


def test_quest_renamed_updates_root_title() -> None:
    # The narrative builder must reflect the settled title after quest_renamed.
    b = nv.NarrativeBuilder()
    b.add(ev("1", "quest_created", title=""))
    b.add(ev("2", "quest_renamed", id="aaa111",
             old_title="", new_title="Add OAuth"))
    assert b.tree.root.title == "Add OAuth"


def test_quest_renamed_ignores_legacy_partial_event() -> None:
    # The handler gates on new_title. A legacy/partial event
    # missing new_title must be IGNORED, not half-applied.
    b = nv.NarrativeBuilder()
    b.add(ev("1", "quest_created", title="orig"))
    b.add(ev("2", "quest_renamed",
             old_title="orig",
             title="renamed"))   # NO new_title and NO handle — legacy/partial schema
    assert b.tree.root.title == "orig"          # unchanged (event ignored)


def test_toolcall_call_ts_stamped_by_both_constructors() -> None:
    b = nv.NarrativeBuilder()
    b.add({"id": "1", "type": "step_started", "agent_id": None,
           "payload": {"step": "s", "kind": "agent", "phase": "p"}})
    # normal tool_call constructor
    b.add({"id": "e1", "type": "tool_call", "agent_id": "a",
           "timestamp": "2026-06-10T00:00:01",
           "payload": {"id": "c1", "name": "bash", "input": {"command": "ls"}}})
    # tool_result orphan constructor (no matching open call; "c1" is open but id
    # "orphan" doesn't match it, so a new orphan ToolCall is appended)
    b.add({"id": "e3", "type": "tool_result", "agent_id": "a",
           "timestamp": "2026-06-10T00:00:03",
           "payload": {"id": "orphan", "content": "x"}})
    burst = b.current_burst
    assert burst is not None
    by_id = {c.call_id: c for c in burst.calls}
    assert by_id["c1"].call_ts == "2026-06-10T00:00:01"
    assert by_id["orphan"].call_ts == "2026-06-10T00:00:03"


def test_structural_repr_omits_call_ts() -> None:
    b = nv.NarrativeBuilder()
    b.add({"id": "1", "type": "step_started", "agent_id": None,
           "payload": {"step": "s", "kind": "agent", "phase": "p"}})
    b.add({"id": "e1", "type": "tool_call", "agent_id": "a",
           "timestamp": "2026-06-10T00:00:01",
           "payload": {"id": "c1", "name": "bash", "input": {"command": "ls"}}})
    rep = nv.structural_repr(b.current_burst)
    assert "call_ts" not in rep["calls"][0]


def test_visible_calls_omit_set() -> None:
    burst = nv.ToolBurstFold(calls=[
        nv.ToolCall(call_id="1", name="bash", input={}),
        nv.ToolCall(call_id="2", name="load_skill", input={}),
        nv.ToolCall(call_id="3", name="tool", input={}),
        nv.ToolCall(call_id="6", name="update_step_label", input={}),
        nv.ToolCall(call_id="7", name="read_file", input={}),
    ])
    assert [c.name for c in nv.visible_calls(burst)] == ["bash", "read_file"]
    assert frozenset({"load_skill", "tool", "update_step_label"}) == nv._OMIT
    assert frozenset({"complete_step", "rename_quest", "create_and_enter_worktree",
                      "save_and_exit"}) == nv._INTERNAL_TOOLS
    assert frozenset({"script", "branch", "merge_and_exit_worktree"}) == nv.CHROME_KINDS


def test_tool_result_citations_go_to_footer_not_result() -> None:
    b = nv.NarrativeBuilder()
    b.add(ev("1", "step_started", step="s", kind="agent", phase="p"))
    b.add(ev("t1", "tool_call", agent_id="a", id="c1", name="web_search",
             input={"query": "x"}))
    b.add(ev("t2", "tool_result", agent_id="a", id="c1", content="body text",
             citations=[{"url": "https://a.test"}]))
    call = b.current_burst.calls[0]
    assert call.result == "body text"          # footer NOT appended into result
    assert call.citations_footer == "\n\nSources:\n- https://a.test"
    rep = nv.structural_repr(b.current_burst)["calls"][0]
    assert rep["citations_footer"] == "\n\nSources:\n- https://a.test"
    assert rep["input"] == {"query": "x"}


def test_current_activity_burst_form_count_and_named_on_visible_basis() -> None:
    b = nv.NarrativeBuilder()
    b.add(ev("1", "step_started", step="s", kind="agent", phase="p"))
    b.add(ev("t1", "tool_call", agent_id="a", id="c1", name="load_skill",
             input={"name": "x"}))                       # omitted
    b.add(ev("t2", "tool_call", agent_id="a", id="c2", name="bash",
             input={"command": "make test"}))
    act = b.current_activity()
    assert isinstance(act, nv.Activity)
    assert act.detail == "1 call · bash make test"
    assert act.verb == "thinking"


def test_current_activity_all_killed_basis_is_bare_count() -> None:
    b = nv.NarrativeBuilder()
    b.add(ev("1", "step_started", step="s", kind="agent", phase="p"))
    b.add(ev("t1", "tool_call", agent_id="a", id="c1", name="bash", input={}))
    b.add(ev("k1", "tool_killed", agent_id="a", id="c1", name="bash"))
    act = b.current_activity()
    assert act.detail == "1 call"         # count keeps killed; no named tool (all killed)
    assert act.verb == "thinking"


def test_current_activity_agent_step_no_burst_is_thinking() -> None:
    b = nv.NarrativeBuilder()
    b.add(ev("1", "step_started", step="s", kind="agent", phase="p"))
    assert b.current_activity() == nv.Activity(verb="thinking", kind="agent")


def test_current_activity_chrome_step_is_running() -> None:
    b = nv.NarrativeBuilder()
    b.add(ev("1", "step_started", step="g", kind="merge_and_exit_worktree", phase="p"))
    assert b.current_activity() == nv.Activity(kind="merge_and_exit_worktree")


def _wfu_step(b):
    b.add(ev("1", "step_started", step="s", kind="agent", phase="p"))


def _step_node(b):
    # A step_started carrying a `phase` wraps the StepNode inside a PhaseNode
    # (_h_step_started → _ensure_phase). So root.children[-1] is the PhaseNode and
    # the StepNode — which owns the synthesized UserMessageNode / ToolBurstFold
    # children — is its sole child. This mirrors the file's existing convention
    # (tests/test_narrative.py uses tree.root.children[0].children[0]).
    return b.tree.root.children[-1].children[0]


def test_wfu_error_shaped_result_does_not_synthesize():             # [I1]
    b = nv.NarrativeBuilder()
    _wfu_step(b)
    b.add(ev("t1", "tool_call", agent_id="a", id="w1", name="wait_for_user",
             input={"prompt": "ok?"}))
    b.add(ev("t2", "tool_result", agent_id="a", id="w1",
             content="AssertionError: boom", error=True))
    step = _step_node(b)
    assert not any(isinstance(k, nv.UserMessageNode) for k in step.children)
    call = b.current_burst.calls[0]
    assert call.is_error is True and call.result == "AssertionError: boom"


def test_resume_tool_result_shaped_user_message_renders_reply():    # [I1]
    b = nv.NarrativeBuilder()
    b.add(ev("1", "step_started", step="s", kind="agent", phase="p"))
    b.add(ev("aw", "awaiting_user_input", agent_id="a", prompt="continue?"))
    b.add(ev("um", "user_message", agent_id="a", subtype="reply", content=[
        {"type": "tool_result", "tool_use_id": "w1", "is_error": False,
         "content": "the human reply"}]))
    step = _step_node(b)            # PhaseNode→StepNode helper from Task 8's block
    user = next(k for k in step.children if isinstance(k, nv.UserMessageNode))
    assert user.interactive is True
    assert user.text == "the human reply"


def test_resume_balance_injection_is_engine_message_not_user():
    b = nv.NarrativeBuilder()
    b.add(ev("1", "step_started", step="s", kind="agent", phase="p"))
    # A resume balance-injection is an engine_message/continuation
    # (transcript.balance_success_on_resume), never a human user_message — so it
    # produces no UserMessageNode.
    b.add(ev("um", "engine_message", agent_id="a", subtype="continuation", content=[
        {"type": "tool_result", "tool_use_id": "x", "is_error": False,
         "content": "previous step output recorded; resuming"}]))
    step = _step_node(b)            # PhaseNode→StepNode helper from Task 8's block
    assert not any(isinstance(k, nv.UserMessageNode) for k in step.children)


def test_error_node_threads_full_summary_absent_tolerantly() -> None:
    b = nv.NarrativeBuilder()
    b.add(ev("e1", "error", category="loop", message="[loop] repeated failure x3: y",
             detail={"full_summary": "the full unclipped summary"}))
    err = next(k for k in b.tree.root.children if isinstance(k, nv.ErrorNode))
    assert err.detail == "the full unclipped summary"
    rep = nv.structural_repr(err)
    assert rep["detail"] == "the full unclipped summary"

    b2 = nv.NarrativeBuilder()
    b2.add(ev("e2", "error", category="api", message="boom"))   # no detail key
    err2 = next(k for k in b2.tree.root.children if isinstance(k, nv.ErrorNode))
    assert err2.detail == ""


def test_incremental_equals_batch_for_wfu_and_grown_repr() -> None:
    events = [
        ev("2", "step_started", step="s", kind="agent", phase="p"),
        ev("t0", "tool_call", agent_id="a", id="b1", name="bash",
           input={"command": "ls"}, timestamp="2026-06-10T00:00:01"),
        ev("r0", "tool_result", agent_id="a", id="b1", content="ok",
           citations=[{"url": "https://a.test"}]),
        ev("e1", "error", category="loop", message="x",
           detail={"full_summary": "deep detail"}),
    ]
    # batch == incremental over the whole tree (structural_repr on root)
    batch = nv.build_tree(events)
    b = nv.NarrativeBuilder()
    for e in events:
        b.add(e)
    assert nv.structural_repr(batch.root) == nv.structural_repr(b.tree.root)
    # The above is a smoke test (both use the same NarrativeBuilder path).
    # Positive assertions verify the GROWN FIELD VALUES in the structural repr:
    repr_root = nv.structural_repr(batch.root)
    # Navigate: root → PhaseNode → StepNode → [ToolBurstFold, ErrorNode]
    phase = repr_root["children"][0]
    step = phase["children"][0]
    burst = next(c for c in step["children"] if c["t"] == "ToolBurstFold")
    err = next(c for c in step["children"] if c["t"] == "ErrorNode")
    # Task 3: citations_footer is non-empty (citations were parsed)
    assert burst["calls"][0]["citations_footer"] != ""          # Task 3
    # Task 12: ErrorNode.detail carries the full_summary string
    assert err["detail"] == "deep detail"                       # Task 12
    # Task 1: call_ts is excluded from call structural repr
    assert "call_ts" not in burst["calls"][0]                   # Task 1


def test_concurrent_inprocess_folds_close_independently_by_block_ns() -> None:
    # AC36: a batch logs two `task` tool_calls (non-empty block ids) BEFORE any
    # result. Both folds must stay open concurrently — the second tool_call must
    # NOT resume-close the first — and an id_ns="block" result closes only the
    # matching fold.
    b = nv.NarrativeBuilder()
    b.add(_ev("st", "step_started", step="s", kind="agent", phase="p",
              model="m", provider="anthropic"))
    b.add(_ev("tk1", "tool_call", agent_id="step:p/s", id="b1", name="sub_agent",
              input={"subagent_name": "reviewer", "task_description": "go"}))
    b.add(_ev("tk2", "tool_call", agent_id="step:p/s", id="b2", name="sub_agent",
              input={"subagent_name": "tester", "task_description": "go"}))
    step = b._step_stack[-1]
    folds = {f.subagent_name: f for f in step.children
             if isinstance(f, nv.SubagentFold)}
    # Both siblings open at once (master resume-closes the first → this fails).
    assert folds["reviewer"].running is True
    assert folds["tester"].running is True
    # Close only task2 via its block id, stamped id_ns="block".
    b.add(_ev("r2", "tool_result", agent_id="step:p/s", id="b2", id_ns="block",
              content='{"status":"Success"}', error=False))
    assert folds["tester"].running is False
    assert folds["reviewer"].running is True


def test_codex_task_fold_closes_on_its_own_stamped_result() -> None:
    # Replay of what codex_runner actually writes: a sub_agent tool_call stamped
    # id_ns="block" on a non-empty SDK item id, an intervening turn-end event from
    # the opener, then the matching stamped result. The stamps make awaits_result
    # True, which is what shields the fold from the resume-close fallback.
    b = nv.NarrativeBuilder()
    b.add(ev("st", "step_started", step="s", kind="agent", phase="p",
             model="m", provider="codex"))
    b.add(ev("tk1", "tool_call", "step:p/s", id="item_1", id_ns="block",
             name="sub_agent",
             input={"subagent_name": "explorer", "task_description": "map it"},
             timestamp="2026-09-04T00:00:01"))
    step = b._step_stack[-1]
    fold = next(c for c in step.children if isinstance(c, nv.SubagentFold))
    # A codex agent_message is unconditionally turn-end, so an UNSTAMPED fold
    # would be resume-closed here, outcome-free, with end_ts frozen at :02.
    b.add(ev("am1", "agent_message", "step:p/s", provider="codex", model="m",
             stop_reason=None, usage={},
             raw={"content": [{"type": "text", "text": "dispatched"}]},
             timestamp="2026-09-04T00:00:02"))
    assert fold.running is True
    b.add(ev("r1", "tool_result", "step:p/s", id="item_1", id_ns="block",
             content="explored 4 files", error=False,
             timestamp="2026-09-04T00:00:09"))
    assert fold.running is False
    assert fold.outcome == "explored 4 files"       # closed BY the result
    assert fold.end_ts == "2026-09-04T00:00:09"     # from the result, not :02
    warnings = [c for c in step.children
                if isinstance(c, nv.ErrorNode) and c.is_warning]
    assert warnings == []


def test_concurrent_codex_folds_close_by_sdk_item_id() -> None:
    # Two codex sub_agent dispatches carry DISTINCT non-empty SDK item ids, both
    # stamped id_ns="block". A result closes only the fold sharing its id.
    b = nv.NarrativeBuilder()
    b.add(ev("st", "step_started", step="s", kind="agent", phase="p",
             model="m", provider="codex"))
    b.add(ev("tk1", "tool_call", "step:p/s", id="item_1", id_ns="block",
             name="sub_agent",
             input={"subagent_name": "reviewer", "task_description": "go"}))
    b.add(ev("tk2", "tool_call", "step:p/s", id="item_2", id_ns="block",
             name="sub_agent",
             input={"subagent_name": "tester", "task_description": "go"}))
    step = b._step_stack[-1]
    folds = {f.subagent_name: f for f in step.children
             if isinstance(f, nv.SubagentFold)}
    assert folds["reviewer"].running is True
    assert folds["tester"].running is True
    b.add(ev("r2", "tool_result", "step:p/s", id="item_2", id_ns="block",
             content='{"status":"Success"}', error=False))
    assert folds["tester"].running is False
    assert folds["tester"].outcome == '{"status":"Success"}'
    assert folds["reviewer"].running is True
    # The sibling then closes against its OWN result. _take_subagent_fold pops by
    # both block_id and event_id, so closing one fold must not strand the other.
    b.add(ev("r1", "tool_result", "step:p/s", id="item_1", id_ns="block",
             content='{"status":"Error"}', error=True))
    assert folds["reviewer"].running is False
    assert folds["reviewer"].outcome == '{"status":"Error"}'
    assert folds["reviewer"].is_error is True
    warnings = [c for c in step.children
                if isinstance(c, nv.ErrorNode) and c.is_warning]
    assert warnings == []


def test_task_result_with_no_open_fold_still_warns() -> None:
    # A genuinely malformed stream (a keyed task result with no fold to close)
    # keeps producing the diagnostic, verbatim — the fix stops it firing
    # spuriously, it does not silence it.
    b = nv.NarrativeBuilder()
    b.add(ev("st", "step_started", step="s", kind="agent", phase="p",
             model="m", provider="codex"))
    b.add(ev("r1", "tool_result", "step:p/s", id="item_7", id_ns="block",
             content='{"status":"Success"}', error=False))
    step = b._step_stack[-1]
    warnings = [c for c in step.children
                if isinstance(c, nv.ErrorNode) and c.is_warning]
    assert len(warnings) == 1
    assert warnings[0].message == (
        "task result 'item_7' (id_ns='block') matched no open fold")
    # No orphan "tool" burst entry was created for the unmatched result.
    bursts = [c for c in step.children if isinstance(c, nv.ToolBurstFold)]
    assert all(call.name != "tool" for burst in bursts for call in burst.calls)


def test_event_stamped_result_no_longer_closes_by_the_call_event_id() -> None:
    # The "event" id_ns namespace is deleted, so a result stamped with it names an
    # unknown namespace: it closes nothing and warns, even when its id is exactly
    # the open fold's tool_call EVENT id — the key the old probe looked up.
    b = nv.NarrativeBuilder()
    b.add(_ev("st", "step_started", step="s", kind="agent", phase="p",
              model="m", provider="codex"))
    b.add(_ev("tk1", "tool_call", agent_id="step:p/s", id="b1", name="sub_agent",
              input={"subagent_name": "reviewer", "task_description": "go"}))
    step = b._step_stack[-1]
    fold = next(c for c in step.children if isinstance(c, nv.SubagentFold))
    assert fold.event_id == "tk1"
    b.add(_ev("r1", "tool_result", agent_id="step:p/s", id="tk1", id_ns="event",
              content='{"status":"Success"}', error=False))
    assert fold.running is True                      # not closed
    warnings = [c for c in step.children
                if isinstance(c, nv.ErrorNode) and c.is_warning]
    assert [w.message for w in warnings] == [
        "task result 'tk1' (id_ns='event') matched no open fold"]


def test_stamped_result_missing_its_namespace_renders_warning_not_close() -> None:
    # AC36/AC38: a result naming a namespace that does not exist renders a
    # malformed-log WARNING and closes nothing — even with exactly one fold open.
    b = nv.NarrativeBuilder()
    b.add(_ev("st", "step_started", step="s", kind="agent", phase="p",
              model="m", provider="anthropic"))
    b.add(_ev("tk1", "tool_call", agent_id="step:p/s", id="b1", name="sub_agent",
              input={"subagent_name": "reviewer", "task_description": "go"}))
    step = b._step_stack[-1]
    fold = next(c for c in step.children if isinstance(c, nv.SubagentFold))
    # "b1" IS a live block id, which suppresses the resume-close fallback (add()
    # matches on the raw id, ignoring id_ns) — so the unknown namespace is the only
    # thing between this result and a close, and the fold must survive it.
    b.add(_ev("r1", "tool_result", agent_id="step:p/s", id="b1", id_ns="event",
              content='{"status":"Success"}', error=False))
    assert fold.running is True                      # not closed
    warnings = [c for c in step.children
                if isinstance(c, nv.ErrorNode) and c.is_warning]
    assert len(warnings) == 1
    # No orphan "tool" burst entry was created for the stamped miss.
    bursts = [c for c in step.children if isinstance(c, nv.ToolBurstFold)]
    assert all(call.name != "tool" for burst in bursts for call in burst.calls)


def test_late_result_after_resume_close_is_benign_not_a_warning() -> None:
    # Regression: a task fold provisionally closed by the resume-close fallback
    # (a non-matching PARENT event arrived before the canonical result) must NOT
    # warn "matched no open fold" when its real id_ns="block" result lands late.
    # It is the AUTHORITATIVE close: the outcome-free salvage close is upgraded.
    b = nv.NarrativeBuilder()
    b.add(_ev("st", "step_started", step="s", kind="agent", phase="p",
              model="m", provider="anthropic"))
    b.add(_ev("tk1", "tool_call", agent_id="step:p/s", id="m1", name="sub_agent",
              input={"subagent_name": "reviewer", "task_description": "go"}))
    step = b._step_stack[-1]
    fold = next(c for c in step.children if isinstance(c, nv.SubagentFold))
    # A non-matching parent event triggers the resume-close fallback (pops m1).
    b.add(_ev("a1", "agent_message", agent_id="step:p/s", provider="anthropic",
              model="m", stop_reason="end_turn", usage={},
              raw={"content": [{"type": "text", "text": "hmm"}]}))
    assert fold.running is False                     # provisionally closed
    assert fold.outcome != '{"status":"Success"}'    # outcome-free salvage close
    # The canonical close arrives late, stamped id_ns="block".
    b.add(_ev("r1", "tool_result", agent_id="step:p/s", id="m1", id_ns="block",
              content='{"status":"Success"}', error=False))
    warnings = [c for c in step.children
                if isinstance(c, nv.ErrorNode) and c.is_warning]
    assert warnings == []                            # benign race, no warning
    assert fold.outcome == '{"status":"Success"}'    # outcome upgraded
    # No orphan "tool" burst entry was created for the late result.
    bursts = [c for c in step.children if isinstance(c, nv.ToolBurstFold)]
    assert all(call.name != "tool" for burst in bursts for call in burst.calls)


def test_late_result_after_pause_resume_finalize_is_benign() -> None:
    # Regression sibling: a fold finalized "interrupted" by pause/resume whose
    # dispatch outlived the pause still logs its canonical result afterward. That
    # result must be recognised (no warning); the interrupted terminal state is
    # preserved (a success result does not un-interrupt a finalized fold).
    b = nv.NarrativeBuilder()
    b.add(_ev("st", "step_started", step="s", kind="agent", phase="p",
              model="m", provider="anthropic"))
    b.add(_ev("tk1", "tool_call", agent_id="step:p/s", id="m1", name="sub_agent",
              input={"subagent_name": "reviewer", "task_description": "go"}))
    step = b._step_stack[-1]
    fold = next(c for c in step.children if isinstance(c, nv.SubagentFold))
    b.add(_ev("p1", "workflow_paused", reason="ctrl-c"))
    b.add(_ev("rs1", "workflow_resumed"))
    assert fold.terminal_state == "interrupted"
    b.add(_ev("r1", "tool_result", agent_id="step:p/s", id="m1", id_ns="block",
              content='{"status":"Success"}', error=False))
    warnings = [c for c in step.children
                if isinstance(c, nv.ErrorNode) and c.is_warning]
    assert warnings == []
    assert fold.terminal_state == "interrupted"      # not un-interrupted


def test_unstamped_legacy_result_closes_via_ordered_two_dict_probe() -> None:
    # AC38: a legacy (id_ns absent) result still closes its fold via the ordered
    # block-id-then-event-id probe — no regression for pre-upgrade logs.
    tree = nv.build_tree(_step_events(
        _ev("tk1", "tool_call", agent_id="step:p/s", id="b1", name="sub_agent",
            input={"subagent_name": "reviewer", "task_description": "go"}),
        _ev("r1", "tool_result", agent_id="step:p/s", id="b1",
            content='{"status":"Success"}', error=False),
    ))
    step = _only_step(tree)
    sub = next(c for c in step.children if isinstance(c, nv.SubagentFold))
    assert sub.running is False
    assert sub.outcome == '{"status":"Success"}'


def test_workflow_resumed_finalizes_dangling_folds_as_interrupted() -> None:
    # AC37: a suspended batch leaves the unsettled subset's folds open. On
    # workflow_resumed they finalize as interrupted (stop running, terminal_state
    # set), current_activity() is no longer shadowed by them, and the resumed
    # turn's fresh task tool_calls open new running folds.
    b = nv.NarrativeBuilder()
    b.add(_ev("st", "step_started", step="s", kind="agent", phase="p",
              model="m", provider="anthropic"))
    b.add(_ev("tk1", "tool_call", agent_id="step:p/s", id="b1", name="sub_agent",
              input={"subagent_name": "reviewer", "task_description": "go"}))
    step = b._step_stack[-1]
    dangling = next(c for c in step.children if isinstance(c, nv.SubagentFold))
    assert dangling.running is True                       # open at suspend

    b.add(_ev("p1", "workflow_paused", interrupt_reason="interrupt"))
    b.add(_ev("r1", "workflow_resumed"))
    # Dangling fold finalized as interrupted (master leaves it running).
    assert dangling.running is False
    assert dangling.terminal_state == "interrupted"
    # current_activity no longer shadowed by the dangling fold.
    act4 = b.current_activity()
    assert act4 is None or "subagent(reviewer)" not in (act4.verb or "")

    # Resumed turn re-enters the step and opens a fresh running fold.
    b.add(_ev("st2", "step_started", step="s", kind="agent", phase="p",
              model="m", provider="anthropic"))
    b.add(_ev("tk2", "tool_call", agent_id="step:p/s", id="b9", name="sub_agent",
              input={"subagent_name": "reviewer", "task_description": "again"}))
    fresh = [c for c in b._step_stack[-1].children
             if isinstance(c, nv.SubagentFold) and c.block_id == "b9"]
    assert len(fresh) == 1 and fresh[0].running is True


def test_resume_drops_interrupted_fold_membership_so_late_events_dont_reattach() -> None:
    # A dangling fold finalized as interrupted on workflow_resumed must also lose
    # its `_sub_by_agent_id` membership entry — otherwise a late `sub:` event for
    # it would still route in by exact membership and keep accumulating onto an
    # interrupted fold, the very thing the finalize exists to stop.
    b = nv.NarrativeBuilder()
    b.add(_ev("st", "step_started", step="s", kind="agent", phase="p",
              model="m", provider="anthropic"))
    b.add(_ev("tk1", "tool_call", agent_id="step:p/s", id="b1", name="sub_agent",
              input={"subagent_name": "reviewer", "task_description": "go"}))
    step = b._step_stack[-1]
    dangling = next(c for c in step.children if isinstance(c, nv.SubagentFold))
    assert "sub:tk1" in b._sub_by_agent_id

    b.add(_ev("p1", "workflow_paused", interrupt_reason="interrupt"))
    b.add(_ev("r1", "workflow_resumed"))
    assert "sub:tk1" not in b._sub_by_agent_id          # membership dropped
    count_before = dangling.event_count

    # A late event carrying the interrupted fold's sub: id must NOT reattach.
    b.add(_ev("late", "agent_message", agent_id="sub:tk1", usage={}))
    assert dangling.event_count == count_before


def test_current_activity_several_running_subagents_show_a_count() -> None:
    # keke66 B1: one running fold -> its own verb; several -> a "running N subagents"
    # count on row 1 (each subagent's detail moves to the indented per-subagent rows).
    def _task(eid: str, bid: str, name: str) -> dict:
        return _ev(eid, "tool_call", agent_id="step:p/s", id=bid, name="sub_agent",
                   input={"subagent_name": name, "task_description": "go"})

    b = nv.NarrativeBuilder()
    b.add(_ev("st", "step_started", step="s", kind="agent", phase="p",
              model="m", provider="anthropic"))
    b.add(_task("tk1", "b1", "reviewer"))
    one = b.current_activity()
    assert one.verb == "subagent(reviewer)"               # single fold: its own verb

    b.add(_task("tk2", "b2", "tester"))
    two = b.current_activity()
    assert two.verb == "running 2 subagents"              # several -> a count
    assert "subagent(reviewer)" not in (two.verb or "")
    assert (two.provider, two.model) == ("anthropic", "m")

    b.add(_task("tk3", "b3", "researcher"))
    three = b.current_activity()
    assert three.verb == "running 3 subagents"            # plural count grows with N


def test_interrupt_config_event_suppresses_error_and_marks_step() -> None:
    tree = nv.build_tree([
        _ev("1", "step_started", step="spec-writer", kind="agent", phase="design",
            model="m", provider="anthropic"),
        _ev("2", "error", category="config", kind="interrupted",
            message="runner cancelled by interrupt"),
        _ev("3", "step_completed", step="spec-writer", kind="agent",
            action={"verb": "pause"}, var_mutations={}),
    ])
    step = tree.root.children[0].children[0]
    assert step.interrupted is True

    def _has_error(node: object) -> bool:
        if isinstance(node, nv.ErrorNode):
            return True
        return any(_has_error(c) for c in getattr(node, "children", []))

    assert not _has_error(tree.root)
    crumbs = [c for c in step.children if isinstance(c, nv.BreadcrumbLine)]
    assert len(crumbs) == 1
    assert crumbs[0].interrupted is True


def test_non_interrupt_config_error_still_renders() -> None:
    tree = nv.build_tree([
        _ev("1", "step_started", step="spec", kind="agent", phase="design",
            model="m", provider="anthropic"),
        _ev("2", "error", category="config", kind="transcript_invalid",
            message="transcript ended with dangling tool_use"),
    ])

    def _errors(node: object) -> list[object]:
        out = [node] if isinstance(node, nv.ErrorNode) else []
        for c in getattr(node, "children", []):
            out.extend(_errors(c))
        return out

    assert len(_errors(tree.root)) == 1


def test_quest_merged_becomes_merged_node_not_fallback() -> None:
    tree = nv.build_tree([
        _ev("0", "quest_created", id="runx01", title="Run X"),
        _ev("1", "step_started", step="merge-to-base",
            kind="merge_and_exit_worktree", phase="review"),
        _ev("2", "step_completed", step="merge-to-base", kind="merge_and_exit_worktree",
            action={"verb": "continue"}, var_mutations={}),
        _ev("3", "quest_merged", base="master", commit="37dde900abc"),
    ])
    merged = [c for c in tree.root.children if isinstance(c, nv.MergedNode)]
    assert len(merged) == 1
    assert merged[0].quest_id == "runx01"
    assert merged[0].base == "master"
    assert merged[0].commit == "37dde900abc"
    # the raw event no longer leaks as a fallback line anywhere in the tree
    fbs: list[nv.FallbackLine] = []

    def walk(nodes: list) -> None:
        for n in nodes:
            if isinstance(n, nv.FallbackLine):
                fbs.append(n)
            walk(getattr(n, "children", []))
    walk(tree.root.children)
    assert all(f.event_type != "quest_merged" for f in fbs)


def test_merge_blocked_event_is_suppressed() -> None:
    tree = nv.build_tree([
        _ev("1", "step_started", step="merge-to-base",
            kind="merge_and_exit_worktree", phase="review"),
        _ev("2", "merge_blocked", reason="refused", base="master",
            detail="error: Your local changes ... Aborting"),
    ])
    step = tree.root.children[0].children[0]
    fbs = [c for c in step.children if isinstance(c, nv.FallbackLine)]
    assert fbs == []


def _reviewer_conclusion(narration: str, findings: str) -> nv.NarrativeTree:
    return nv.build_tree([
        _ev("1", "step_started", step="spec-reviewer", kind="agent", phase="spec",
            model="m", provider="anthropic"),
        _ev("2", "agent_message", agent_id="step:spec/spec-reviewer",
            provider="anthropic", model="m", usage={},
            raw={"content": [{"type": "text", "text": narration}]}),
        _ev("3", "tool_call", agent_id="step:spec/spec-reviewer", id="cs1",
            name="complete_step",
            input={"status": "Success", "verdict": "approve", "findings": findings}),
        _ev("4", "agent_result", agent_id="step:spec/spec-reviewer",
            status="Success", verdict="approve", findings=findings),
        _ev("5", "step_completed", step="spec-reviewer", kind="agent",
            action={"verb": "continue"}, var_mutations={}),
    ])


def test_trailing_narration_kept_and_findings_not_promoted() -> None:
    # E1: the agent's prose narration is the single rendered copy. The complete_step
    # `findings` arg is plumbing, never promoted to a LiteralTextNode, and the
    # (now-deleted) near-duplicate guard no longer drops the narration behind it.
    tree = _reviewer_conclusion(
        narration="No findings. Spec is complete and ready for planning.",
        findings=("No findings. Spec is complete and ready for planning. "
                  "All load-bearing claims were verified against the codebase."))
    step = tree.root.children[0].children[0]
    agent_texts = [c for c in step.children if isinstance(c, nv.AgentTextNode)]
    literals = [c for c in step.children if isinstance(c, nv.LiteralTextNode)]
    bursts = [c for c in step.children if isinstance(c, nv.ToolBurstFold)]
    assert len(bursts) == 1           # the trailing complete_step burst is present
    assert len(agent_texts) == 1      # the narration renders once (not dropped)
    assert literals == []             # findings is not a prose block (E1)


def _panel_member(eid: str, verdict: str, findings: str = "") -> list[dict]:
    bid = f"b{eid}"
    return [
        _ev(f"c{eid}", "tool_call", agent_id="step:review/review", id=bid,
            name="sub_agent", input={"subagent_name": f"member-{eid}",
                                 "task_description": "review"}),
        _ev(f"r{eid}", "tool_result", agent_id="step:review/review", id=bid,
            content=json.dumps(
                {"status": "Success", "verdict": verdict, "findings": findings}),
            error=False),
    ]


def test_clean_panel_findings_not_promoted_to_prose() -> None:
    events = [
        _ev("1", "step_started", step="review", kind="agent", phase="review",
            model="m", provider="anthropic"),
    ]
    events += _panel_member("1", "approve", "nit: rename foo")
    events += _panel_member("2", "approve", "")
    events += _panel_member("3", "approve", "nit: add a comment")
    events += _panel_member("4", "approve", "")
    events += [
        _ev("ao", "agent_result", agent_id="step:review/review",
            status="Success", verdict="approve",
            findings="all four approve; two nits were noted\n(foo rename, comment style)"),
        _ev("sc", "step_completed", step="review", kind="agent",
            action={"verb": "goto", "target": "integrate-rebase"}, var_mutations={}),
    ]
    tree = nv.build_tree(events)
    step = tree.root.children[0].children[0]
    folds = [c for c in step.children if isinstance(c, nv.SubagentFold)]
    literals = [c for c in step.children if isinstance(c, nv.LiteralTextNode)]
    assert len(folds) == 4
    assert literals == []                  # findings arg is not a prose block (E1)
    assert not hasattr(nv, "PanelSummaryNode")


def test_blocking_panel_findings_not_promoted_to_prose() -> None:
    events = [
        _ev("1", "step_started", step="review", kind="agent", phase="review",
            model="m", provider="anthropic"),
    ]
    events += _panel_member("1", "task_gap", "blocking: missing handler")
    events += _panel_member("2", "approve", "")
    events += [
        _ev("ao", "agent_result", agent_id="step:review/review",
            status="Success", verdict="rework",
            findings="### member-1 (task-level)\nblocking: missing handler that is "
                     "reachable in normal usage and clearly blocks the merge gate"),
        _ev("sc", "step_completed", step="review", kind="agent",
            action={"verb": "goto", "target": "plan-writer"}, var_mutations={}),
    ]
    tree = nv.build_tree(events)
    step = tree.root.children[0].children[0]
    literals = [c for c in step.children if isinstance(c, nv.LiteralTextNode)]
    assert literals == []                  # findings arg is not a prose block (E1)


def test_agent_step_activity_has_no_step_prefix() -> None:
    b = nv.NarrativeBuilder()
    for e in [
        ev("1", "quest_created", quest_id=1, title="x"),
        ev("2", "step_started", step="plan-review", kind="agent", phase="review",
           model="m", provider="anthropic"),
        ev("3", "thinking", agent_id="step:review/plan-review", content="hmm"),
    ]:
        b.add(e)
    assert b.current_activity() == nv.Activity(
        verb="thinking", model="m", provider="anthropic", kind="agent")


def test_current_activity_command_less_chrome_step_emits_no_verb() -> None:
    b = nv.NarrativeBuilder()
    for e in [
        ev("1", "quest_created", quest_id=1, title="x"),
        ev("2", "step_started", step="merge-it", kind="merge_and_exit_worktree", phase="integrate"),
    ]:
        b.add(e)
    assert b.current_activity() == nv.Activity(kind="merge_and_exit_worktree")


def test_current_activity_agent_no_burst_emits_no_verb() -> None:
    b = nv.NarrativeBuilder()
    b.add(ev("1", "step_started", step="spec", kind="agent", phase="design",
             label="speccing"))
    assert b.current_activity() == nv.Activity(verb="speccing", kind="agent")


def test_current_activity_script_step_names_the_command() -> None:
    b = nv.NarrativeBuilder()
    b.add(ev("1", "step_started", step="gate", kind="script", phase="verify",
             label="testing", command="make check"))
    # Label wins over command as the verb; in the new model verb = label
    assert b.current_activity() == nv.Activity(verb="testing", kind="script")


def test_subagent_activity_has_no_step_prefix() -> None:
    b = nv.NarrativeBuilder()
    for e in [
        ev("1", "quest_created", quest_id=1, title="x"),
        ev("2", "step_started", step="plan-review", kind="agent", phase="review",
           model="m", provider="anthropic"),
        ev("tk1", "tool_call", agent_id="step:review/plan-review", id="b2", name="sub_agent",
           input={"subagent_name": "plan-reviewer", "task_description": "go"}),
        ev("s1", "thinking", agent_id="sub:tk1", content="reviewing"),
    ]:
        b.add(e)
    assert b.current_activity().verb == "subagent(plan-reviewer)"


def test_step_label_and_resumed_land_on_step_and_breadcrumb() -> None:
    tree = nv.build_tree([
        ev("1", "quest_created", quest_id=1, title="x"),
        ev("2", "step_started", step="code-reviewer", kind="agent",
           phase="implement", model="m", provider="anthropic",
           label="reviewing code", resumed=True),
        ev("3", "step_completed", step="code-reviewer", kind="agent",
           action={"verb": "continue"}, var_mutations={}),
    ])
    step = next(c for p in tree.root.children if isinstance(p, nv.PhaseNode)
                for c in p.children if isinstance(c, nv.StepNode))
    assert step.label == "reviewing code"
    # The payload flag alone is a routing loop, not a resume.
    assert step.resumed is False
    bc = next(c for c in step.children if isinstance(c, nv.BreadcrumbLine))
    assert bc.step_label == "reviewing code"


def test_resume_reentry_updates_label_and_resumed_on_step_node() -> None:
    tree = nv.build_tree([
        ev("1", "quest_created", quest_id=1, title="x"),
        ev("2", "step_started", step="code-reviewer", kind="agent",
           phase="implement", model="m", provider="anthropic",
           label="reviewing code", resumed=False),
        ev("3", "workflow_paused", reason="ctrl-c"),
        ev("4", "workflow_resumed"),
        ev("5", "step_started", step="code-reviewer", kind="agent",
           phase="implement", model="m", provider="anthropic",
           label="reviewing code", resumed=True),
    ])
    step = _only_step(tree)
    assert step.label == "reviewing code"
    # A continuation marks its resume point, not the pre-pause header.
    assert step.resumed is False
    assert _headers(step) == [nv.StepHeaderNode(
        label="reviewing code", resumed=True, phase_label=None, model="m",
        route_label=None)]


def test_resume_reentry_updates_position_label_on_step_node() -> None:
    tree = nv.build_tree([
        ev("1", "quest_created", quest_id=1, title="x"),
        ev("2", "step_started", step="task-lead", kind="agent",
           phase="implement", model="m", provider="anthropic",
           position_label=False),
        ev("3", "workflow_paused", reason="ctrl-c"),
        ev("4", "workflow_resumed"),
        ev("5", "step_started", step="task-lead", kind="agent",
           phase="implement", model="m", provider="anthropic",
           position_label=True, restarted=True),
    ])
    step = next(c for p in tree.root.children if isinstance(p, nv.PhaseNode)
                for c in p.children if isinstance(c, nv.StepNode))
    assert step.position_label is True


def _only_step(tree: nv.NarrativeTree) -> nv.StepNode:
    return next(c for p in tree.root.children if isinstance(p, nv.PhaseNode)
                for c in p.children if isinstance(c, nv.StepNode))


def _headers(step: nv.StepNode) -> list[nv.StepHeaderNode]:
    return [c for c in step.children if isinstance(c, nv.StepHeaderNode)]


def test_routed_reentry_without_pause_or_resume_is_not_marked() -> None:
    from imp.cli import render_text as rt
    tree = nv.build_tree([
        ev("1", "quest_created", quest_id=1, title="x"),
        ev("2", "step_started", step="plan-writer", kind="agent", phase="planning",
           model="m", provider="anthropic", resumed=True,
           label="addressing plan review feedback",
           route_label="addressing plan review feedback"),
    ])
    step = _only_step(tree)
    assert step.resumed is False
    assert "(resume)" not in rt.header_line(step, depth=0, color=False)


@pytest.mark.parametrize("restarted", [True, False])
def test_replayed_resume_marks_step_and_header(restarted: bool) -> None:
    from imp.cli import render_text as rt
    tree = nv.build_tree([
        ev("1", "quest_created", quest_id=1, title="x"),
        ev("2", "step_started", step="task-lead", kind="agent", phase="implement",
           model="m", provider="anthropic", label="task 1/2"),
        ev("3", "workflow_paused", reason="ctrl-c"),
        ev("4", "workflow_resumed"),
        ev("5", "step_started", step="task-lead", kind="agent", phase="implement",
           model="m", provider="anthropic", label="task 1/2", resumed=True,
           restarted=restarted),
    ])
    step = _only_step(tree)
    if restarted:
        assert step.resumed is True
        assert " (resume)" in rt.header_line(step, depth=0, color=False)
        assert _headers(step) == []
    else:
        assert step.resumed is False
        assert "(resume)" not in rt.header_line(step, depth=0, color=False)
        assert _headers(step) == [nv.StepHeaderNode(
            label="task 1/2", resumed=True, phase_label=None, model="m",
            route_label=None)]


@pytest.mark.parametrize("reason", ["ctrl-c", "awaiting_user"])
def test_in_session_resume_without_resumed_event_marks_step(reason: str) -> None:
    # The sequence an ATTACHED client receives: workflow_resumed is never
    # broadcast, so only the pause is visible. A your-turn reply is no resume.
    from imp.cli import render_text as rt
    tree = nv.build_tree([
        ev("1", "quest_created", quest_id=1, title="x"),
        ev("2", "step_started", step="task-lead", kind="agent", phase="implement",
           model="m", provider="anthropic", label="task 1/2"),
        ev("3", "workflow_paused", reason=reason),
        ev("4", "step_started", step="task-lead", kind="agent", phase="implement",
           model="m", provider="anthropic", label="task 1/2", resumed=True),
    ])
    step = _only_step(tree)
    assert step.resumed is False
    assert "(resume)" not in rt.header_line(step, depth=0, color=False)
    if reason == "ctrl-c":
        assert _headers(step) == [nv.StepHeaderNode(
            label="task 1/2", resumed=True, phase_label=None, model="m",
            route_label=None)]
    else:
        assert _headers(step) == []


def test_crash_shaped_resume_without_a_pause_marks_step() -> None:
    # A crash journals no workflow_paused; the recovery-path workflow_resumed is
    # the only boundary marker.
    tree = nv.build_tree([
        ev("1", "quest_created", quest_id=1, title="x"),
        ev("2", "step_started", step="task-lead", kind="agent", phase="implement",
           model="m", provider="anthropic", label="task 1/2"),
        ev("3", "workflow_resumed"),
        ev("4", "step_started", step="task-lead", kind="agent", phase="implement",
           model="m", provider="anthropic", label="task 1/2", resumed=True),
    ])
    step = _only_step(tree)
    assert step.resumed is False
    assert _headers(step) == [nv.StepHeaderNode(
        label="task 1/2", resumed=True, phase_label=None, model="m", route_label=None)]


def test_retry_of_a_resumed_step_keeps_the_marker_and_refreshes_the_label() -> None:
    tree = nv.build_tree([
        ev("1", "quest_created", quest_id=1, title="x"),
        ev("2", "step_started", step="task-lead", kind="agent", phase="implement",
           model="m", provider="anthropic", label="task 1/2"),
        ev("3", "workflow_paused", reason="ctrl-c"),
        ev("4", "step_started", step="task-lead", kind="agent", phase="implement",
           model="m", provider="anthropic", label="task 1/2", resumed=True),
        # Internal transient-error retry: no step_completed, no new pause/resume.
        ev("5", "step_started", step="task-lead", kind="agent", phase="implement",
           model="m", provider="anthropic", label="task 2/2", resumed=True,
           restarted=True),
    ])
    step = _only_step(tree)
    assert step.resumed is False
    # The resume adds one header node; the retry adds none.
    assert _headers(step) == [nv.StepHeaderNode(
        label="task 1/2", resumed=True, phase_label=None, model="m", route_label=None)]
    assert step.label == "task 2/2"      # the existing label refresh still applies


def test_retry_of_a_routed_step_stays_unmarked() -> None:
    tree = nv.build_tree([
        ev("1", "quest_created", quest_id=1, title="x"),
        ev("2", "step_started", step="plan-writer", kind="agent", phase="planning",
           model="m", provider="anthropic", resumed=True,
           label="addressing plan review feedback"),
        ev("3", "step_started", step="plan-writer", kind="agent", phase="planning",
           model="m", provider="anthropic", resumed=True, restarted=True,
           label="addressing plan review feedback"),
    ])
    assert _only_step(tree).resumed is False


def test_a_consumed_pause_does_not_latch_for_the_rest_of_the_session() -> None:
    tree = nv.build_tree([
        ev("1", "quest_created", quest_id=1, title="x"),
        ev("2", "step_started", step="task-lead", kind="agent", phase="implement",
           model="m", provider="anthropic", label="task 1/2"),
        ev("3", "workflow_paused", reason="ctrl-c"),
        ev("4", "step_started", step="task-lead", kind="agent", phase="implement",
           model="m", provider="anthropic", label="task 1/2", resumed=True),
        ev("5", "step_completed", step="task-lead", kind="agent",
           action={"verb": "continue"}, var_mutations={}),
        # A routed re-entry later in the same session: follows the CONSUMED pause,
        # but no new one.
        ev("6", "step_started", step="task-lead", kind="agent", phase="implement",
           model="m", provider="anthropic", label="fixing test failures",
           resumed=True, route_label="fixing test failures"),
    ])
    steps = [c for p in tree.root.children if isinstance(p, nv.PhaseNode)
             for c in p.children if isinstance(c, nv.StepNode)]
    assert [s.resumed for s in steps] == [False, False]
    assert [len(_headers(s)) for s in steps] == [1, 0]


def test_a_resume_signal_alone_does_not_mark_a_fresh_step() -> None:
    tree = nv.build_tree([
        ev("1", "quest_created", quest_id=1, title="x"),
        ev("2", "step_started", step="spec-writer", kind="agent", phase="spec",
           model="m", provider="anthropic", label="drafting the spec"),
        ev("3", "step_completed", step="spec-writer", kind="agent",
           action={"verb": "continue"}, var_mutations={}),
        ev("4", "workflow_paused", reason="ctrl-c"),
        ev("5", "workflow_resumed"),
        ev("6", "step_started", step="spec-finalizer", kind="agent", phase="spec",
           model="m", provider="anthropic", label="finalizing the spec",
           resumed=False),
    ])
    fresh = next(c for p in tree.root.children if isinstance(p, nv.PhaseNode)
                 for c in p.children
                 if isinstance(c, nv.StepNode) and c.name == "spec-finalizer")
    assert fresh.resumed is False


def test_resume_window_reuse_derives_the_flag_from_the_payload() -> None:
    # The reuse branch is gated on the resume window, not on the payload flag, so
    # a payload resumed=False inside that window must stay unmarked.
    tree = nv.build_tree([
        ev("1", "quest_created", quest_id=1, title="x"),
        ev("2", "step_started", step="task-lead", kind="agent", phase="implement",
           model="m", provider="anthropic", label="task 1/2"),
        ev("3", "workflow_paused", reason="ctrl-c"),
        ev("4", "workflow_resumed"),
        ev("5", "step_started", step="task-lead", kind="agent", phase="implement",
           model="m", provider="anthropic", label="task 1/2", resumed=False),
    ])
    assert _only_step(tree).resumed is False
    assert _headers(_only_step(tree)) == []


_PHASE = "implementation phase · plan 1/1"


def _texts(step: nv.StepNode) -> list[str]:
    return [c.text for c in step.children if isinstance(c, nv.AgentTextNode)]


def test_on_stack_restart_keeps_children_and_appends_a_resume_header() -> None:
    tree = nv.build_tree([
        ev("1", "quest_created", quest_id=1, title="t"),
        task_lead_started("2"),
        agent_text("3", "before"),
        ev("4", "workflow_paused", reason="interrupt", interrupt_reason="ctrl-c"),
        task_lead_started("5", label="task 2/2", resumed=True, restarted=True),
    ])
    step = _only_step(tree)
    assert _texts(step) == ["before"]
    assert step.resumed is False
    assert _headers(step) == [nv.StepHeaderNode(
        label="task 2/2", resumed=True, phase_label=_PHASE, model="m", route_label=None)]


def test_plain_retry_only_refreshes_the_live_label() -> None:
    tree = nv.build_tree([
        ev("1", "quest_created", quest_id=1, title="t"),
        task_lead_started("2"),
        task_lead_started("3", label="task 2/2", resumed=True, restarted=True),
    ])
    step = _only_step(tree)
    assert _headers(step) == []
    assert step.resumed is False
    assert step.label == "task 2/2"


def test_reopen_restart_adds_no_header_node() -> None:
    tree = nv.build_tree([
        ev("1", "quest_created", quest_id=1, title="t"),
        task_lead_started("2"),
        agent_text("3", "before"),
        ev("4", "workflow_paused", reason="interrupt", interrupt_reason="ctrl-c"),
        ev("5", "workflow_resumed"),
        task_lead_started("6", label="task 2/2", resumed=True, restarted=True),
    ])
    step = _only_step(tree)
    assert _texts(step) == []
    assert _headers(step) == []
    assert step.resumed is True


def test_hibernated_restart_after_a_solicit_clears_the_resume_mark() -> None:
    tree = nv.build_tree([
        ev("1", "quest_created", quest_id=1, title="t"),
        task_lead_started("2"),
        agent_text("3", "before"),
        ev("4", "workflow_paused", reason="interrupt", interrupt_reason="ctrl-c"),
        ev("5", "workflow_resumed"),
        task_lead_started("6", resumed=True, restarted=True),
        ev("7", "workflow_paused", reason="awaiting_user"),
        ev("8", "workflow_resumed"),
        task_lead_started("9", label="task 2/2", resumed=True, restarted=True),
        agent_text("10", "fresh"),
    ])
    step = _only_step(tree)
    assert _texts(step) == ["fresh"]
    assert _headers(step) == []
    assert step.resumed is False
    assert step.label == "task 2/2"


def test_hidden_step_gains_no_resume_header() -> None:
    tree = nv.build_tree([
        ev("1", "quest_created", quest_id=1, title="t"),
        task_lead_started("2", show_label=False),
        ev("3", "workflow_paused", reason="interrupt", interrupt_reason="ctrl-c"),
        task_lead_started("4", show_label=False, resumed=True),
    ])
    step = _only_step(tree)
    assert _headers(step) == []
    assert step.resumed is False


def test_a_later_solicit_reply_keeps_an_earned_resume_mark() -> None:
    tree = nv.build_tree([
        ev("1", "quest_created", quest_id=1, title="t"),
        ev("2", "workflow_paused", reason="interrupt", interrupt_reason="ctrl-c"),
        ev("3", "workflow_resumed"),
        task_lead_started("4", resumed=True, restarted=False),
        ev("5", "workflow_paused", reason="awaiting_user"),
        ev("6", "workflow_resumed"),
        task_lead_started("7", resumed=True, restarted=False),
    ])
    step = _only_step(tree)
    assert step.resumed is True
    assert _headers(step) == []


def test_hidden_step_gains_no_header_on_a_reopen_resume() -> None:
    tree = nv.build_tree([
        ev("1", "quest_created", quest_id=1, title="t"),
        task_lead_started("2", show_label=False),
        ev("3", "workflow_paused", reason="interrupt", interrupt_reason="ctrl-c"),
        ev("4", "workflow_resumed"),
        task_lead_started("5", show_label=False, resumed=True, restarted=False),
    ])
    step = _only_step(tree)
    assert _headers(step) == []
    assert step.resumed is False


def test_parallel_batch_folds_ignore_interleaved_parent_chunks() -> None:
    # Regression: a parallel `task` batch dispatches two subagents BEFORE any
    # result. Between the dispatch and the results the parent streams its own
    # mid-turn chunks (a stop_reason=None llm_response and a thinking event).
    # Those chunks must NOT resume-close either still-open sibling fold; each fold
    # closes exactly once, on its OWN task result, stamping its own end_ts.
    b = nv.NarrativeBuilder()
    b.add(ev("st", "step_started", step="s", kind="agent", phase="p",
             model="m", provider="anthropic"))
    b.add(ev("tk1", "tool_call", agent_id="step:p/s", id="b1", name="sub_agent",
             input={"subagent_name": "reviewer", "task_description": "go"},
             timestamp="2026-06-19T10:00:00+00:00"))
    b.add(ev("tk2", "tool_call", agent_id="step:p/s", id="b2", name="sub_agent",
             input={"subagent_name": "tester", "task_description": "go"},
             timestamp="2026-06-19T10:00:01+00:00"))
    step = b._step_stack[-1]
    folds = {f.subagent_name: f for f in step.children
             if isinstance(f, nv.SubagentFold)}
    assert folds["reviewer"].running is True and folds["tester"].running is True
    # Parent mid-turn streaming chunks -- must NOT close either sibling.
    b.add(ev("a1", "agent_message", agent_id="step:p/s", provider="anthropic",
             model="m", stop_reason=None, usage={},
             raw={"content": [{"type": "text", "text": "still working"}]},
             timestamp="2026-06-19T10:00:02+00:00"))
    b.add(ev("a2", "agent_message", agent_id="step:p/s", provider="anthropic",
             model="m", stop_reason="tool_use", usage={},
             raw={"content": []},
             timestamp="2026-06-19T10:00:02.500000+00:00"))
    b.add(ev("th1", "thinking", agent_id="step:p/s", content="pondering",
             timestamp="2026-06-19T10:00:03+00:00"))
    assert folds["reviewer"].running is True and folds["tester"].running is True
    # Each task result closes only its own fold.
    b.add(ev("r1", "tool_result", agent_id="step:p/s", id="b1", id_ns="block",
             content='{"status":"Success"}', error=False,
             timestamp="2026-06-19T10:00:10+00:00"))
    assert folds["reviewer"].running is False and folds["tester"].running is True
    b.add(ev("r2", "tool_result", agent_id="step:p/s", id="b2", id_ns="block",
             content='{"status":"Success"}', error=False,
             timestamp="2026-06-19T10:00:20+00:00"))
    assert folds["tester"].running is False
    # Durations span each fold's own task-call -> own-result, never a parent chunk.
    assert folds["reviewer"].end_ts == "2026-06-19T10:00:10+00:00"
    assert folds["tester"].end_ts == "2026-06-19T10:00:20+00:00"
    assert nv.elapsed_seconds(
        folds["reviewer"].start_ts, folds["reviewer"].end_ts) == 10.0
    assert nv.elapsed_seconds(
        folds["tester"].start_ts, folds["tester"].end_ts) == 19.0


def test_intent_stays_at_root_after_first_step() -> None:
    tree = nv.build_tree([
        {"id": "e1", "agent_id": None, "type": "quest_created",
         "payload": {"handle": "h", "title": "t"}},
        {"id": "e2", "agent_id": None, "type": "user_message",
         "payload": {"role": "user", "subtype": "intent",
                     "content": [{"type": "text", "text": "add a --json flag"}]}},
        _ev("e3", "step_started", step="spec", kind="agent", phase="design",
            model="m", provider="anthropic"),
    ])
    # The intent is a child of root, ahead of the first phase — never re-homed.
    intents = [c for c in tree.root.children if isinstance(c, nv.UserMessageNode)]
    assert len(intents) == 1
    assert intents[0].text == "add a --json flag"
    # interactive=True is required, else render_text drops it as synthetic plumbing.
    assert intents[0].interactive is True
    phase = next(c for c in tree.root.children if isinstance(c, nv.PhaseNode))
    step = next(c for c in phase.children if isinstance(c, nv.StepNode))
    assert not any(isinstance(c, nv.UserMessageNode) for c in step.children)
    # Ordering: the intent precedes the phase header at root.
    root_kinds = [type(c) for c in tree.root.children]
    assert root_kinds.index(nv.UserMessageNode) < root_kinds.index(nv.PhaseNode)


def test_no_intent_yields_no_user_message_node() -> None:
    tree = nv.build_tree([
        _ev("e1", "step_started", step="spec", kind="agent", phase="design",
            model="m", provider="anthropic"),
    ])
    phase = next(c for c in tree.root.children if isinstance(c, nv.PhaseNode))
    step = next(c for c in phase.children if isinstance(c, nv.StepNode))
    assert not any(isinstance(c, nv.UserMessageNode) for c in step.children)
    assert not any(isinstance(c, nv.UserMessageNode) for c in tree.root.children)


def test_intent_at_root_not_under_any_step() -> None:
    tree = nv.build_tree([
        {"id": "e1", "agent_id": None, "type": "quest_created",
         "payload": {"handle": "h", "title": "t"}},
        {"id": "e2", "agent_id": None, "type": "user_message",
         "payload": {"role": "user", "subtype": "intent",
                     "content": [{"type": "text", "text": "add a --json flag"}]}},
        _ev("s1", "step_started", step="spec", kind="agent", phase="design",
            model="m", provider="anthropic"),
        _ev("s1c", "step_completed", step="spec", kind="agent",
            action={"verb": "continue"}, var_mutations={}),
        _ev("s2", "step_started", step="writing-plans", kind="agent", phase="design",
            model="m", provider="anthropic"),
    ])
    intents = [c for c in tree.root.children if isinstance(c, nv.UserMessageNode)]
    assert len(intents) == 1
    phase = next(c for c in tree.root.children if isinstance(c, nv.PhaseNode))
    steps = [c for c in phase.children if isinstance(c, nv.StepNode)]
    assert len(steps) == 2
    # Neither step holds the intent — it stays at root.
    assert not any(isinstance(c, nv.UserMessageNode) for c in steps[0].children)
    assert not any(isinstance(c, nv.UserMessageNode) for c in steps[1].children)


def test_intent_stays_at_root_across_resume_reentry() -> None:
    seed = [
        {"id": "e1", "agent_id": None, "type": "user_message",
         "payload": {"role": "user", "subtype": "intent",
                     "content": [{"type": "text", "text": "add a --json flag"}]}},
        _ev("s1", "step_started", step="spec", kind="agent", phase="design",
            model="m", provider="anthropic"),
        _ev("wp", "workflow_paused", reason="ctrl-c"),
        _ev("wr", "workflow_resumed"),
    ]
    live = [
        # re-emitted on resume: same phase+step triggers the resume re-entry branch.
        _ev("s1b", "step_started", step="spec", kind="agent", phase="design",
            model="m", provider="anthropic", restarted=True),
    ]
    tree = nv.build_tree(seed + live)
    intents = [c for c in tree.root.children if isinstance(c, nv.UserMessageNode)]
    assert len(intents) == 1
    assert intents[0].text == "add a --json flag"
    assert intents[0].interactive is True
    phase = next(c for c in tree.root.children if isinstance(c, nv.PhaseNode))
    step = next(c for c in phase.children if isinstance(c, nv.StepNode))
    # Not re-homed under the step on resume re-entry.
    assert not any(isinstance(c, nv.UserMessageNode) for c in step.children)


def test_nested_lost_result_step_completed_closes_post_order_and_salvages_by_owner() -> None:
    b = nv.NarrativeBuilder()
    for event in [
        ev("st", "step_started", step="s", kind="agent", phase="p"),
        ev("tk1", "tool_call", agent_id="step:p/s", id="shared", name="sub_agent",
           input={"subagent_name": "parent"}),
        ev("tk2", "tool_call", agent_id="sub:tk1", id="shared", name="sub_agent",
           input={"subagent_name": "grandchild"}),
    ]:
        b.add(event)
    parent = b._descendant_builders["sub:tk1"]._owner_fold
    grandchild = b._descendant_builders["sub:tk2"]._owner_fold
    assert parent is not None and grandchild is not None

    b.add(ev("sc", "step_completed", step="s", kind="agent",
             action={"verb": "continue"}, var_mutations={},
             timestamp="2026-06-19T11:00:30+00:00"))
    assert (parent.running, grandchild.running) == (False, False)
    assert parent.end_ts == grandchild.end_ts == "2026-06-19T11:00:30+00:00"
    assert parent.outcome is None and grandchild.outcome is None
    assert set(b._salvage_result_owners) >= {
        ("step:p/s", "shared"), ("sub:tk1", "shared")}

    b.add(ev("late-grand", "tool_result", agent_id="sub:tk1", id="shared",
             id_ns="block", content="grand done", error=False))
    b.add(ev("late-parent", "tool_result", agent_id="step:p/s", id="shared",
             id_ns="block", content="parent done", error=False))
    assert grandchild.outcome == "grand done"
    assert parent.outcome == "parent done"


def test_step_node_command_populated_from_payload_fresh_node() -> None:
    b = nv.NarrativeBuilder()
    b.add(ev("1", "step_started", step="gate", kind="script", phase="verify",
             command="make check"))
    step = b.tree.root.children[-1].children[0]
    assert isinstance(step, nv.StepNode)
    assert step.command == "make check"


def test_step_node_command_populated_on_resume_reentry() -> None:
    seed = [
        ev("c", "quest_created", quest_id=1, title="t"),
        ev("s1", "step_started", step="gate", kind="script", phase="verify",
           command="make test"),
        ev("wp", "workflow_paused", reason="ctrl-c"),
        ev("wr", "workflow_resumed"),
    ]
    live = [
        ev("s1b", "step_started", step="gate", kind="script", phase="verify",
           command="make check", restarted=True),
    ]
    b = nv.NarrativeBuilder()
    for e in seed + live:
        b.add(e)
    phase = next(c for c in b.tree.root.children if isinstance(c, nv.PhaseNode))
    step = phase.children[0]
    assert isinstance(step, nv.StepNode)
    assert step.command == "make check"


def test_tool_use_carrier_agent_message_does_not_split_burst() -> None:
    tree = nv.build_tree([
        ev("s", "step_started", step="s", kind="agent", phase="p",
           model="m", provider="anthropic", timestamp="2026-06-03T00:00:00+00:00"),
        ev("p1", "agent_message", agent_id="step:p/s", provider="anthropic", model="m",
           stop_reason="tool_use", usage={},
           raw={"content": [{"type": "text", "text": "starting"}]},
           timestamp="2026-06-03T00:00:01+00:00"),
        ev("1", "tool_call", agent_id="step:p/s", id="b1", name="bash", input={},
           timestamp="2026-06-03T00:00:02+00:00"),
        ev("2", "tool_result", agent_id="step:p/s", id="b1", content="ok", error=False,
           timestamp="2026-06-03T00:00:03+00:00"),
        # tool-use carrier: a lone tool_use block, no prose text.
        ev("carrier", "agent_message", agent_id="step:p/s", provider="anthropic",
           model="m", stop_reason="tool_use", usage={},
           raw={"content": [{"type": "tool_use", "id": "b2", "name": "grep", "input": {}}]},
           timestamp="2026-06-03T00:00:04+00:00"),
        ev("3", "tool_call", agent_id="step:p/s", id="b2", name="grep", input={},
           timestamp="2026-06-03T00:00:05+00:00"),
        ev("4", "tool_result", agent_id="step:p/s", id="b2", content="hit", error=False,
           timestamp="2026-06-03T00:00:06+00:00"),
    ])
    step = tree.root.children[0].children[0]
    bursts = [c for c in step.children if isinstance(c, nv.ToolBurstFold)]
    # The empty-text carrier did not split the burst.
    assert len(bursts) == 1
    assert [c.name for c in bursts[0].calls] == ["bash", "grep"]


def test_lane_events_render_no_fallback_breadcrumb() -> None:
    tree = nv.build_tree([
        _ev("1", "step_started", step="wait-for-head", kind="branch", phase="merge"),
        _ev("2", "lane_acquired", base="main", step="wait-for-head"),
        _ev("3", "lane_released", base="main", step="reconciler"),
        _ev("4", "lane_evicted", base="main", rewind_to="wait-for-head"),
    ])
    step = tree.root.children[0].children[0]
    fbs = [c for c in step.children if isinstance(c, nv.FallbackLine)]
    assert fbs == []


def test_lane_restored_renders_no_visible_line() -> None:
    base = [
        _ev("1", "step_started", step="wait-for-head", kind="branch", phase="merge"),
        _ev("2", "lane_acquired", base="main", step="wait-for-head"),
    ]
    with_restore = [*base,
                    _ev("3", "lane_restored", base="main", step="summarizer")]
    assert "lane_restored" in nv.NarrativeBuilder._HANDLERS
    step_with = nv.build_tree(with_restore).root.children[0].children[0]
    step_without = nv.build_tree(base).root.children[0].children[0]
    # An unregistered type would render a FallbackLine breadcrumb and grow the step.
    assert [c for c in step_with.children if isinstance(c, nv.FallbackLine)] == []
    assert len(step_with.children) == len(step_without.children)


def test_error_node_falls_back_to_stderr_tail_when_no_full_summary() -> None:
    # A TerminatedError / unknown-path error carries stderr_tail (and exit_code)
    # but no full_summary. The renderer must surface the real stderr instead of
    # leaving detail empty (the "Check stderr output for details" regression).
    b = nv.NarrativeBuilder()
    b.add(ev("e1", "error", category="terminated",
             message="claude agent terminated with an error (cause unknown)",
             detail={"stderr_tail": "Error: Session ID abc is already in use.",
                     "exit_code": 1}))
    err = next(k for k in b.tree.root.children if isinstance(k, nv.ErrorNode))
    assert "Session ID abc is already in use." in err.detail
    assert "exit code 1" in err.detail

    # stderr_tail with no exit_code → detail is just the tail.
    b2 = nv.NarrativeBuilder()
    b2.add(ev("e2", "error", category="unknown", message="boom",
              detail={"stderr_tail": "boom on stderr"}))
    err2 = next(k for k in b2.tree.root.children if isinstance(k, nv.ErrorNode))
    assert err2.detail == "boom on stderr"

    # full_summary still wins when present (LoopError path unchanged).
    b3 = nv.NarrativeBuilder()
    b3.add(ev("e3", "error", category="loop", message="x",
              detail={"full_summary": "the full summary", "stderr_tail": "ignored"}))
    err3 = next(k for k in b3.tree.root.children if isinstance(k, nv.ErrorNode))
    assert err3.detail == "the full summary"


def test_latest_context_tokens_tracks_most_recent_turn_not_cumulative() -> None:
    b = nv.NarrativeBuilder()
    b.add(ev("1", "quest_created", id="z9gixx", title="streaming toolbar"))
    b.add(ev("2", "step_started", step="s", kind="agent", phase="p", model="sonnet"))
    assert b.latest_context_tokens is None          # no agent_message yet
    b.add(ev("3", "agent_message", agent_id="step:p/s", provider="anthropic",
             model="sonnet",
             usage={"input_tokens": 100, "cache_read_input_tokens": 20,
                    "cache_creation_input_tokens": 5, "output_tokens": 9},
             raw={"content": []}))
    assert b.latest_context_tokens == 125          # input + both caches, this turn only
    b.add(ev("4", "agent_message", agent_id="step:p/s", provider="anthropic",
             model="sonnet",
             usage={"input_tokens": 200, "output_tokens": 4}, raw={"content": []}))
    assert b.latest_context_tokens == 200          # latest turn REPLACES, does not sum


def test_latest_context_tokens_keeps_last_value_when_neither_key_is_present() -> None:
    # Neither context_tokens nor usage: nothing resolves, so the merged slot holds
    # the last good occupancy instead of blanking the gauge.
    b = nv.NarrativeBuilder()
    b.add(ev("1", "agent_message", agent_id="step:p/s", provider="anthropic", model="m",
             usage={"input_tokens": 42}, raw={"content": []}))
    b.add(ev("2", "agent_message", agent_id="step:p/s", provider="anthropic", model="m",
             raw={"content": []}))                 # no usage key at all
    assert b.latest_context_tokens == 42           # untouched by a usage-less turn


def test_subagent_agent_message_does_not_move_latest_context_tokens() -> None:
    # A subagent turn must NOT flip the context bar to its own prompt count, but
    # its tokens must still fold into the cumulative change total (F2).
    b = nv.NarrativeBuilder()
    b.add(ev("st", "step_started", step="s", kind="agent", phase="p",
             model="m", provider="anthropic"))
    # A top-level turn sets the bar.
    b.add(ev("p1", "agent_message", agent_id="step:p/s", provider="anthropic",
             model="m", stop_reason="end_turn",
             usage={"input_tokens": 100, "output_tokens": 5}, raw={"content": []}))
    assert b.latest_context_tokens == 100
    # Open a subagent fold so the sub: turn routes through a registered child.
    b.add(ev("tk1", "tool_call", agent_id="step:p/s", id="b1", name="sub_agent",
             input={"subagent_name": "reviewer", "task_description": "go"}))
    # The subagent turn carries usage but a sub: agent_id — the bar must not move.
    b.add(ev("s1", "agent_message", agent_id="sub:tk1", provider="anthropic",
             model="m", stop_reason="end_turn",
             usage={"input_tokens": 999, "output_tokens": 7}, raw={"content": []}))
    assert b.latest_context_tokens == 100       # frozen at the parent's last value


def test_current_activity_carries_current_step_model() -> None:
    b = nv.NarrativeBuilder()
    b.add(ev("1", "step_started", step="s", kind="agent", phase="p",
             model="claude-sonnet-4-5"))
    act = b.current_activity()
    assert act is not None
    assert act.model == "claude-sonnet-4-5"


def test_current_activity_model_none_without_a_step() -> None:
    b = nv.NarrativeBuilder()
    # quest_created opens the root but starts no step; model has no source.
    b.add(ev("1", "quest_created", id="z9gixx", title="t"))
    act = b.current_activity()
    assert act is None or act.model is None


def test_seeded_opening_intent_reconciles_in_place_at_root() -> None:
    b = nv.NarrativeBuilder()
    b.seed_opening_intent("typed text")
    b.add({"id": "oi", "agent_id": None, "type": "user_message",
         "payload": {"role": "user", "subtype": "intent",
                     "content": [{"type": "text", "text": "authoritative text"}]}})
    b.add(_ev("s1", "step_started", step="spec", kind="agent", phase="design",
              model="m", provider="anthropic"))
    tree = b.tree
    intents = [c for c in tree.root.children if isinstance(c, nv.UserMessageNode)]
    # Exactly one node at root, carrying the AUTHORITATIVE event text (not the seed).
    assert len(intents) == 1
    assert intents[0].text == "authoritative text"
    assert intents[0].interactive is True
    phase = next(c for c in tree.root.children if isinstance(c, nv.PhaseNode))
    step = next(c for c in phase.children if isinstance(c, nv.StepNode))
    assert not any(isinstance(c, nv.UserMessageNode) for c in step.children)


def test_unseeded_opening_intent_still_creates_single_node() -> None:
    b = nv.NarrativeBuilder()
    b.add({"id": "oi", "agent_id": None, "type": "user_message",
         "payload": {"role": "user", "subtype": "intent",
                     "content": [{"type": "text", "text": "add a --json flag"}]}})
    b.add(_ev("s1", "step_started", step="spec", kind="agent", phase="design",
              model="m", provider="anthropic"))
    tree = b.tree
    intents = [c for c in tree.root.children if isinstance(c, nv.UserMessageNode)]
    # One node at root — never re-homed under the step, even unseeded.
    assert len(intents) == 1
    assert intents[0].text == "add a --json flag"
    assert intents[0].interactive is True
    phase = next(c for c in tree.root.children if isinstance(c, nv.PhaseNode))
    step = next(c for c in phase.children if isinstance(c, nv.StepNode))
    assert not any(isinstance(c, nv.UserMessageNode) for c in step.children)


def test_seeded_pending_reply_reconciles_into_single_node() -> None:
    b = nv.NarrativeBuilder()
    b.add(_ev("s1", "step_started", step="spec", kind="agent", phase="design",
              model="m", provider="anthropic"))
    b.add(_ev("aw", "awaiting_user_input", agent_id="step:design/spec"))
    b.seed_pending_reply("typed reply")
    b.add(_ev("um", "user_message", agent_id="step:design/spec", role="user",
              content=[{"type": "text", "text": "authoritative reply"}]))
    tree = b.tree
    phase = next(c for c in tree.root.children if isinstance(c, nv.PhaseNode))
    step = next(c for c in phase.children if isinstance(c, nv.StepNode))
    replies = [c for c in step.children if isinstance(c, nv.UserMessageNode)]
    # One node only, carrying the AUTHORITATIVE event text (not the seed).
    assert len(replies) == 1
    assert replies[0].text == "authoritative reply"
    assert replies[0].interactive is True


def test_seed_pending_reply_second_seed_while_outstanding_is_noop() -> None:
    b = nv.NarrativeBuilder()
    b.add(_ev("s1", "step_started", step="spec", kind="agent", phase="design",
              model="m", provider="anthropic"))
    b.add(_ev("aw", "awaiting_user_input", agent_id="step:design/spec"))
    b.seed_pending_reply("my answer")
    b.seed_pending_reply("my answer")       # single-outstanding: no second node
    phase = next(c for c in b.tree.root.children if isinstance(c, nv.PhaseNode))
    step = next(c for c in phase.children if isinstance(c, nv.StepNode))
    replies = [c for c in step.children if isinstance(c, nv.UserMessageNode)]
    assert len(replies) == 1
    assert replies[0].text == "my answer"
    # After the authoritative user_message reconciles, the pointer clears, so a
    # later distinct reply seeds fresh (the guard must not block future turns).
    b.add(_ev("um", "user_message", agent_id="step:design/spec", role="user",
              content=[{"type": "text", "text": "my answer"}]))
    b.seed_pending_reply("next turn")
    replies2 = [c for c in step.children if isinstance(c, nv.UserMessageNode)]
    assert len(replies2) == 2
    assert replies2[1].text == "next turn"


def test_replayed_user_message_leaves_optimistic_echo_armed() -> None:
    b = nv.NarrativeBuilder()
    b.add(_ev("s1", "step_started", step="planner", kind="agent", phase="plan",
              model="m", provider="anthropic"))
    b.seed_pending_reply("my answer")            # armed at submit, before replay
    replay = _ev("um", "user_message", agent_id="step:plan/planner", role="user",
                 content=[{"type": "text", "text": "an earlier turn"}])
    replay["_replay"] = True
    b.add(replay)
    assert b._pending_reply_node is not None      # a replayed turn never consumes it
    # The armed echo pairs only with the LIVE turn, reconciling in place.
    b.add(_ev("um2", "user_message", agent_id="step:plan/planner", role="user",
              content=[{"type": "text", "text": "my answer"}]))
    assert b._pending_reply_node is None


def test_replayed_opening_intent_reconciles_into_optimistic_echo() -> None:
    b = nv.NarrativeBuilder()
    b.seed_opening_intent("typed text")
    replay = {"id": "oi", "agent_id": None, "type": "user_message",
              "payload": {"role": "user", "subtype": "intent",
                          "content": [{"type": "text", "text": "authoritative text"}]}}
    replay["_replay"] = True
    b.add(replay)
    intents = [c for c in b.tree.root.children if isinstance(c, nv.UserMessageNode)]
    assert len(intents) == 1
    assert intents[0].text == "authoritative text"
    assert intents[0].interactive is True
    assert b._opening_intent_node is intents[0]


def test_append_committed_reply_is_non_reconciling() -> None:
    b = nv.NarrativeBuilder()
    b.add(_ev("s1", "step_started", step="spec", kind="agent", phase="design",
              model="m", provider="anthropic"))
    b.add(_ev("aw", "awaiting_user_input", agent_id="step:design/spec"))
    # A control-command echo: committed, arms NO pending reconciliation pointer.
    b.append_committed_reply("/model claude-sonnet-4-5")
    assert b._pending_reply_node is None
    # A subsequent normal reply reconciles into its OWN pending node, leaving the
    # committed control echo intact as a separate node.
    b.seed_pending_reply("real reply")
    b.add(_ev("um", "user_message", agent_id="step:design/spec", role="user",
              content=[{"type": "text", "text": "real reply"}]))
    tree = b.tree
    phase = next(c for c in tree.root.children if isinstance(c, nv.PhaseNode))
    step = next(c for c in phase.children if isinstance(c, nv.StepNode))
    replies = [c for c in step.children if isinstance(c, nv.UserMessageNode)]
    assert [r.text for r in replies] == ["/model claude-sonnet-4-5", "real reply"]
    assert all(r.interactive for r in replies)


def test_unseeded_reply_appends_as_today() -> None:
    b = nv.NarrativeBuilder()
    b.add(_ev("s1", "step_started", step="spec", kind="agent", phase="design",
              model="m", provider="anthropic"))
    b.add(_ev("aw", "awaiting_user_input", agent_id="step:design/spec"))
    b.add(_ev("um", "user_message", agent_id="step:design/spec", role="user",
              content=[{"type": "text", "text": "the reply"}]))
    tree = b.tree
    phase = next(c for c in tree.root.children if isinstance(c, nv.PhaseNode))
    step = next(c for c in phase.children if isinstance(c, nv.StepNode))
    replies = [c for c in step.children if isinstance(c, nv.UserMessageNode)]
    assert len(replies) == 1
    assert replies[0].text == "the reply"
    assert replies[0].interactive is True


def test_builder_prefers_the_reported_context_tokens_over_the_billing_sum() -> None:
    from imp.cli import narrative as nv

    b = nv.NarrativeBuilder()
    assert b.latest_context_tokens is None
    assert b.latest_context_window is None

    # A lone tool_use carrier (no prose) — exercises the add() step-0 capture
    # without needing an open container. The billing sum (82628) runs well above
    # the live occupancy the provider reported, so the slot must take the latter.
    b.add({
        "type": "agent_message", "agent_id": None,
        "payload": {
            "provider": "claude", "model": "opus-1m",
            "stop_reason": "tool_use",
            "raw": {"content": [{"type": "tool_use", "id": "t1",
                                 "name": "bash", "input": {}}],
                    "stop_reason": "tool_use", "usage": {}},
            "usage": {"input_tokens": 82_628},
            "context_tokens": 137_000, "context_window": 1_000_000,
        },
    })

    assert b.latest_context_tokens == 137_000
    assert b.latest_context_window == 1_000_000


def test_subagent_agent_message_does_not_move_context_tokens_or_window() -> None:
    # A subagent turn carries its own session occupancy and window; like the
    # billing sum, they must NOT flip the top-level context bar.
    b = nv.NarrativeBuilder()
    b.add(ev("st", "step_started", step="s", kind="agent", phase="p",
             model="m", provider="anthropic"))
    b.add(ev("p1", "agent_message", agent_id="step:p/s", provider="claude",
             model="opus-1m", stop_reason="end_turn", usage={},
             raw={"content": []}, context_tokens=125_000,
             context_window=1_000_000))
    assert b.latest_context_tokens == 125_000
    assert b.latest_context_window == 1_000_000
    # Open a subagent fold so the sub: turn routes through a registered child.
    b.add(ev("tk1", "tool_call", agent_id="step:p/s", id="b1", name="sub_agent",
             input={"subagent_name": "reviewer", "task_description": "go"}))
    b.add(ev("s1", "agent_message", agent_id="sub:tk1", provider="claude",
             model="opus-1m", stop_reason="end_turn", usage={},
             raw={"content": []}, context_tokens=190_000, context_window=200_000))
    assert b.latest_context_tokens == 125_000    # frozen at the parent's value
    assert b.latest_context_window == 1_000_000


def test_step_node_reads_position_label_from_step_started() -> None:
    b = nv.NarrativeBuilder()
    b.add(ev("1", "quest_created", quest_id=1, title="x"))
    b.add(ev("2", "step_started", step="task-lead", kind="agent", phase="code",
             model="m", provider="anthropic", label="task 1/3",
             phase_label="implementation phase · plan 1/1", position_label=True))
    top = b._step_stack[-1]
    assert top.position_label is True


def test_step_node_position_label_defaults_false() -> None:
    b = nv.NarrativeBuilder()
    b.add(ev("1", "quest_created", quest_id=1, title="x"))
    b.add(ev("2", "step_started", step="plan-writer", kind="agent", phase="plan",
             model="m", provider="anthropic", label="writing plan",
             phase_label="planning phase · plan 1/1"))
    assert b._step_stack[-1].position_label is False


def test_current_activity_burst_detail_concatenates_count_and_inflight_tool() -> None:
    b = nv.NarrativeBuilder()
    b.add(ev("1", "quest_created", quest_id=1, title="x"))
    b.add(ev("2", "step_started", step="plan-writer", kind="agent", phase="plan",
             model="m", provider="anthropic", label="writing plan",
             phase_label="planning phase · plan 1/1"))
    b.add(ev("3", "tool_call", agent_id="a", id="b1", name="read_file",
             input={"path": "config.py"}))
    act = b.current_activity()
    assert act.verb == "writing plan"               # static activity label (verb step)
    assert act.detail == "1 call · read_file config.py"
    assert act.position == "planning phase · plan 1/1"


def test_current_activity_position_step_is_thinking_with_counter_on_position() -> None:
    b = nv.NarrativeBuilder()
    b.add(ev("1", "quest_created", quest_id=1, title="x"))
    b.add(ev("2", "step_started", step="task-lead", kind="agent", phase="code",
             model="m", provider="anthropic", label="task 2/3",
             phase_label="implementation phase · plan 1/1", position_label=True))
    act = b.current_activity()
    assert act.verb == "thinking"                   # position step, no static verb
    assert act.position == "implementation phase · plan 1/1 · task 2/3"
    assert act.detail is None


def test_current_activity_running_subagent_label_is_verb_nested_tool_is_detail() -> None:
    b = nv.NarrativeBuilder()
    b.add(ev("1", "quest_created", quest_id=1, title="x"))
    b.add(ev("2", "step_started", step="task-lead", kind="agent", phase="code",
             model="m", provider="anthropic", label="task 1/3",
             phase_label="implementation phase · plan 1/1", position_label=True))
    b.add(ev("3", "tool_call", agent_id="step:code/task-lead", id="t1", name="sub_agent",
             input={"subagent_name": "coder"}, label="writing code"))
    b.add(ev("4", "tool_call", agent_id="sub:3", id="n1", name="bash",
             input={"command": "pytest"}))
    act = b.current_activity()
    assert act.verb == "writing code"               # subagent frontmatter label
    assert act.detail is not None and "bash pytest" in act.detail
    assert act.position == "implementation phase · plan 1/1 · task 1/3"


def test_current_activity_script_step_verb_is_the_label() -> None:
    b = nv.NarrativeBuilder()
    b.add(ev("1", "quest_created", quest_id=1, title="x"))
    b.add(ev("2", "step_started", step="verify", kind="script", phase="code",
             label="running tests", phase_label="implementation phase · plan 1/1",
             command="pytest -q"))
    act = b.current_activity()
    assert act.verb == "running tests"
    assert act.position == "implementation phase · plan 1/1"


def test_current_activity_two_running_subagents_show_the_count_not_a_detail() -> None:
    b = nv.NarrativeBuilder()
    b.add(ev("1", "quest_created", quest_id=1, title="x"))
    b.add(ev("2", "step_started", step="task-lead", kind="agent", phase="code",
             model="m", provider="anthropic", label="task 1/3",
             phase_label="implementation phase · plan 1/1", position_label=True))
    b.add(ev("3", "tool_call", agent_id="step:code/task-lead", id="t1", name="sub_agent",
             input={"subagent_name": "coder"}, label="writing code"))
    b.add(ev("4", "tool_call", agent_id="step:code/task-lead", id="t2", name="sub_agent",
             input={"subagent_name": "code-reviewer"}, label="reviewing code"))
    act = b.current_activity()
    assert act.verb == "running 2 subagents"              # count, not the first fold's verb
    assert act.detail is None or "+" not in (act.detail or "")   # no "+N subagents" tail


def test_query_result_renders_without_fallback() -> None:
    tree = nv.build_tree([
        ev("c", "quest_created", title="t"),
        ev("s", "step_started", step="s", kind="agent", phase="p"),
        ev("q", "query_result", agent_id="step:p/s",
           total_cost_usd=0.0, num_turns=1, usage={}),
    ])

    def _walk(node):
        yield node
        for c in getattr(node, "children", []):
            yield from _walk(c)

    assert not any(isinstance(n, nv.FallbackLine) for n in _walk(tree.root))


def test_claude_turn_end_detection_uses_real_stop_reason() -> None:
    # The coalesced claude llm_response now carries a real stop_reason, so
    # turn-end detection works through the standard frozenset path.
    assert nv._agent_message_is_turn_end(
        {"provider": "claude", "stop_reason": "end_turn"}) is True
    assert nv._agent_message_is_turn_end(
        {"provider": "claude", "stop_reason": None}) is False


def test_builder_has_no_checkpoint_threshold_capture() -> None:
    # The cutoff is model-derived now; nothing reads a configured threshold.
    b = nv.NarrativeBuilder()
    b.add({"type": "tiers_resolved", "agent_id": None,
           "payload": {"tiers": {}, "steps": {}, "checkpoint_threshold": 0.65}})
    assert not hasattr(b, "latest_checkpoint_threshold")
    assert not hasattr(b, "_latest_checkpoint_threshold")


def test_usage_prompt_tokens_sums_input_and_both_cache_buckets() -> None:
    usage = {"input_tokens": 100, "cache_read_input_tokens": 20,
             "cache_creation_input_tokens": 5}
    assert nv.usage_prompt_tokens(usage) == 125


def test_usage_prompt_tokens_none_when_empty() -> None:
    assert nv.usage_prompt_tokens(None) is None
    assert nv.usage_prompt_tokens({}) is None


def test_fmt_ctx_tokens_rounds_to_the_nearest_thousand() -> None:
    assert nv.fmt_ctx_tokens(0) == "0"
    assert nv.fmt_ctx_tokens(999) == "999"
    assert nv.fmt_ctx_tokens(1000) == "1k"
    assert nv.fmt_ctx_tokens(12_000) == "12k"
    assert nv.fmt_ctx_tokens(108_528) == "109k"     # rounds, never truncates
    assert nv.fmt_ctx_tokens(200_000) == "200k"


def test_resume_continuation_preserves_pre_interrupt_children() -> None:
    # restarted=False: a continued resume keeps the pre-interrupt rendered work,
    # appends the post-resume turns, and retains the original start clock.
    seed = [
        ev("c", "quest_created", quest_id=1, title="t"),
        ev("s", "step_started", step="build", kind="agent", phase="design",
           model="m", provider="anthropic", timestamp="2026-06-28T00:00:00+00:00"),
        ev("ll1", "agent_message", agent_id="step:design/build", provider="anthropic",
           model="m", stop_reason="end_turn", usage={},
           raw={"content": [{"type": "text", "text": "partial attempt"}]}),
        ev("wp", "workflow_paused", reason="ctrl-c"),
        ev("wr", "workflow_resumed"),
    ]
    live = [
        ev("sb", "step_started", step="build", kind="agent", phase="design",
           model="m", provider="anthropic", resumed=True, restarted=False,
           timestamp="2026-06-28T00:05:00+00:00"),
        ev("ll2", "agent_message", agent_id="step:design/build", provider="anthropic",
           model="m", stop_reason="end_turn", usage={},
           raw={"content": [{"type": "text", "text": "fresh complete"}]}),
        ev("sc", "step_completed", step="build", kind="agent",
           action={"verb": "continue"}, var_mutations={}),
    ]
    tree = nv.build_tree(seed + live)
    phases = [c for c in tree.root.children if isinstance(c, nv.PhaseNode)]
    assert len(phases) == 1
    build = next(c for c in phases[0].children if isinstance(c, nv.StepNode))
    texts = [c.text for c in build.children if isinstance(c, nv.AgentTextNode)]
    assert texts == ["partial attempt", "fresh complete"]   # BOTH preserved
    assert build.start_ts == "2026-06-28T00:00:00+00:00"    # original clock kept
    assert build.closed is True
    assert build.resumed is False
    assert [type(c) for c in build.children
            if isinstance(c, (nv.AgentTextNode, nv.StepHeaderNode))] == [
        nv.AgentTextNode, nv.StepHeaderNode, nv.AgentTextNode]
    assert _headers(build) == [nv.StepHeaderNode(
        label="build", resumed=True, phase_label=None, model="m", route_label=None)]


def test_resume_restart_clears_pre_interrupt_children() -> None:
    # restarted=True: a genuine re-run from an empty/never-ran transcript wipes
    # the partial and restamps the clock (preserving pre-change behavior).
    seed = [
        ev("c", "quest_created", quest_id=1, title="t"),
        ev("s", "step_started", step="build", kind="agent", phase="design",
           model="m", provider="anthropic", timestamp="2026-06-28T00:00:00+00:00"),
        ev("ll1", "agent_message", agent_id="step:design/build", provider="anthropic",
           model="m", stop_reason="end_turn", usage={},
           raw={"content": [{"type": "text", "text": "partial attempt"}]}),
        ev("wp", "workflow_paused", reason="ctrl-c"),
        ev("wr", "workflow_resumed"),
    ]
    live = [
        ev("sb", "step_started", step="build", kind="agent", phase="design",
           model="m", provider="anthropic", resumed=True, restarted=True,
           timestamp="2026-06-28T00:05:00+00:00"),
        ev("ll2", "agent_message", agent_id="step:design/build", provider="anthropic",
           model="m", stop_reason="end_turn", usage={},
           raw={"content": [{"type": "text", "text": "fresh complete"}]}),
        ev("sc", "step_completed", step="build", kind="agent",
           action={"verb": "continue"}, var_mutations={}),
    ]
    tree = nv.build_tree(seed + live)
    build = next(c for p in tree.root.children if isinstance(p, nv.PhaseNode)
                 for c in p.children if isinstance(c, nv.StepNode))
    texts = [c.text for c in build.children if isinstance(c, nv.AgentTextNode)]
    assert texts == ["fresh complete"]                      # partial wiped
    assert build.start_ts == "2026-06-28T00:05:00+00:00"    # clock restamped


def test_error_node_uses_last_agent_text_when_no_full_summary_or_stderr() -> None:
    b = nv.NarrativeBuilder()
    b.add(ev("e1", "error", category="terminated",
             message="claude agent terminated with an error (cause unknown)",
             detail={"last_agent_text": "API Error 400 — tool use concurrency"}))
    err = next(k for k in b.tree.root.children if isinstance(k, nv.ErrorNode))
    assert err.detail == "API Error 400 — tool use concurrency"


def test_error_node_full_summary_wins_over_last_agent_text() -> None:
    b = nv.NarrativeBuilder()
    b.add(ev("e1", "error", category="loop", message="loop guard",
             detail={"full_summary": "the full summary",
                     "last_agent_text": "secondary text"}))
    err = next(k for k in b.tree.root.children if isinstance(k, nv.ErrorNode))
    assert err.detail == "the full summary"


def test_max_tokens_truncated_event_renders_no_fallback_node() -> None:
    tree = nv.build_tree(_step_events(
        _ev("1", "max_tokens_truncated", agent_id="step:p/s",
            tier="high", model="claude-sonnet-4-6", max_tokens=32000,
            output_tokens=32000, thinking_tokens=5000),
    ))
    step = _only_step(tree)
    assert not [c for c in step.children if isinstance(c, nv.FallbackLine)]


def _all_literals(node):
    out = []
    if isinstance(node, nv.LiteralTextNode):
        out.append(node)
    for child in getattr(node, "children", []):
        out.extend(_all_literals(child))
    return out


def test_waiting_for_test_lane_leaves_no_lifecycle_line() -> None:
    b = nv.NarrativeBuilder()
    b.add(_ev("0", "quest_created", quest_id=1, title="x"))
    b.add(_ev("1", "step_started", step="verify", kind="script", phase="build"))
    b.add(_ev("2", "waiting_for_test_lane", step="verify"))
    lits = _all_literals(b.tree.root)
    assert not any(n.text == "waiting to test" for n in lits)


def test_current_activity_position_segments_for_a_plan_phase_step() -> None:
    b = nv.NarrativeBuilder()
    b.add(ev("1", "quest_created", quest_id=1, title="x"))
    b.add(ev("2", "step_started", step="plan-writer", kind="agent", phase="plan",
             model="m", provider="anthropic", label="writing plan",
             phase_label="planning phase · plan 3/6"))
    act = b.current_activity()
    assert act.phase == "planning phase"      # phase head (step-blue)
    assert act.plan == "plan 3/6"             # plan-counter tail (step-blue)
    assert act.counter == "writing plan"      # every step's label rides the position row
    assert act.position == "planning phase · plan 3/6"   # composed string unchanged


def test_current_activity_position_segments_for_a_position_label_step() -> None:
    b = nv.NarrativeBuilder()
    b.add(ev("1", "quest_created", quest_id=1, title="x"))
    b.add(ev("2", "step_started", step="task-lead", kind="agent", phase="code",
             model="m", provider="anthropic", label="task 2/3",
             phase_label="implementation phase · plan 1/1", position_label=True))
    act = b.current_activity()
    assert act.phase == "implementation phase"
    assert act.plan == "plan 1/1"
    assert act.counter == "task 2/3"          # the position_label counter leaf (step-blue)


def test_current_activity_counter_prefers_pushed_label() -> None:
    b = nv.NarrativeBuilder()
    b.add(ev("1", "quest_created", quest_id=1, title="x"))
    b.add(ev("2", "step_started", step="task-lead", kind="agent", phase="code",
             model="m", provider="anthropic", label="task 2/3",
             phase_label="implementation phase · plan 1/1", position_label=True))
    b.add(ev("3", "step_label_updated", label="task 3/3"))
    act = b.current_activity()
    assert act.counter == "task 3/3"
    assert act.phase == "implementation phase"
    assert act.plan == "plan 1/1"


def test_current_activity_position_segments_when_phase_has_no_plan() -> None:
    b = nv.NarrativeBuilder()
    b.add(ev("1", "quest_created", quest_id=1, title="x"))
    b.add(ev("2", "step_started", step="spec", kind="agent", phase="design",
             model="m", provider="anthropic", label="writing spec",
             phase_label="design phase"))
    act = b.current_activity()
    assert act.phase == "design phase"
    assert act.plan is None                   # no plan counter in the phase label
    assert act.counter == "writing spec"      # every step's label rides the position row


def test_current_activity_counter_carries_a_non_position_step_label() -> None:
    b = nv.NarrativeBuilder()
    b.add(ev("1", "quest_created", quest_id=1, title="x"))
    b.add(ev("2", "step_started", step="verify", kind="script",
             phase="integration", label="running tests",
             phase_label="integration phase"))
    act = b.current_activity()
    assert act.counter == "running tests"     # a non-position step's label rides the position row
    assert act.phase == "integration phase"
    assert act.plan is None
    assert act.position == "integration phase"  # composed breadcrumb string unchanged


def test_current_activity_counter_prefers_pushed_label_for_non_position_step() -> None:
    b = nv.NarrativeBuilder()
    b.add(ev("1", "quest_created", quest_id=1, title="x"))
    b.add(ev("2", "step_started", step="verify", kind="script",
             phase="integration", label="running tests",
             phase_label="integration phase"))
    b.add(ev("3", "step_label_updated", label="running 42 tests"))
    act = b.current_activity()
    assert act.counter == "running 42 tests"


def test_continuation_folds_into_injected_message_node() -> None:
    tree = nv.build_tree([
        _ev("1", "step_started", step="s", kind="agent", phase="p"),
        {"id": "2", "agent_id": None, "type": "engine_message",
         "payload": {"role": "user", "subtype": "continuation",
                     "content": [{"type": "text",
                                  "text": "continue from where you left off"}]}},
    ])
    step = tree.root.children[0].children[0]
    # No FallbackLine (raw fallthrough would leak the nudge as a plain line).
    assert [c for c in step.children if isinstance(c, nv.FallbackLine)] == []
    # The continuation folds into one EngineMessageNode carrying the text.
    assert len(step.children) == 1
    node = step.children[0]
    assert isinstance(node, nv.EngineMessageNode)
    assert node.subtype == "continuation"
    assert node.text == "continue from where you left off"


def test_test_lane_acquired_emits_no_visible_line() -> None:
    b = nv.NarrativeBuilder()
    b.add(_ev("0", "quest_created", quest_id=1, title="x"))
    b.add(_ev("1", "step_started", step="verify", kind="script", phase="build"))
    before = len(_all_literals(b.tree.root))
    b.add(_ev("2", "test_lane_acquired", step="verify"))
    assert len(_all_literals(b.tree.root)) == before   # silent resolution

    def _walk(node):
        yield node
        for c in getattr(node, "children", []):
            yield from _walk(c)

    fbs = [n for n in _walk(b.tree.root)
           if isinstance(n, nv.FallbackLine) and n.event_type == "test_lane_acquired"]
    assert fbs == []


def test_seed_first_spinner_arms_preboot_activity(monkeypatch) -> None:
    monkeypatch.setattr("imp.agents.random_status.random_status_phrase",
                        lambda: "scheming")
    b = nv.NarrativeBuilder()
    b.add(ev("1", "quest_created", quest_id=1, title="x"))
    # No step/phase yet: footer is dark and the seed is unset (TUI/headless default).
    assert b._seed_phrase is None
    assert b.current_activity() is None
    # Arming the pre-boot spinner produces a bare rotating-phrase activity.
    b.seed_first_spinner()
    assert b.current_activity() == nv.Activity(verb="scheming")


def test_seed_first_spinner_phrase_is_drawn_once(monkeypatch) -> None:
    draws = iter(["rot1", "rot2", "rot3"])
    monkeypatch.setattr("imp.agents.random_status.random_status_phrase",
                        lambda: next(draws))
    b = nv.NarrativeBuilder()
    b.add(ev("1", "quest_created", quest_id=1, title="x"))
    b.seed_first_spinner()
    # The footer redraws ~10x/s; the verb must stay stable across redraws
    # (drawn once at arm time, not re-randomized per current_activity() call).
    assert b.current_activity().verb == "rot1"
    assert b.current_activity().verb == "rot1"


def test_started_step_supersedes_seeded_spinner(monkeypatch) -> None:
    draws = iter(["seed-phrase", "step-phrase"])
    monkeypatch.setattr("imp.agents.random_status.random_status_phrase",
                        lambda: next(draws))
    b = nv.NarrativeBuilder()
    b.add(ev("1", "quest_created", quest_id=1, title="x"))
    b.seed_first_spinner()
    assert b.current_activity().verb == "seed-phrase"
    # The daemon's first rotating step draws its own phrase and takes precedence
    # over the tail seed branch — the seed is never consulted again (no teardown).
    b.add(ev("2", "step_started", step="threshold", kind="agent",
             phase="threshold", model="m", provider="anthropic",
             label="frozen0", rotate_phrase=True))
    assert b.current_activity().verb == "step-phrase"


def test_active_phase_supersedes_seeded_spinner(monkeypatch) -> None:
    monkeypatch.setattr("imp.agents.random_status.random_status_phrase",
                        lambda: "seed-phrase")
    b = nv.NarrativeBuilder()
    b.add(ev("1", "quest_created", quest_id=1, title="x"))
    b.seed_first_spinner()
    assert b.current_activity().verb == "seed-phrase"
    # Open a phase then end its step: _current_phase stays set with no running step.
    b.add(ev("2", "step_started", step="threshold", kind="agent",
             phase="threshold", model="m", provider="anthropic"))
    b.add(ev("3", "step_completed", step="threshold", kind="agent",
             action={"verb": "continue"}, var_mutations={}))
    # The _current_phase guard sits ABOVE the seed branch in current_activity(),
    # so an active phase (step ended) supersedes the seed with "working".
    assert b.current_activity() == nv.Activity(verb="working")


def test_seed_identity_sets_root_id_and_title() -> None:
    b = nv.NarrativeBuilder()
    b.seed_identity("lufu50", "Instant opening identity bar")
    assert b.tree.root.id == "lufu50"
    assert b.tree.root.title == "Instant opening identity bar"


def test_seed_identity_empty_title_keeps_title_empty() -> None:
    b = nv.NarrativeBuilder()
    b.seed_identity("lufu50", "")
    assert b.tree.root.id == "lufu50"
    assert b.tree.root.title == ""


def test_seeded_identity_survives_quest_created_replay_unchanged() -> None:
    b = nv.NarrativeBuilder()
    b.seed_identity("lufu50", "Instant opening identity bar")
    assert b.tree.root.id == "lufu50"                     # set pre-replay by seed_identity
    assert b.tree.root.title == "Instant opening identity bar"
    b.add({"id": "e1", "agent_id": None, "type": "quest_created",
           "payload": {"id": "lufu50", "title": "Instant opening identity bar"}})
    assert b.tree.root.id == "lufu50"                     # replay supersedes with identical values
    assert b.tree.root.title == "Instant opening identity bar"


def test_agent_message_event_renders_prose_node():
    tree = nv.build_tree([
        _ev("1", "step_started", step="plan", kind="agent", phase="build",
            model="m", provider="anthropic"),
        _ev("a1", "agent_message", agent_id="step:build/plan",
            provider="anthropic", model="m", stop_reason="end_turn", usage={},
            raw={"content": [{"type": "text", "text": "hello from the model"}]}),
    ])
    step = tree.root.children[0].children[0]
    texts = [c.text for c in step.children if isinstance(c, nv.AgentTextNode)]
    assert texts == ["hello from the model"]


def test_interject_is_single_interactive_human_turn():
    tree = nv.build_tree([
        _ev("u1", "user_message", subtype="interject", role="user",
            content=[{"type": "text", "text": "hold on"}]),
    ])
    ums = [c for c in tree.root.children if isinstance(c, nv.UserMessageNode)]
    assert len(ums) == 1
    assert ums[0].text == "hold on"
    assert ums[0].interactive is True


def test_engine_message_becomes_subtyped_engine_node():
    tree = nv.build_tree([
        _ev("1", "step_started", step="plan", kind="agent", phase="build",
            model="m", provider="anthropic"),
        _ev("e1", "engine_message", agent_id="step:build/plan",
            subtype="continuation", role="user",
            content=[{"type": "text", "text": "please continue"}]),
    ])
    step = tree.root.children[0].children[0]
    ems = [c for c in step.children if isinstance(c, nv.EngineMessageNode)]
    assert len(ems) == 1
    assert ems[0].subtype == "continuation"
    assert ems[0].text == "please continue"
    assert not any(isinstance(c, nv.UserMessageNode) for c in step.children)


def test_completion_engine_message_drops_bare_waiting_reply():
    tree = nv.build_tree([
        _ev("1", "step_started", step="plan", kind="agent", phase="build",
            model="m", provider="anthropic"),
        _ev("e1", "engine_message", agent_id="step:build/plan",
            subtype="completion", role="user",
            content=[{"type": "text", "text": "did you finish?"}]),
        _ev("a1", "agent_message", agent_id="step:build/plan",
            provider="anthropic", model="m", stop_reason="end_turn", usage={},
            raw={"content": [{"type": "text", "text": "WAITING"}]}),
    ])
    step = tree.root.children[0].children[0]
    assert not any(isinstance(c, nv.AgentTextNode) for c in step.children)


def test_activity_provider_defaults_to_none() -> None:
    assert nv.Activity(model="sonnet").provider is None


def test_current_activity_carries_the_step_provider_beside_the_model() -> None:
    # The gauge resolves limits per (provider, model), so the provider has to
    # ride the same Activity the model does.
    b = nv.NarrativeBuilder()
    b.add(ev("1", "quest_created", quest_id=1, title="x"))
    b.add(ev("2", "step_started", step="s", kind="agent", phase="p",
             model="gpt-6.1-sol", provider="openai"))
    act = b.current_activity()
    assert act is not None
    assert (act.provider, act.model) == ("openai", "gpt-6.1-sol")


def _open_three_level_builder() -> tuple[
    nv.NarrativeBuilder, nv.SubagentFold, nv.SubagentFold, nv.SubagentFold
]:
    b = nv.NarrativeBuilder()
    for event in [
        ev("st", "step_started", None, step="s", kind="agent", phase="p"),
        ev("parent", "tool_call", "step:p/s", id="parent-block", id_ns="block",
           name="sub_agent", input={"subagent_name": "parent"}),
        ev("sibling", "tool_call", "step:p/s", id="sibling-block", id_ns="block",
           name="sub_agent", input={"subagent_name": "sibling"}),
        ev("grandchild", "tool_call", "sub:parent", id="grandchild-block", id_ns="block",
           name="sub_agent", input={"subagent_name": "grandchild"}),
    ]:
        b.add(event)
    parent = b._sub_by_agent_id["sub:parent"]
    sibling = b._sub_by_agent_id["sub:sibling"]
    grandchild = parent.nested._sub_by_agent_id["sub:grandchild"]
    return b, parent, sibling, grandchild


@pytest.mark.parametrize(("error", "kind", "terminal"), [
    (False, None, None),
    (True, None, None),
    (True, "interrupted", "interrupted"),
])
def test_grandchild_task_result_closes_only_grandchild(
    error: bool, kind: str | None, terminal: str | None,
) -> None:
    b, parent, sibling, grandchild = _open_three_level_builder()
    b.add(ev("grandchild-done", "tool_result", "sub:parent", id="grandchild-block",
             id_ns="block", content="done", error=error, kind=kind))

    assert (grandchild.running, grandchild.is_error, grandchild.terminal_state) == (
        False, error, terminal)
    assert parent.running is True and sibling.running is True
    assert "sub:grandchild" not in b._descendant_builders
    assert b._retired_descendant_builders["sub:grandchild"] is grandchild.nested
    assert "sub:parent" in b._descendant_builders
    assert "sub:sibling" in b._descendant_builders


def test_active_nested_stamped_miss_surfaces_at_root() -> None:
    from imp.cli.render_text import render_tree

    b = nv.NarrativeBuilder()
    for event in [
        ev("st", "step_started", None, step="s", kind="agent", phase="p"),
        ev("parent-call", "tool_call", "step:p/s", id="parent-block", id_ns="block",
           name="sub_agent", input={"subagent_name": "parent"}),
        ev("child-call", "tool_call", "sub:parent-call", id="child-block", id_ns="block",
           name="sub_agent", input={"subagent_name": "child"}),
    ]:
        b.add(event)
    parent = b._sub_by_agent_id["sub:parent-call"]
    child = parent.nested._sub_by_agent_id["sub:child-call"]
    b.add(ev("child-done", "tool_result", "sub:parent-call", id="child-block",
             id_ns="block", content="child done", error=False))
    salvage_before = dict(b._salvage_result_owners)
    b.add(ev("child-duplicate", "tool_result", "sub:parent-call", id="child-block",
             id_ns="block", content="duplicate", error=False))
    step = b._step_stack[-1]
    warning = (
        "parent [sub:parent-call]: task result 'child-block' "
        "(id_ns='block') matched no open fold"
    )
    before_close = [n.message for n in step.children if isinstance(n, nv.ErrorNode)]

    b.add(ev("parent-done", "tool_result", "step:p/s", id="parent-block",
             id_ns="block", content="parent done", error=False))
    root_warnings = [n for n in step.children if isinstance(n, nv.ErrorNode)]
    parent_warnings = [n for n in parent.tree.root.children if isinstance(n, nv.ErrorNode)]
    child_warnings = [n for n in child.tree.root.children if isinstance(n, nv.ErrorNode)]
    lean = "\n".join(render_tree(b.tree, full=False, color=False))
    full = "\n".join(render_tree(b.tree, full=True, color=False))

    assert (
        before_close,
        [(n.message, n.is_warning) for n in root_warnings],
        parent_warnings,
        child_warnings,
        child.outcome,
        parent.outcome,
        set(b._retired_descendant_builders),
        b._salvage_result_owners,
        parent.nested._closed_sub_by_block,
        f"warning: {warning}" in lean,
        f"warning: {warning}" in full,
    ) == (
        [warning],
        [(warning, True)],
        [],
        [],
        "child done",
        "parent done",
        {"sub:parent-call", "sub:child-call"},
        salvage_before,
        {},
        True,
        True,
    )


def test_settled_child_stays_settled_when_open_sibling_is_paused() -> None:
    b, parent, sibling, grandchild = _open_three_level_builder()
    b.add(ev("sibling-done", "tool_result", "step:p/s", id="sibling-block",
             id_ns="block", content="settled", error=False))
    b.add(ev("ask", "awaiting_user_input", None, prompt="continue?"))
    b.add(ev("pause", "workflow_paused", None, reason="awaiting_user"))

    assert (sibling.outcome, sibling.is_error, sibling.terminal_state) == (
        "settled", False, None)
    assert parent.terminal_state == "interrupted"
    assert grandchild.terminal_state == "interrupted"


def test_normal_completion_retires_every_descendant_and_suppresses_stale_prose() -> None:
    b, parent, sibling, grandchild = _open_three_level_builder()
    b.add(ev("parent-done", "tool_result", "step:p/s", id="parent-block",
             id_ns="block", content="parent done", error=False))
    b.add(ev("sibling-done", "tool_result", "step:p/s", id="sibling-block",
             id_ns="block", content="sibling done", error=False))
    children_before = list(grandchild.tree.root.children)
    b.add(ev("stale", "agent_message", "sub:grandchild", provider="codex", usage={},
             raw={"content": [{"type": "text", "text": "too late"}]}))

    assert b._descendant_builders == {}
    assert set(b._retired_descendant_builders) == {
        "sub:parent", "sub:sibling", "sub:grandchild"}
    assert grandchild.tree.root.children == children_before
    assert parent.outcome == "parent done" and sibling.outcome == "sibling done"


@pytest.mark.parametrize(("etype", "payload", "expected", "warning"), [
    ("runner_warning", {"message": "runner slipped", "detail": "detail", "category": "runner"},
     "grandchild [sub:grandchild]: runner slipped", True),
    ("warning", {"message": "heads up", "detail": "detail", "category": "notice"},
     "grandchild [sub:grandchild]: heads up", True),
    ("sandbox_overlay_unavailable", {"reason": "overlay lost", "detail": "detail",
                                     "category": "sandbox"},
     "grandchild [sub:grandchild]: overlay lost", True),
    ("error", {"message": "boom", "detail": {"full_summary": "detail"},
               "category": "unknown"},
     "grandchild [sub:grandchild]: boom", False),
])
def test_retired_descendant_diagnostic_surfaces_at_root(
    etype: str, payload: dict, expected: str, warning: bool,
) -> None:
    b, _, _, grandchild = _open_three_level_builder()
    b.add(ev("parent-done", "tool_result", "step:p/s", id="parent-block",
             id_ns="block", content="done", error=False))
    before = list(grandchild.tree.root.children)
    b.add(ev("diagnostic", etype, "sub:grandchild", **payload))
    nodes = [n for n in b._step_stack[-1].children if isinstance(n, nv.ErrorNode)]

    assert nodes[-1].message == expected
    assert nodes[-1].is_warning is warning
    assert nodes[-1].category == payload.get("category")
    assert nodes[-1].detail == "detail"
    assert grandchild.tree.root.children == before


def test_crash_resume_recursively_retires_once_and_salvages_late_results() -> None:
    b, parent, sibling, grandchild = _open_three_level_builder()
    b.add(ev("resume", "workflow_resumed", None, timestamp="2026-01-01T00:00:05+00:00"))
    retired = dict(b._retired_descendant_builders)
    salvage = dict(b._salvage_result_owners)
    b.add(ev("resume-again", "workflow_resumed", None,
             timestamp="2026-01-01T00:00:06+00:00"))
    assert b._retired_descendant_builders == retired
    assert b._salvage_result_owners == salvage
    assert all(f.terminal_state == "interrupted" for f in (parent, sibling, grandchild))

    b.add(ev("grand-late", "tool_result", "sub:parent", id="grandchild-block",
             id_ns="block", content="late grand", error=False))
    assert grandchild.outcome == "late grand"
    assert parent.outcome is None and sibling.outcome is None


def test_awaiting_user_then_pause_uses_owner_qualified_late_result_salvage() -> None:
    b = nv.NarrativeBuilder()
    for event in [
        ev("st", "step_started", None, step="s", kind="agent", phase="p"),
        ev("parent", "tool_call", "step:p/s", id="parent-block", id_ns="block",
           name="sub_agent", input={"subagent_name": "parent"}),
        ev("sibling", "tool_call", "step:p/s", id="sibling-block", id_ns="block",
           name="sub_agent", input={"subagent_name": "sibling"}),
        ev("grand-a", "tool_call", "sub:parent", id="shared", id_ns="block",
           name="sub_agent", input={"subagent_name": "grand-a"}),
        ev("grand-b", "tool_call", "sub:sibling", id="shared", id_ns="block",
           name="sub_agent", input={"subagent_name": "grand-b"}),
    ]:
        b.add(event)
    grand_a = b._descendant_builders["sub:grand-a"]._owner_fold
    grand_b = b._descendant_builders["sub:grand-b"]._owner_fold
    assert grand_a is not None and grand_b is not None

    b.add(ev("ask", "awaiting_user_input", None, prompt="continue?"))
    assert not grand_a.running and not grand_b.running
    assert grand_a.outcome is None and grand_b.outcome is None
    assert "sub:grand-a" in b._descendant_builders
    b.add(ev("pause", "workflow_paused", None, reason="awaiting_user"))
    assert "sub:grand-a" not in b._descendant_builders
    assert set(b._salvage_result_owners) >= {
        ("sub:parent", "shared"), ("sub:sibling", "shared")}

    b.add(ev("wrong", "tool_result", "sub:grand-a", id="shared", id_ns="block",
             content="wrong", error=False))
    assert grand_a.outcome is None and grand_b.outcome is None
    errors = [n for n in b._step_stack[-1].children if isinstance(n, nv.ErrorNode)]
    assert errors[-1].message == (
        "grand-a [sub:grand-a]: task result 'shared' (id_ns='block') matched no open fold")
    b.add(ev("late-a", "tool_result", "sub:parent", id="shared", id_ns="block",
             content="late a", error=False))
    b.add(ev("late-b", "tool_result", "sub:sibling", id="shared", id_ns="block",
             content="late b", error=False))
    assert grand_a.outcome == "late a" and grand_b.outcome == "late b"
    b.add(ev("duplicate", "tool_result", "sub:parent", id="shared", id_ns="block",
             content="duplicate", error=False))
    errors = [n for n in b._step_stack[-1].children if isinstance(n, nv.ErrorNode)]
    assert errors[-1].message == (
        "parent [sub:parent]: task result 'shared' (id_ns='block') matched no open fold")
    assert grand_a.outcome == "late a" and grand_b.outcome == "late b"


def test_recursive_subagent_events_route_to_the_immediate_owner() -> None:
    b = nv.NarrativeBuilder()
    events = [
        ev("st", "step_started", None, step="s", kind="agent", phase="p"),
        ev("root-tool", "tool_call", "step:p/s", id="tool-1", name="bash", input={}),
        ev("root-result", "tool_result", "step:p/s", id="tool-1", content="root"),
        ev("parent", "tool_call", "step:p/s", id="parent-block", id_ns="block",
           name="sub_agent", input={"subagent_name": "parent"}),
        ev("sibling", "tool_call", "step:p/s", id="sibling-block", id_ns="block",
           name="sub_agent", input={"subagent_name": "sibling"}),
        ev("parent-tool", "tool_call", "sub:parent", id="tool-1", name="bash", input={}),
        ev("parent-tool-result", "tool_result", "sub:parent", id="tool-1", content="parent"),
        ev("grandchild", "tool_call", "sub:parent", id="grandchild-block", id_ns="block",
           name="sub_agent", input={"subagent_name": "grandchild"}),
        ev("deep-prose", "agent_message", "sub:grandchild", provider="codex", usage={},
           raw={"content": [{"type": "text", "text": "deep finding"}]}),
        ev("grand-tool", "tool_call", "sub:grandchild", id="tool-1", name="bash", input={}),
        ev("grand-result", "tool_result", "sub:grandchild", id="tool-1", content="deep"),
        ev("sibling-tool", "tool_call", "sub:sibling", id="tool-1", name="bash", input={}),
        ev("sibling-result", "tool_result", "sub:sibling", id="tool-1", content="sibling"),
        ev("grandchild-done", "tool_result", "sub:parent", id="grandchild-block",
           id_ns="block", content="grandchild done", error=False),
        ev("parent-done", "tool_result", "step:p/s", id="parent-block", id_ns="block",
           content="parent done", error=False),
        ev("sibling-done", "tool_result", "step:p/s", id="sibling-block", id_ns="block",
           content="sibling done", error=False),
    ]
    for event in events:
        b.add(event)

    parent = b._retired_descendant_builders["sub:parent"]._owner_fold
    sibling = b._retired_descendant_builders["sub:sibling"]._owner_fold
    assert parent is not None and sibling is not None
    grandchild = next(f for f in parent.tree.root.children
                      if isinstance(f, nv.SubagentFold))
    assert (
        parent.running, sibling.running, grandchild.running,
        [c.text for c in grandchild.tree.root.children if isinstance(c, nv.AgentTextNode)],
        parent.outcome, sibling.outcome, grandchild.outcome,
    ) == (False, False, False, ["deep finding"], "parent done", "sibling done", "grandchild done")


def test_current_activity_keeps_the_provider_while_a_subagent_runs() -> None:
    # The running-subagent branch builds a different Activity; the footer still
    # shows the parent step's bar, so the provider must survive that path too.
    b = nv.NarrativeBuilder()
    b.add(ev("1", "quest_created", quest_id=1, title="x"))
    b.add(ev("2", "step_started", step="s", kind="agent", phase="p",
             model="gpt-6.1-sol", provider="openai"))
    b.add(ev("t1", "tool_call", agent_id="step:p/s", id="blk-t1",
             name="sub_agent", input={"subagent_name": "coder"},
             timestamp="2026-01-01T00:00:00+00:00"))
    act = b.current_activity()
    assert act is not None
    assert act.verb == "subagent(coder)"        # sanity: the subagent branch ran
    assert (act.provider, act.model) == ("openai", "gpt-6.1-sol")


_NO_DETAIL = object()


@pytest.mark.parametrize(("etype", "key"), [
    ("warning", "message"),
    ("runner_warning", "message"),
    ("sandbox_overlay_unavailable", "reason"),
])
@pytest.mark.parametrize(("detail", "expected"), [
    ({}, ""),
    (_NO_DETAIL, ""),
    ("plain detail", "plain detail"),
    ({"stderr_tail": "a\nb"}, "a\nb"),
])
def test_warning_detail_is_text_never_dict_repr(
    etype: str, key: str, detail: object, expected: str,
) -> None:
    from imp.cli.render_text import render_tree

    payload: dict = {key: "heads up", "category": "notice"}
    if detail is not _NO_DETAIL:
        payload["detail"] = detail
    tree = nv.build_tree([
        _ev("1", "step_started", step="s", kind="agent", phase="p"),
        _ev("2", etype, agent_id="step:p/s", **payload),
    ])
    step = tree.root.children[0].children[0]
    [node] = [c for c in step.children if isinstance(c, nv.ErrorNode)]
    assert node.detail == expected

    lines = list(render_tree(tree, color=False))
    assert not any("{" in line for line in lines)
    if expected == "":
        assert lines[-1] == "warning: heads up"


def test_position_push_appends_a_header_node_only_on_change() -> None:
    b = nv.NarrativeBuilder()
    b.add(ev("1", "quest_created", quest_id=1, title="t"))
    b.add(task_lead_started("2"))
    b.add(ev("3", "step_label_updated", agent_id=TASK_LEAD_AID, label="task 1/2"))
    assert _headers(_only_step(b.tree)) == []
    b.add(ev("4", "step_label_updated", agent_id=TASK_LEAD_AID, label="task 2/2"))
    b.add(ev("5", "step_label_updated", agent_id=TASK_LEAD_AID, label="task 2/2"))
    assert _headers(_only_step(b.tree)) == [nv.StepHeaderNode(
        label="task 2/2", resumed=False, phase_label=_PHASE, model="m", route_label=None)]


def test_position_push_on_a_hidden_step_adds_no_header() -> None:
    tree = nv.build_tree([
        ev("1", "quest_created", quest_id=1, title="t"),
        task_lead_started("2", show_label=False),
        ev("3", "step_label_updated", agent_id=TASK_LEAD_AID, label="task 2/2"),
    ])
    step = _only_step(tree)
    assert _headers(step) == []
    assert step.label == "task 2/2"


def test_verb_push_adds_no_header() -> None:
    tree = nv.build_tree([
        ev("1", "quest_created", quest_id=1, title="t"),
        ev("2", "step_started", step="plan-writer", kind="agent", phase="plan",
           model="m", provider="anthropic", label="writing plan"),
        ev("3", "step_label_updated", agent_id="step:plan/plan-writer",
           label="addressing plan review feedback"),
    ])
    assert _headers(_only_step(tree)) == []


def test_position_push_lands_its_header_after_the_open_burst() -> None:
    # The push fires mid-burst (between update_step_label's call and result) and
    # must not split it: later calls still join the burst above the header.
    tree = nv.build_tree([
        ev("1", "quest_created", quest_id=1, title="t"),
        task_lead_started("2"),
        ev("3", "tool_call", agent_id=TASK_LEAD_AID, id="t1", name="update_step_label",
           input={"label": "task 2/2"}),
        ev("4", "step_label_updated", agent_id=TASK_LEAD_AID, label="task 2/2"),
        ev("5", "tool_result", agent_id=TASK_LEAD_AID, id="t1", content="ok"),
        ev("6", "tool_call", agent_id=TASK_LEAD_AID, id="t2", name="read_file",
           input={"path": "x"}),
        ev("7", "tool_result", agent_id=TASK_LEAD_AID, id="t2", content="ok"),
    ])
    step = _only_step(tree)
    assert [type(c) for c in step.children] == [nv.ToolBurstFold, nv.StepHeaderNode]
    assert [c.name for c in step.children[0].calls] == ["update_step_label", "read_file"]


def test_own_header_keeps_its_entry_label_across_on_stack_reentries() -> None:
    from imp.cli import render_text as rt
    restart = nv.build_tree([
        ev("1", "quest_created", quest_id=1, title="t"),
        task_lead_started("2"),
        agent_text("3", "before"),
        ev("4", "workflow_paused", reason="interrupt", interrupt_reason="ctrl-c"),
        task_lead_started("5", label="task 2/2", resumed=True, restarted=True),
    ])
    retry = nv.build_tree([
        ev("1", "quest_created", quest_id=1, title="t"),
        task_lead_started("2"),
        task_lead_started("3", label="task 2/2", resumed=True, restarted=True),
    ])
    for tree in (restart, retry):
        step = _only_step(tree)
        assert step.label == "task 2/2"
        assert rt.header_line(step, depth=0, color=False) == f"{_PHASE} · task 1/2 · m"


@pytest.mark.parametrize("name", INTERNAL_TOOLS)
def test_visible_calls_hides_internal_tool_recorded_calls_keeps_it(name: str) -> None:
    burst = nv.ToolBurstFold(calls=[
        nv.ToolCall(call_id="1", name="bash", input={}),
        nv.ToolCall(call_id="2", name=name, input={}),
        nv.ToolCall(call_id="3", name="load_skill", input={}),
    ])
    assert [c.name for c in nv.visible_calls(burst)] == ["bash"]
    assert [c.name for c in nv.recorded_calls(burst)] == ["bash", name]


@pytest.mark.parametrize("name", INTERNAL_TOOLS)
def test_current_activity_skips_inflight_internal_tool(name: str) -> None:
    b = nv.NarrativeBuilder()
    b.add(ev("1", "step_started", step="s", kind="agent", phase="p"))
    b.add(ev("t1", "tool_call", agent_id="a", id="c1", name="bash",
             input={"command": "ls"}))
    b.add(ev("t2", "tool_result", agent_id="a", id="c1", content="ok"))
    b.add(ev("t3", "tool_call", agent_id="a", id="c2", name=name,
             input={"status": "Success"}))
    assert b.current_activity().detail == "1 call"


@pytest.mark.parametrize("name", INTERNAL_TOOLS)
def test_current_activity_internal_only_burst_has_no_call_detail(name: str) -> None:
    b = nv.NarrativeBuilder()
    b.add(ev("1", "step_started", step="s", kind="agent", phase="p"))
    b.add(ev("t1", "tool_call", agent_id="a", id="c1", name=name,
             input={"status": "Success"}))
    act = b.current_activity()
    assert act.detail is None
    assert act.verb == "thinking"
