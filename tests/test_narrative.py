# tests/test_narrative.py
"""Tests for the narrative-tree model (src/imp/cli/narrative.py)."""
from __future__ import annotations

import json

import pytest

from imp.cli import narrative as nv
from tests._event_fixtures import ev


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


def test_tokentotals_add_usage_tolerates_missing_keys() -> None:
    tt = nv.TokenTotals()
    tt.add_usage({"input_tokens": 10, "output_tokens": 3,
                  "cache_read_input_tokens": 2, "cache_creation_input_tokens": 1})
    tt.add_usage({"input_tokens": 5})          # missing keys default to 0
    tt.add_usage(None)                          # None is a no-op
    assert (tt.input, tt.output, tt.cache_read, tt.cache_creation) == (15, 3, 2, 1)



def test_build_tree_empty_yields_root_only() -> None:
    tree = nv.build_tree([])
    assert isinstance(tree.root, nv.WorkflowNode)
    assert tree.root.children == []


def test_change_created_sets_handle_and_title() -> None:
    tree = nv.build_tree([
        {"id": "e1", "agent_id": None, "type": "change_created",
         "payload": {"handle": "add-auth-abc123", "title": "add auth"}},
    ])
    assert tree.root.handle == "add-auth-abc123"
    assert tree.root.title == "add auth"


def test_malformed_events_become_error_nodes_and_never_abort() -> None:
    tree = nv.build_tree([
        {"id": "e1", "agent_id": None, "type": "change_created",
         "payload": {"change_id": 1, "title": "ok"}},
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


def test_token_totals_accumulate_across_all_llm_responses() -> None:
    tree = nv.build_tree([
        {"id": "a", "agent_id": "step:p/s", "type": "llm_response",
         "payload": {"usage": {"input_tokens": 100, "output_tokens": 20}}},
        {"id": "b", "agent_id": "step:p/s", "type": "llm_response",
         "payload": {"usage": {"input_tokens": 50, "output_tokens": 5}}},
    ])
    assert tree.tokens.input == 150
    assert tree.tokens.output == 25


def _ev(eid, etype, agent_id=None, **payload):
    return {"id": eid, "agent_id": agent_id, "type": etype, "payload": payload}


def test_phase_derived_from_step_started_payload() -> None:
    tree = nv.build_tree([
        _ev("1", "change_created", change_id=1, title="x"),
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
        _ev("3", "step_context", agent_id="step:p/s", provider="anthropic",
            model="m", system_prompt="SYS", tools_schema=[{"name": "a"}],
            params={"max_tokens": 8192}, max_tool_calls=50),
        _ev("4", "thinking", agent_id="step:p/s", content="let me think hard"),
    ])
    step = tree.root.children[0].children[0]
    kinds = [type(c).__name__ for c in step.children]
    assert "StepContextNode" in kinds
    assert "ThinkingFold" in kinds
    ctx = next(c for c in step.children if isinstance(c, nv.StepContextNode))
    assert ctx.payload["system_prompt"] == "SYS"
    think = next(c for c in step.children if isinstance(c, nv.ThinkingFold))
    assert think.content == "let me think hard"


def test_agent_text_node_holds_payload_and_parses_lazily() -> None:
    tree = nv.build_tree([
        _ev("2", "step_started", step="s", kind="agent", phase="p",
            model="m", provider="anthropic"),
        _ev("3", "llm_response", agent_id="step:p/s", provider="anthropic",
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
        _ev("3", "llm_response", agent_id="step:p/s", provider="anthropic", model="m",
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


def test_read_task_grep_splits_into_three_nodes() -> None:
    # design §5.3 edge (a): a run [read_file, task, grep] yields
    # ToolBurstFold(read_file), SubagentFold, ToolBurstFold(grep).
    tree = nv.build_tree(_step_events(
        _ev("1", "tool_call", agent_id="step:p/s", id="b1", name="read_file",
            input={"path": "x"}),
        _ev("2", "tool_result", agent_id="step:p/s", id="b1", content="data", error=False),
        # task tool_call: EVENT id "tk1", tool_use block id "b2".
        _ev("tk1", "tool_call", agent_id="step:p/s", id="b2", name="task",
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
        _ev("tk1", "tool_call", agent_id="step:p/s", id="b1", name="task",
            input={"subagent_name": "reviewer", "task_description": "go"}),
        # subagent's own stream (agent_id = sub:tk1):
        _ev("s1", "step_context", agent_id="sub:tk1", provider="anthropic",
            model="m", system_prompt="SYS", tools_schema=[], params={}, max_tool_calls=50),
        _ev("s2", "llm_response", agent_id="sub:tk1", provider="anthropic", model="m",
            stop_reason="end_turn", usage={"input_tokens": 7, "output_tokens": 2},
            raw={"content": [{"type": "text", "text": "subagent says hi"}]}),
        # parent's task tool_result closes the fold:
        _ev("r1", "tool_result", agent_id="step:p/s", id="b1",
            content='{"status":"Success"}', error=False),
        # parent content after the subagent — must attach to the step, not the fold:
        _ev("a1", "llm_response", agent_id="step:p/s", provider="anthropic", model="m",
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
    # Subagent tokens count toward the change total (F2): 7+3 in, 2+1 out.
    assert tree.tokens.input == 10 and tree.tokens.output == 3


def test_subagent_running_until_task_result_arrives() -> None:
    tree = nv.build_tree(_step_events(
        _ev("tk1", "tool_call", agent_id="step:p/s", id="b1", name="task",
            input={"subagent_name": "reviewer", "task_description": "go"}),
        _ev("s1", "llm_response", agent_id="sub:tk1", provider="anthropic", model="m",
            stop_reason="end_turn", usage={}, raw={"content": []}),
    ))   # NB: no task tool_result yet
    step = _only_step(tree)
    sub = next(c for c in step.children if isinstance(c, nv.SubagentFold))
    assert sub.running is True
    assert sub.outcome is None


def test_error_event_becomes_error_node() -> None:
    tree = nv.build_tree([
        _ev("1", "step_started", step="s", kind="agent", phase="p"),
        _ev("2", "error", category="auth", message="Invalid API key"),
    ])
    step = tree.root.children[0].children[0]
    errs = [c for c in step.children if isinstance(c, nv.ErrorNode)]
    assert len(errs) == 1
    assert errs[0].category == "auth"
    assert errs[0].message == "Invalid API key"
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
    _ev("c", "change_created", change_id=1, title="parity"),
    _ev("s", "step_started", step="build", kind="agent", phase="impl",
        model="m", provider="anthropic"),
    _ev("b1c", "tool_call", agent_id="step:impl/build", id="b1", name="read_file",
        input={"path": "x"}),
    _ev("b1r", "tool_result", agent_id="step:impl/build", id="b1", content="data", error=False),
    _ev("tk1", "tool_call", agent_id="step:impl/build", id="b2", name="task",
        input={"subagent_name": "reviewer", "task_description": "review"}),
    _ev("su1", "step_context", agent_id="sub:tk1", provider="anthropic", model="m",
        system_prompt="S", tools_schema=[], params={}, max_tool_calls=50),
    _ev("su2", "llm_response", agent_id="sub:tk1", provider="anthropic", model="m",
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
    assert (batch.tokens.input, batch.tokens.output) == \
           (incremental.tokens.input, incremental.tokens.output)


def test_parity_holds_with_open_step_when_paused() -> None:
    # A live-then-paused log has an open StepNode; a batch replay of the same
    # log shows it open too. Both are correct (design §5.1 / §11).
    log = [*INTERLEAVED_LOG[:-1], _ev("wp", "workflow_paused", reason="ctrl-c")]
    batch = nv.build_tree(log)
    b = nv.NarrativeBuilder()
    for e in log:
        b.add(e)
    assert nv.structural_repr(batch.root) == nv.structural_repr(b.tree.root)


def test_resume_reentry_reuses_phase_and_resets_step() -> None:
    seed = [
        _ev("c", "change_created", change_id=1, title="t"),
        _ev("s1", "step_started", step="spec", kind="agent", phase="design",
            model="m", provider="anthropic"),
        _ev("s1c", "step_completed", step="spec", kind="agent",
            action={"verb": "continue"}, var_mutations={}),
        _ev("s2", "step_started", step="build", kind="agent", phase="design",
            model="m", provider="anthropic"),
        _ev("ll1", "llm_response", agent_id="step:design/build", provider="anthropic",
            model="m", stop_reason="end_turn", usage={},
            raw={"content": [{"type": "text", "text": "partial attempt"}]}),
        _ev("wp", "workflow_paused", reason="ctrl-c"),
        _ev("wr", "workflow_resumed"),
    ]
    live = [
        # re-emitted on resume — same phase + step → re-enters, does not duplicate
        _ev("s2b", "step_started", step="build", kind="agent", phase="design",
            model="m", provider="anthropic"),
        _ev("ll2", "llm_response", agent_id="step:design/build", provider="anthropic",
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
        ev("1", "change_created", change_id=1, title="x"),
        ev("3", "step_started", step="spec", kind="agent", phase="design",
           model="m", provider="anthropic"),
        ev("4", "tool_call", agent_id="step:design/spec", id="b1",
           name="read_file", input={"path": "config.py"}),
    ]:
        b.add(e)
    assert b.current_burst is not None
    act = b.current_activity()
    assert act.count == "1 call"
    assert act.detail == "read_file config.py"
    assert act.phase == "design"
    assert act.step_label is None
    assert act.call_elapsed is not None


def test_current_activity_idle_gap_between_nodes() -> None:
    b = nv.NarrativeBuilder()
    for e in [
        ev("1", "change_created", change_id=1, title="x"),
        ev("3", "step_started", step="spec", kind="agent", phase="design",
           model="m", provider="anthropic"),
    ]:
        b.add(e)
    assert b.current_burst is None
    assert b.current_activity() == nv.Activity(phase="design", label="thinking")


def test_current_activity_none_when_no_step() -> None:
    b = nv.NarrativeBuilder()
    b.add(ev("1", "change_created", change_id=1, title="x"))
    assert b.current_activity() is None


def test_current_activity_agent_step_with_thinking_child_is_thinking() -> None:
    b = nv.NarrativeBuilder()
    for e in [
        ev("1", "change_created", change_id=1, title="x"),
        ev("3", "step_started", step="spec", kind="agent", phase="design",
           model="m", provider="anthropic"),
        ev("4", "thinking", agent_id="step:design/spec", content="reasoning..."),
    ]:
        b.add(e)
    assert b.current_activity() == nv.Activity(phase="design", label="thinking")



def test_subagent_current_activity_includes_nested_detail() -> None:
    b = nv.NarrativeBuilder()
    for e in [
        ev("1", "change_created", change_id=1, title="x"),
        ev("3", "step_started", step="spec", kind="agent", phase="design",
           model="m", provider="anthropic"),
        ev("4", "tool_call", agent_id="step:design/spec", id="t1",
           name="task", input={"subagent_name": "coder"}),
        ev("5", "tool_call", agent_id="sub:4", id="n1",
           name="edit_file", input={"path": "executor.py"}),
    ]:
        b.add(e)
    act = b.current_activity()
    assert act is not None and act.label is not None
    assert "task → coder" in act.label and "executor.py" in act.label


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
        _ev("tk1", "tool_call", agent_id="step:p/s", id="b1", name="task",
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
    # claude_code): a `task` tool_call + tool_result sharing the same non-empty
    # block id, with parent prose AFTER the result. The fold must close via the
    # canonical _h_tool_result path carrying the outcome (is_error/glyph), NOT via
    # resume-close, and there must be NO orphan "tool" burst line.
    tree = nv.build_tree(_step_events(
        _ev("tk1", "tool_call", agent_id="step:p/s", id="b1", name="task",
            input={"subagent_name": "reviewer", "task_description": "go"}),
        _ev("s1", "step_context", agent_id="sub:tk1", provider="anthropic",
            model="m", system_prompt="SYS", tools_schema=[], params={},
            max_tool_calls=50),
        _ev("r1", "tool_result", agent_id="step:p/s", id="b1",
            content="RuntimeError: boom", error=True),
        _ev("a1", "llm_response", agent_id="step:p/s", provider="anthropic", model="m",
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
    b.add(_ev("tk1", "tool_call", agent_id="step:p/s", id=block_id, name="task",
              input={"subagent_name": "reviewer", "task_description": "go"}))
    b.add(_ev("s1", "step_context", agent_id="sub:tk1", provider="anthropic",
              model="m", system_prompt="SYS", tools_schema=[], params={},
              max_tool_calls=50))
    step = b._step_stack[-1]
    sub = next(c for c in step.children if isinstance(c, nv.SubagentFold))
    assert sub.running is True
    # current_activity sits on the running subagent before the parent resumes.
    assert "task → reviewer" in nv.activity_to_text(b.current_activity())
    # Parent resumes (same agent_id) with NO task tool_result — resume-close fires.
    b.add(_ev("a1", "llm_response", agent_id="step:p/s", provider="anthropic",
              model="m", stop_reason="end_turn", usage={}, raw={"content": [
                  {"type": "text", "text": "parent resumes"}]}))
    assert sub.running is False
    # Status line advanced off the subagent (no running fold left).
    act = nv.activity_to_text(b.current_activity())
    assert "task → reviewer" not in act


def test_step_started_stashes_start_ts_normal_and_resume() -> None:
    # Normal path: start_ts captured from the event's top-level timestamp.
    e1 = _ev("1", "step_started", step="spec", kind="agent", phase="design")
    e1["timestamp"] = "2026-05-30T10:00:00+00:00"
    b = nv.NarrativeBuilder()
    b.add(_ev("0", "change_created", change_id=1, title="x"))
    b.add(e1)
    step = b._step_stack[-1]
    assert step.start_ts == "2026-05-30T10:00:00+00:00"
    assert step.agent_output is None

    # Resume re-entry path reuses the open StepNode and must restamp start_ts.
    b.add(_ev("2", "workflow_resumed"))
    e3 = _ev("3", "step_started", step="spec", kind="agent", phase="design")
    e3["timestamp"] = "2026-05-30T10:05:00+00:00"
    b.add(e3)
    reused = b._step_stack[-1]
    assert reused is step                      # same node reused
    assert reused.start_ts == "2026-05-30T10:05:00+00:00"


def test_agent_output_stashes_args_without_emitting_prose_node() -> None:
    b = nv.NarrativeBuilder()
    b.add(_ev("0", "change_created", change_id=1, title="x"))
    b.add(_ev("1", "step_started", step="coder", kind="agent", phase="build"))
    # agent_output fires before step_completed; its long free-text field is
    # stashed for the breadcrumb/--full view, NEVER promoted to a prose block.
    b.add(_ev("2", "agent_output", agent_id="step:build/coder",
              summary="A long summary. " * 10, status="Success",
              plan_path="docs/plan.md"))
    step = b._step_stack[-1]
    assert step.agent_output["status"] == "Success"
    assert step.agent_output["summary"].startswith("A long summary.")
    lits = [c for c in step.children if isinstance(c, nv.LiteralTextNode)]
    assert lits == []          # output fields are not prose blocks (E1)


def test_agent_output_skips_whitespace_only_prose_field() -> None:
    # A whitespace-only multi-line field passes _is_prose_field (it contains a
    # newline) but must NOT promote to _render_output_args output — the v.strip()
    # guard in _render_output_args skips empty/whitespace values.
    b = nv.NarrativeBuilder()
    b.add(_ev("0", "change_created", change_id=1, title="x"))
    b.add(_ev("1", "step_started", step="coder", kind="agent", phase="build"))
    b.add(_ev("2", "agent_output", agent_id="step:build/coder",
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


def test_current_activity_collapses_multiline_bash_command() -> None:
    b = nv.NarrativeBuilder()
    b.add(_ev("0", "change_created", change_id=1, title="x"))
    b.add(_ev("1", "step_started", step="coder", kind="agent", phase="build"))
    cmd = "python3 -c 'import sys\nprint(sys.version)\n" + "x" * 80 + "'"
    b.add(_ev("2", "tool_call", agent_id="step:build/coder", id="t1",
              name="bash", input={"command": cmd}))
    act = b.current_activity()
    assert act.detail is not None
    assert "\n" not in act.detail                    # single line only
    assert "…" in act.detail                         # truncated
    assert len(act.detail) < 120                      # not the whole script
    assert act.detail.startswith("bash ")             # name + sanitized arg, no step prefix


def test_step_completed_emits_merged_breadcrumb_and_preserves_outcome() -> None:
    b = nv.NarrativeBuilder()
    b.add(_ev("0", "change_created", change_id=1, title="x"))
    s = _ev("1", "step_started", step="coder", kind="agent", phase="build")
    s["timestamp"] = "2026-05-30T10:00:00+00:00"
    b.add(s)
    b.add(_ev("2", "agent_output", agent_id="step:build/coder",
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


def test_empty_prose_llm_response_yields_no_agent_text_node() -> None:
    # A thinking-only turn (no text block) renders to empty prose. It must NOT
    # produce an AgentTextNode — that would render as an indent-only blank line.
    tree = nv.build_tree([
        _ev("1", "step_started", step="spec", kind="agent", phase="design"),
        _ev("2", "thinking", agent_id="a", content="hmm"),
        _ev("3", "llm_response", agent_id="a",
            raw={"content": [{"type": "thinking", "thinking": "hmm"}]}, usage={}),
        _ev("4", "llm_response", agent_id="a",
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


def test_empty_llm_response_between_tools_keeps_burst_open() -> None:
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
    b.add({"type": "llm_response", "agent_id": None,
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
    b.add({"type": "llm_response", "agent_id": None,
           "payload": {"raw": {"content": [{"type": "text", "text": "done."}]}}, "id": "e3"})
    assert b.current_burst is None


def test_task_result_with_matching_id_closes_fold_carrying_outcome() -> None:
    # New contract (replaces the old §11 mismatched-id heuristic, removed when
    # Part 1 made claude_code mint a real block id matching the call+result).
    # The task tool_call and its tool_result share the SAME non-empty block id;
    # the fold closes via _h_tool_result's block-id match, carrying the outcome.
    b = nv.NarrativeBuilder()
    b.add({"type": "step_started", "agent_id": None,
           "payload": {"step": "task-lead", "kind": "agent", "phase": "implement"},
           "id": "e1", "timestamp": "2026-06-03T00:00:00+00:00"})
    b.add({"type": "tool_call", "agent_id": "step:implement/task-lead",
           "payload": {"name": "task", "id": "toolu_minted",
                       "input": {"subagent_name": "implementer", "task_description": "go"}},
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


def test_change_renamed_updates_root_title_and_handle() -> None:
    # The narrative builder must reflect the settled name after change_renamed.
    b = nv.NarrativeBuilder()
    b.add(ev("1", "change_created", handle="", title=""))
    b.add(ev("2", "change_renamed", id="aaa111",
             old_title="", new_title="Add OAuth",
             handle="add-oauth-aaa111"))
    assert b.tree.root.title == "Add OAuth"
    assert b.tree.root.handle == "add-oauth-aaa111"


def test_change_renamed_ignores_legacy_partial_event() -> None:
    # The handler applies new_title + handle atomically. A legacy/partial event
    # missing new_title must be IGNORED, not half-applied — otherwise the header
    # would show a new handle beside a stale title.
    b = nv.NarrativeBuilder()
    b.add(ev("1", "change_created", handle="orig-xyz", title="orig"))
    b.add(ev("2", "change_renamed",
             old_title="orig",
             title="renamed"))   # NO new_title and NO handle — legacy/partial schema
    assert b.tree.root.title == "orig"          # unchanged (event ignored)
    assert b.tree.root.handle == "orig-xyz"  # unchanged (no half-update)


def test_toolcall_call_ts_stamped_by_all_three_constructors() -> None:
    b = nv.NarrativeBuilder()
    b.add({"id": "1", "type": "step_started", "agent_id": None,
           "payload": {"step": "s", "kind": "agent", "phase": "p"}})
    # normal tool_call constructor
    b.add({"id": "e1", "type": "tool_call", "agent_id": "a",
           "timestamp": "2026-06-10T00:00:01",
           "payload": {"id": "c1", "name": "bash", "input": {"command": "ls"}}})
    # tool_killed standalone-marker constructor (no matching open call)
    b.add({"id": "e2", "type": "tool_killed", "agent_id": "a",
           "timestamp": "2026-06-10T00:00:02",
           "payload": {"id": "zz", "name": "sleep"}})
    # tool_result orphan constructor (no matching open call; "c1" is open but id
    # "orphan" doesn't match it, so a new orphan ToolCall is appended)
    b.add({"id": "e3", "type": "tool_result", "agent_id": "a",
           "timestamp": "2026-06-10T00:00:03",
           "payload": {"id": "orphan", "content": "x"}})
    burst = b.current_burst
    assert burst is not None
    by_id = {c.call_id: c for c in burst.calls}
    assert by_id["c1"].call_ts == "2026-06-10T00:00:01"
    assert by_id["zz"].call_ts == "2026-06-10T00:00:02"
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


def test_visible_calls_omits_load_skill_tool_and_wait_for_user() -> None:
    burst = nv.ToolBurstFold(calls=[
        nv.ToolCall(call_id="1", name="bash", input={}),
        nv.ToolCall(call_id="2", name="load_skill", input={}),
        nv.ToolCall(call_id="3", name="tool", input={}),
        nv.ToolCall(call_id="4", name="wait_for_user", input={}),
        nv.ToolCall(call_id="5", name="read_file", input={}),
    ])
    assert [c.name for c in nv.visible_calls(burst)] == ["bash", "read_file"]
    assert frozenset({"load_skill", "tool", "wait_for_user"}) == nv._OMIT
    assert frozenset({"script", "branch", "merge"}) == nv.CHROME_KINDS


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
    assert act.count == "1 call"
    assert act.detail == "bash make test"
    assert act.label is None


def test_current_activity_all_killed_basis_is_bare_count() -> None:
    b = nv.NarrativeBuilder()
    b.add(ev("1", "step_started", step="s", kind="agent", phase="p"))
    b.add(ev("t1", "tool_call", agent_id="a", id="c1", name="bash", input={}))
    b.add(ev("k1", "tool_killed", agent_id="a", id="c1", name="bash"))
    act = b.current_activity()
    assert act.count == "1 call"          # count keeps killed
    assert act.detail is None             # named call excludes killed → bare count


def test_current_activity_agent_step_no_burst_is_thinking() -> None:
    b = nv.NarrativeBuilder()
    b.add(ev("1", "step_started", step="s", kind="agent", phase="p"))
    assert b.current_activity() == nv.Activity(phase="p", label="thinking")


def test_current_activity_chrome_step_is_running() -> None:
    b = nv.NarrativeBuilder()
    b.add(ev("1", "step_started", step="g", kind="merge", phase="p"))
    assert b.current_activity() == nv.Activity(phase="p", label="running")


def test_activity_to_text_flattens_segments() -> None:
    assert nv.activity_to_text(nv.Activity(count="3 calls", detail="bash x")) == \
        "3 calls · bash x"
    assert nv.activity_to_text(nv.Activity(label="thinking")) == "thinking"
    assert nv.activity_to_text(None) == ""


def _wfu_step(b):
    b.add(ev("1", "step_started", step="s", kind="agent", phase="p"))


def _step_node(b):
    # A step_started carrying a `phase` wraps the StepNode inside a PhaseNode
    # (_h_step_started → _ensure_phase). So root.children[-1] is the PhaseNode and
    # the StepNode — which owns the synthesized UserMessageNode / ToolBurstFold
    # children — is its sole child. This mirrors the file's existing convention
    # (tests/test_narrative.py uses tree.root.children[0].children[0]).
    return b.tree.root.children[-1].children[0]


def test_wfu_pairing_synthesizes_user_turn_and_splits_burst():       # [I1][I2][I3]
    b = nv.NarrativeBuilder()
    _wfu_step(b)
    b.add(ev("t1", "tool_call", agent_id="a", id="w1", name="wait_for_user",
             input={"prompt": "continue?"}, timestamp="2026-06-10T00:00:01"))
    b.add(ev("t2", "tool_result", agent_id="a", id="w1", content="yes please"))
    step = _step_node(b)
    kids = step.children
    # child order: closed burst (now empty → still present), user turn, no fresh burst
    user = next(k for k in kids if isinstance(k, nv.UserMessageNode))
    assert user.interactive is True
    assert user.text == "yes please"
    assert b.current_burst is None        # no siblings → no fresh burst, cursor cleared


def test_wfu_pairing_carries_unresulted_sibling_into_fresh_burst():  # [I2][I3]
    b = nv.NarrativeBuilder()
    _wfu_step(b)
    b.add(ev("t0", "tool_call", agent_id="a", id="b1", name="bash",
             input={"command": "sleep 40"}, timestamp="2026-06-10T00:00:01"))
    b.add(ev("t1", "tool_call", agent_id="a", id="w1", name="wait_for_user",
             input={"prompt": "ok?"}, timestamp="2026-06-10T00:00:02"))
    b.add(ev("t2", "tool_result", agent_id="a", id="w1", content="ok"))
    fresh = b.current_burst
    assert fresh is not None
    assert [c.call_id for c in fresh.calls] == ["b1"]      # sibling carried
    assert fresh.start_ts == "2026-06-10T00:00:01"          # earliest carried call_ts


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


def test_wfu_end_ts_advances_only_on_non_wfu_pairing():             # [I2]
    b = nv.NarrativeBuilder()
    _wfu_step(b)
    b.add(ev("t0", "tool_call", agent_id="a", id="b1", name="bash", input={},
             timestamp="2026-06-10T00:00:01"))
    b.add(ev("r0", "tool_result", agent_id="a", id="b1", content="ok",
             timestamp="2026-06-10T00:00:05"))
    b.add(ev("t1", "tool_call", agent_id="a", id="w1", name="wait_for_user",
             input={"prompt": "ok?"}, timestamp="2026-06-10T00:00:06"))
    b.add(ev("r1", "tool_result", agent_id="a", id="w1", content="yes",
             timestamp="2026-06-10T00:45:00"))
    closed = next(k for k in _step_node(b).children
                  if isinstance(k, nv.ToolBurstFold))
    assert closed.end_ts == "2026-06-10T00:00:05"   # the human's wait is NOT billed


def test_wfu_orphan_net_synthesizes_when_pairing_missed():          # [I1][I3]
    b = nv.NarrativeBuilder()
    _wfu_step(b)
    b.add(ev("t1", "tool_call", agent_id="a", id="w1", name="wait_for_user",
             input={"prompt": "ok?"}, timestamp="2026-06-10T00:00:01"))
    # a prose llm_response flushes the burst, so the wfu call's result misses pairing
    b.add(ev("p1", "llm_response", agent_id="a",
             raw={"content": [{"type": "text", "text": "thinking"}]}, usage={}))
    b.add(ev("r1", "tool_result", agent_id="a", id="w1", content="yes"))
    step = _step_node(b)
    user = next(k for k in step.children if isinstance(k, nv.UserMessageNode))
    assert user.text == "yes" and user.interactive is True
    assert "w1" not in b._pending_wfu        # id discarded after net synthesis


def test_double_wfu_each_synthesis_gets_own_reply():                # [I1] double-wfu
    """Two sequential wait_for_user calls in the same burst; each reply captured
    separately. I1: no reply is lost."""
    b = nv.NarrativeBuilder()
    _wfu_step(b)
    b.add(ev("t0", "tool_call", agent_id="a", id="b1", name="bash",
             input={"command": "ls"}, timestamp="2026-06-10T00:00:01"))
    b.add(ev("t1", "tool_call", agent_id="a", id="w1", name="wait_for_user",
             input={"prompt": "Q1?"}, timestamp="2026-06-10T00:00:02"))
    b.add(ev("r1", "tool_result", agent_id="a", id="w1", content="A1"))
    # After first synthesis: fresh burst carries bash(b1); w2 is then added to it
    b.add(ev("t2", "tool_call", agent_id="a", id="w2", name="wait_for_user",
             input={"prompt": "Q2?"}, timestamp="2026-06-10T00:00:03"))
    b.add(ev("r2", "tool_result", agent_id="a", id="w2", content="A2"))
    # Two UserMessageNodes must exist
    step = _step_node(b)
    users = [k for k in step.children if isinstance(k, nv.UserMessageNode)]
    assert len(users) == 2                          # [I1] neither reply lost
    assert users[0].text == "A1"
    assert users[1].text == "A2"
    # bash is still unresulted; it should be in the final fresh burst
    assert b.current_burst is not None
    assert b.current_burst.calls[0].call_id == "b1"


def test_resume_tool_result_shaped_user_message_renders_reply():    # [I1]
    b = nv.NarrativeBuilder()
    b.add(ev("1", "step_started", step="s", kind="agent", phase="p"))
    b.add(ev("aw", "awaiting_user_input", agent_id="a", prompt="continue?"))
    b.add(ev("um", "user_message", agent_id="a", content=[
        {"type": "tool_result", "tool_use_id": "w1", "is_error": False,
         "content": "the human reply"}]))
    step = _step_node(b)            # PhaseNode→StepNode helper from Task 8's block
    user = next(k for k in step.children if isinstance(k, nv.UserMessageNode))
    assert user.interactive is True
    assert user.text == "the human reply"


def test_resume_unarmed_balance_injection_renders_nothing():         # [I1] negative
    b = nv.NarrativeBuilder()
    b.add(ev("1", "step_started", step="s", kind="agent", phase="p"))
    # no awaiting_user_input → flag unarmed; balance-shaped sole tool_result block
    b.add(ev("um", "user_message", agent_id="a", content=[
        {"type": "tool_result", "tool_use_id": "x", "is_error": False,
         "content": "previous step output recorded; resuming"}]))
    step = _step_node(b)            # PhaseNode→StepNode helper from Task 8's block
    users = [k for k in step.children if isinstance(k, nv.UserMessageNode)]
    # interactive=False (unarmed) → synthetic, and text stays empty (no text block)
    assert all(not u.interactive for u in users)
    assert all(u.text == "" for u in users)


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
    b.add(_ev("tk1", "tool_call", agent_id="step:p/s", id="b1", name="task",
              input={"subagent_name": "reviewer", "task_description": "go"}))
    b.add(_ev("tk2", "tool_call", agent_id="step:p/s", id="b2", name="task",
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


def test_concurrent_codex_folds_close_by_event_ns_no_empty_block_collision() -> None:
    # AC36: codex mints task tool_calls with an EMPTY block id and keys the
    # tool_result by the minted EVENT id, stamped id_ns="event". Two such folds
    # must not collide on "" and must each close by event id.
    b = nv.NarrativeBuilder()
    b.add(_ev("st", "step_started", step="s", kind="agent", phase="p",
              model="m", provider="anthropic"))
    b.add(_ev("tk1", "tool_call", agent_id="step:p/s", id="", name="task",
              input={"subagent_name": "reviewer", "task_description": "go"}))
    b.add(_ev("tk2", "tool_call", agent_id="step:p/s", id="", name="task",
              input={"subagent_name": "tester", "task_description": "go"}))
    step = b._step_stack[-1]
    folds = {f.subagent_name: f for f in step.children
             if isinstance(f, nv.SubagentFold)}
    assert folds["reviewer"].running is True and folds["tester"].running is True
    # Close task1 by its EVENT id (codex result payload id == minted event id).
    b.add(_ev("r1", "tool_result", agent_id="step:p/s", id="tk1", id_ns="event",
              content='{"status":"Success"}', error=False))
    assert folds["reviewer"].running is False
    assert folds["tester"].running is True


def test_stamped_result_missing_its_namespace_renders_warning_not_close() -> None:
    # AC36/AC38: an id_ns-stamped result whose id matches no open fold in its
    # named namespace renders a malformed-log WARNING and closes nothing — even
    # with exactly one fold open.
    b = nv.NarrativeBuilder()
    b.add(_ev("st", "step_started", step="s", kind="agent", phase="p",
              model="m", provider="anthropic"))
    b.add(_ev("tk1", "tool_call", agent_id="step:p/s", id="b1", name="task",
              input={"subagent_name": "reviewer", "task_description": "go"}))
    step = b._step_stack[-1]
    fold = next(c for c in step.children if isinstance(c, nv.SubagentFold))
    # id_ns="event" but id "b1" only lives in the block-id dict → namespace miss.
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
    b.add(_ev("tk1", "tool_call", agent_id="step:p/s", id="m1", name="task",
              input={"subagent_name": "reviewer", "task_description": "go"}))
    step = b._step_stack[-1]
    fold = next(c for c in step.children if isinstance(c, nv.SubagentFold))
    # A non-matching parent event triggers the resume-close fallback (pops m1).
    b.add(_ev("a1", "llm_response", agent_id="step:p/s", provider="anthropic",
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
    b.add(_ev("tk1", "tool_call", agent_id="step:p/s", id="m1", name="task",
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
        _ev("tk1", "tool_call", agent_id="step:p/s", id="b1", name="task",
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
    b.add(_ev("tk1", "tool_call", agent_id="step:p/s", id="b1", name="task",
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
    assert "task → reviewer" not in nv.activity_to_text(b.current_activity())

    # Resumed turn re-enters the step and opens a fresh running fold.
    b.add(_ev("st2", "step_started", step="s", kind="agent", phase="p",
              model="m", provider="anthropic"))
    b.add(_ev("tk2", "tool_call", agent_id="step:p/s", id="b9", name="task",
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
    b.add(_ev("tk1", "tool_call", agent_id="step:p/s", id="b1", name="task",
              input={"subagent_name": "reviewer", "task_description": "go"}))
    step = b._step_stack[-1]
    dangling = next(c for c in step.children if isinstance(c, nv.SubagentFold))
    assert "sub:tk1" in b._sub_by_agent_id

    b.add(_ev("p1", "workflow_paused", interrupt_reason="interrupt"))
    b.add(_ev("r1", "workflow_resumed"))
    assert "sub:tk1" not in b._sub_by_agent_id          # membership dropped
    count_before = dangling.event_count

    # A late event carrying the interrupted fold's sub: id must NOT reattach.
    b.add(_ev("late", "llm_response", agent_id="sub:tk1", usage={}))
    assert dangling.event_count == count_before


def test_current_activity_suffix_counts_extra_running_subagents() -> None:
    # AC40: one running fold → no suffix; two → "(+1 more subagent)"; three →
    # "(+2 more subagents)" (singular/plural). The named detail is the FIRST
    # running fold's; the suffix counts the rest.
    def _task(eid: str, bid: str, name: str) -> dict:
        return _ev(eid, "tool_call", agent_id="step:p/s", id=bid, name="task",
                   input={"subagent_name": name, "task_description": "go"})

    b = nv.NarrativeBuilder()
    b.add(_ev("st", "step_started", step="s", kind="agent", phase="p",
              model="m", provider="anthropic"))
    b.add(_task("tk1", "b1", "reviewer"))
    one = nv.activity_to_text(b.current_activity())
    assert "task → reviewer" in one
    assert "more subagent" not in one                    # single fold: no suffix

    b.add(_task("tk2", "b2", "tester"))
    two = nv.activity_to_text(b.current_activity())
    assert "task → reviewer" in two                      # first running fold's detail
    assert "(+1 more subagent)" in two                   # singular
    assert "(+1 more subagents)" not in two

    b.add(_task("tk3", "b3", "researcher"))
    three = nv.activity_to_text(b.current_activity())
    assert "(+2 more subagents)" in three                # plural


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


def test_change_merged_becomes_merged_node_not_fallback() -> None:
    tree = nv.build_tree([
        _ev("0", "change_created", id="runx01", handle="run-x-runx01",
            title="Run X"),
        _ev("1", "step_started", step="merge-to-base", kind="merge", phase="review"),
        _ev("2", "step_completed", step="merge-to-base", kind="merge",
            action={"verb": "continue"}, var_mutations={}),
        _ev("3", "change_merged", base="master", commit="37dde900abc"),
    ])
    merged = [c for c in tree.root.children if isinstance(c, nv.MergedNode)]
    assert len(merged) == 1
    assert merged[0].change_id == "run-x-runx01"
    assert merged[0].base == "master"
    # the raw event no longer leaks as a fallback line anywhere in the tree
    fbs: list[nv.FallbackLine] = []

    def walk(nodes: list) -> None:
        for n in nodes:
            if isinstance(n, nv.FallbackLine):
                fbs.append(n)
            walk(getattr(n, "children", []))
    walk(tree.root.children)
    assert all(f.event_type != "change_merged" for f in fbs)


def test_merge_blocked_event_is_suppressed() -> None:
    tree = nv.build_tree([
        _ev("1", "step_started", step="merge-to-base", kind="merge", phase="review"),
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
        _ev("2", "llm_response", agent_id="step:spec/spec-reviewer",
            provider="anthropic", model="m", usage={},
            raw={"content": [{"type": "text", "text": narration}]}),
        _ev("3", "tool_call", agent_id="step:spec/spec-reviewer", id="cs1",
            name="complete_step",
            input={"status": "Success", "verdict": "approve", "findings": findings}),
        _ev("4", "agent_output", agent_id="step:spec/spec-reviewer",
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
            name="task", input={"subagent_name": f"member-{eid}",
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
        _ev("ao", "agent_output", agent_id="step:review/review",
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
        _ev("ao", "agent_output", agent_id="step:review/review",
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
        ev("1", "change_created", change_id=1, title="x"),
        ev("2", "step_started", step="plan-review", kind="agent", phase="review",
           model="m", provider="anthropic"),
        ev("3", "thinking", agent_id="step:review/plan-review", content="hmm"),
    ]:
        b.add(e)
    assert b.current_activity() == nv.Activity(phase="review", label="thinking")


def test_current_activity_running_label_for_chrome_step() -> None:
    b = nv.NarrativeBuilder()
    for e in [
        ev("1", "change_created", change_id=1, title="x"),
        ev("2", "step_started", step="merge-it", kind="merge", phase="integrate"),
    ]:
        b.add(e)
    assert b.current_activity() == nv.Activity(phase="integrate", label="running")


def test_subagent_activity_has_no_step_prefix() -> None:
    b = nv.NarrativeBuilder()
    for e in [
        ev("1", "change_created", change_id=1, title="x"),
        ev("2", "step_started", step="plan-review", kind="agent", phase="review",
           model="m", provider="anthropic"),
        ev("tk1", "tool_call", agent_id="step:review/plan-review", id="b2", name="task",
           input={"subagent_name": "plan-reviewer", "task_description": "go"}),
        ev("s1", "thinking", agent_id="sub:tk1", content="reviewing"),
    ]:
        b.add(e)
    assert b.current_activity().label == "task → plan-reviewer · 1 events"


def test_step_label_and_resumed_land_on_step_and_breadcrumb() -> None:
    tree = nv.build_tree([
        ev("1", "change_created", change_id=1, title="x"),
        ev("2", "step_started", step="code-reviewer", kind="agent",
           phase="implement", model="m", provider="anthropic",
           label="reviewing code", resumed=True),
        ev("3", "step_completed", step="code-reviewer", kind="agent",
           action={"verb": "continue"}, var_mutations={}),
    ])
    step = next(c for p in tree.root.children if isinstance(p, nv.PhaseNode)
                for c in p.children if isinstance(c, nv.StepNode))
    assert step.label == "reviewing code"
    assert step.resumed is True
    bc = next(c for c in step.children if isinstance(c, nv.BreadcrumbLine))
    assert bc.step_label == "reviewing code"


def test_resume_reentry_updates_label_and_resumed_on_step_node() -> None:
    tree = nv.build_tree([
        ev("1", "change_created", change_id=1, title="x"),
        ev("2", "step_started", step="code-reviewer", kind="agent",
           phase="implement", model="m", provider="anthropic",
           label="reviewing code", resumed=False),
        ev("3", "workflow_paused", reason="ctrl-c"),
        ev("4", "workflow_resumed"),
        ev("5", "step_started", step="code-reviewer", kind="agent",
           phase="implement", model="m", provider="anthropic",
           label="reviewing code", resumed=True),
    ])
    step = next(c for p in tree.root.children if isinstance(p, nv.PhaseNode)
                for c in p.children if isinstance(c, nv.StepNode))
    assert step.label == "reviewing code"
    assert step.resumed is True


def test_parallel_batch_folds_ignore_interleaved_parent_chunks() -> None:
    # Regression: a parallel `task` batch dispatches two subagents BEFORE any
    # result. Between the dispatch and the results the parent streams its own
    # mid-turn chunks (a stop_reason=None llm_response and a thinking event).
    # Those chunks must NOT resume-close either still-open sibling fold; each fold
    # closes exactly once, on its OWN task result, stamping its own end_ts.
    b = nv.NarrativeBuilder()
    b.add(ev("st", "step_started", step="s", kind="agent", phase="p",
             model="m", provider="anthropic"))
    b.add(ev("tk1", "tool_call", agent_id="step:p/s", id="b1", name="task",
             input={"subagent_name": "reviewer", "task_description": "go"},
             timestamp="2026-06-19T10:00:00+00:00"))
    b.add(ev("tk2", "tool_call", agent_id="step:p/s", id="b2", name="task",
             input={"subagent_name": "tester", "task_description": "go"},
             timestamp="2026-06-19T10:00:01+00:00"))
    step = b._step_stack[-1]
    folds = {f.subagent_name: f for f in step.children
             if isinstance(f, nv.SubagentFold)}
    assert folds["reviewer"].running is True and folds["tester"].running is True
    # Parent mid-turn streaming chunks -- must NOT close either sibling.
    b.add(ev("a1", "llm_response", agent_id="step:p/s", provider="anthropic",
             model="m", stop_reason=None, usage={},
             raw={"content": [{"type": "text", "text": "still working"}]},
             timestamp="2026-06-19T10:00:02+00:00"))
    b.add(ev("a2", "llm_response", agent_id="step:p/s", provider="anthropic",
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
