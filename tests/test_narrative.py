# tests/test_narrative.py
"""Tests for the narrative-tree model (src/imp/cli/narrative.py)."""
from __future__ import annotations

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
    assert nv.GateNode(kind="human").collapsible is False
    assert nv.ErrorNode(message="boom").collapsible is False
    assert nv.AskUserNode(question="q?").collapsible is False
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


def test_change_created_sets_id_and_title() -> None:
    tree = nv.build_tree([
        {"id": "e1", "agent_id": None, "type": "change_created",
         "payload": {"change_id": 7, "title": "add auth"}},
    ])
    assert tree.root.change_id == 7
    assert tree.root.title == "add auth"


def test_malformed_events_become_error_nodes_and_never_abort() -> None:
    tree = nv.build_tree([
        {"id": "e1", "agent_id": None, "type": "change_created",
         "payload": {"change_id": 1, "title": "ok"}},
        None,                                   # not a dict
        {"agent_id": None, "payload": {}},      # missing "type"
        {"id": "e4", "type": "phase_started", "payload": {"phase": "design"}},
    ])
    # The two malformed items become ErrorNode children at root scope; the
    # build continues and the phase still opens.
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


def test_phase_and_step_nest_and_close() -> None:
    tree = nv.build_tree([
        _ev("1", "change_created", change_id=1, title="x"),
        _ev("2", "phase_started", phase="design"),
        _ev("3", "step_started", step="spec", model="m", provider="anthropic",
            interactive=False),
        _ev("4", "step_completed", step="spec", action="continue"),
        _ev("5", "phase_completed", phase="design"),
    ])
    phases = [c for c in tree.root.children if isinstance(c, nv.PhaseNode)]
    assert len(phases) == 1
    phase = phases[0]
    assert phase.name == "design"
    assert phase.closed is True
    steps = [c for c in phase.children if isinstance(c, nv.StepNode)]
    assert len(steps) == 1
    step = steps[0]
    assert step.name == "spec"
    assert step.kind is None
    assert step.closed is True
    assert isinstance(step.outcome, nv.OutcomeLine)
    assert step.outcome.action == "continue"


def test_open_step_is_a_valid_terminal_state() -> None:
    # User-pause path emits no step_completed (design §5.1). The step stays open.
    tree = nv.build_tree([
        _ev("1", "phase_started", phase="design"),
        _ev("2", "step_started", step="spec", model="m", provider="anthropic",
            interactive=False),
        _ev("3", "workflow_paused", reason="ctrl-c"),
    ])
    phase = tree.root.children[0]
    step = phase.children[0]
    assert step.closed is False
    assert step.outcome is None
    assert tree.root.status == "paused"


def test_agent_gate_inside_open_step_pushes_and_pops_container() -> None:
    # Order from executor: step opens, runs; then a step-level AGENT gate emits
    # its own step_started(kind=agent_gate) while the step is still open; the
    # gate closes via gate_evaluated(kind=agent); then the step completes.
    tree = nv.build_tree([
        _ev("1", "phase_started", phase="design"),
        _ev("2", "step_started", step="spec", model="m", provider="anthropic",
            interactive=False),
        _ev("3", "step_started", kind="agent_gate", gate_stem="spec-lint",
            model="m", provider="anthropic", interactive=False),
        _ev("4", "gate_evaluated", kind="agent", gate="spec-lint",
            action={"kind": "continue"}),
        _ev("5", "step_completed", step="spec", action="continue"),
    ])
    phase = tree.root.children[0]
    step = phase.children[0]
    assert step.name == "spec" and step.kind is None and step.closed is True
    # The agent gate is a StepNode-shaped container nested inside the step.
    gate_containers = [c for c in step.children if isinstance(c, nv.StepNode)]
    assert len(gate_containers) == 1
    gc = gate_containers[0]
    assert gc.kind == "agent_gate"
    assert gc.name == "spec-lint"
    assert gc.closed is True
    assert isinstance(gc.outcome, nv.OutcomeLine)
    assert gc.outcome.action == {"kind": "continue"}


def test_phase_level_agent_gate_attaches_to_phase() -> None:
    tree = nv.build_tree([
        _ev("1", "phase_started", phase="design"),
        _ev("2", "step_started", step="spec", model="m", provider="anthropic",
            interactive=False),
        _ev("3", "step_completed", step="spec", action="continue"),
        _ev("4", "step_started", kind="agent_gate", gate_stem="review",
            model="m", provider="anthropic", interactive=False),
        _ev("5", "gate_evaluated", kind="agent", gate="review",
            action={"kind": "continue"}),
    ])
    phase = tree.root.children[0]
    containers = [c for c in phase.children if isinstance(c, nv.StepNode)]
    assert [c.name for c in containers] == ["spec", "review"]
    assert containers[1].kind == "agent_gate"


def test_user_message_text_extracted_inline_without_heavy_import() -> None:
    import sys
    sys.modules.pop("imp.cli.render_helpers", None)
    tree = nv.build_tree([
        _ev("1", "phase_started", phase="p"),
        _ev("2", "step_started", step="s", model="m", provider="anthropic"),
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
        _ev("1", "phase_started", phase="p"),
        _ev("2", "step_started", step="s", model="m", provider="anthropic"),
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
        _ev("1", "phase_started", phase="p"),
        _ev("2", "step_started", step="s", model="m", provider="anthropic"),
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
    return [_ev("p", "phase_started", phase="p"),
            _ev("s", "step_started", step="s", model="m", provider="anthropic"),
            *content,
            _ev("c", "step_completed", step="s", action="continue")]


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


def test_human_gate_pending_then_evaluated_builds_one_gate_node() -> None:
    tree = nv.build_tree([
        _ev("1", "phase_started", phase="review"),
        _ev("2", "human_gate_pending", phase="review", prompt="Merge?",
            options=[{"label": "Merge"}, {"label": "Back"}]),
        _ev("3", "gate_evaluated", kind="human", action={"kind": "continue"}),
    ])
    phase = tree.root.children[0]
    gates = [c for c in phase.children if isinstance(c, nv.GateNode)]
    assert len(gates) == 1
    g = gates[0]
    assert g.kind == "human"
    assert g.prompt == "Merge?"
    assert [o["label"] for o in g.options] == ["Merge", "Back"]
    assert g.action == {"kind": "continue"}


def test_script_gate_evaluated_builds_leaf_gate_node() -> None:
    tree = nv.build_tree([
        _ev("1", "phase_started", phase="review"),
        _ev("2", "gate_evaluated", kind="script", context={"exit": 0},
            action={"kind": "continue"}),
    ])
    g = next(c for c in tree.root.children[0].children if isinstance(c, nv.GateNode))
    assert g.kind == "script"
    assert g.context == {"exit": 0}
    assert g.action == {"kind": "continue"}


def test_stub_gate_evaluated_builds_leaf_gate_node() -> None:
    tree = nv.build_tree([
        _ev("1", "phase_started", phase="review"),
        _ev("2", "gate_evaluated", kind="stub", action={"kind": "goto",
            "target": "implement"}),
    ])
    g = next(c for c in tree.root.children[0].children if isinstance(c, nv.GateNode))
    assert g.kind == "stub"
    assert g.action == {"kind": "goto", "target": "implement"}


def test_step_level_human_gate_nests_inside_open_step() -> None:
    # Step gates run inside _execute_step, before step_completed → step is open.
    tree = nv.build_tree([
        _ev("1", "phase_started", phase="p"),
        _ev("2", "step_started", step="s", model="m", provider="anthropic"),
        _ev("3", "gate_evaluated", kind="stub", action={"kind": "continue"}),
        _ev("4", "step_completed", step="s", action="continue"),
    ])
    step = tree.root.children[0].children[0]
    assert any(isinstance(c, nv.GateNode) for c in step.children)


def test_error_event_becomes_error_node() -> None:
    tree = nv.build_tree([
        _ev("1", "phase_started", phase="p"),
        _ev("2", "error", category="auth", message="Invalid API key"),
    ])
    errs = [c for c in tree.root.children[0].children if isinstance(c, nv.ErrorNode)]
    assert len(errs) == 1
    assert errs[0].category == "auth"
    assert errs[0].message == "Invalid API key"
    assert errs[0].is_warning is False


def test_runner_warning_is_styled_non_terminal_error_node() -> None:
    tree = nv.build_tree([
        _ev("1", "phase_started", phase="p"),
        _ev("2", "step_started", step="s", model="m", provider="anthropic"),
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
        _ev("1", "phase_started", phase="p"),
        _ev("2", "phase_exhausted", phase="p"),         # legacy
        _ev("3", "never_heard_of_this", foo="bar"),     # unknown
    ])
    fbs = [c for c in tree.root.children[0].children if isinstance(c, nv.FallbackLine)]
    assert {f.event_type for f in fbs} == {"phase_exhausted", "never_heard_of_this"}
    assert fbs[1].payload == {"foo": "bar"}


INTERLEAVED_LOG = [
    _ev("c", "change_created", change_id=1, title="parity"),
    _ev("p", "phase_started", phase="impl"),
    _ev("s", "step_started", step="build", model="m", provider="anthropic"),
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
    _ev("done", "step_completed", step="build", action="continue"),
    _ev("pc", "phase_completed", phase="impl"),
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
    log = [*INTERLEAVED_LOG[:-2], _ev("wp", "workflow_paused", reason="ctrl-c")]
    batch = nv.build_tree(log)
    b = nv.NarrativeBuilder()
    for e in log:
        b.add(e)
    assert nv.structural_repr(batch.root) == nv.structural_repr(b.tree.root)


def test_resume_reentry_reuses_phase_and_resets_step() -> None:
    seed = [
        _ev("c", "change_created", change_id=1, title="t"),
        _ev("p", "phase_started", phase="design"),
        _ev("s1", "step_started", step="spec", model="m", provider="anthropic"),
        _ev("s1c", "step_completed", step="spec", action="continue"),
        _ev("s2", "step_started", step="build", model="m", provider="anthropic"),
        _ev("ll1", "llm_response", agent_id="step:design/build", provider="anthropic",
            model="m", stop_reason="end_turn", usage={},
            raw={"content": [{"type": "text", "text": "partial attempt"}]}),
        _ev("wp", "workflow_paused", reason="ctrl-c"),
        _ev("wr", "workflow_resumed"),
    ]
    live = [
        _ev("p2", "phase_started", phase="design"),        # re-emitted on resume
        _ev("s2b", "step_started", step="build", model="m", provider="anthropic"),
        _ev("ll2", "llm_response", agent_id="step:design/build", provider="anthropic",
            model="m", stop_reason="end_turn", usage={},
            raw={"content": [{"type": "text", "text": "fresh complete"}]}),
        _ev("s2c", "step_completed", step="build", action="continue"),
        _ev("pc", "phase_completed", phase="design"),
    ]
    tree = nv.build_tree(seed + live)
    phases = [c for c in tree.root.children if isinstance(c, nv.PhaseNode)]
    assert len(phases) == 1                              # design NOT duplicated
    assert phases[0].closed is True
    steps = [c for c in phases[0].children if isinstance(c, nv.StepNode)]
    assert [s.name for s in steps] == ["spec", "build"]  # build NOT duplicated
    build = steps[1]
    texts = [c.text for c in build.children if isinstance(c, nv.AgentTextNode)]
    assert texts == ["fresh complete"]                   # partial replaced by re-run
    assert build.closed is True
    assert build.outcome.action == "continue"


def test_goto_loop_keeps_separate_phase_iterations() -> None:
    # No workflow_resumed marker → two design nodes (faithful loop iterations).
    events = [
        _ev("p1", "phase_started", phase="design"),
        _ev("s1", "step_started", step="spec", model="m", provider="anthropic"),
        _ev("s1c", "step_completed", step="spec", action="continue"),
        _ev("g", "gate_evaluated", kind="stub",
            action={"kind": "goto", "target": "design"}),   # loop back, phase left open
        _ev("p2", "phase_started", phase="design"),         # second iteration
        _ev("s2", "step_started", step="spec", model="m", provider="anthropic"),
        _ev("s2c", "step_completed", step="spec", action="continue"),
        _ev("pc", "phase_completed", phase="design"),
    ]
    tree = nv.build_tree(events)
    phases = [c for c in tree.root.children if isinstance(c, nv.PhaseNode)]
    assert len(phases) == 2                              # both iterations preserved


def test_resume_into_a_new_phase_does_not_reuse() -> None:
    # Interrupted phase completed and advanced; resume re-emits a DIFFERENT phase.
    seed = [
        _ev("p1", "phase_started", phase="design"),
        _ev("s1", "step_started", step="spec", model="m", provider="anthropic"),
        _ev("s1c", "step_completed", step="spec", action="continue"),
        _ev("pc", "phase_completed", phase="design"),     # design CLOSED
        _ev("wp", "workflow_paused", reason="ctrl-c"),
        _ev("wr", "workflow_resumed"),
    ]
    live = [_ev("p2", "phase_started", phase="implement")]
    tree = nv.build_tree(seed + live)
    names = [c.name for c in tree.root.children if isinstance(c, nv.PhaseNode)]
    assert names == ["design", "implement"]              # implement is genuinely new


def test_ask_user_is_first_class_node_not_a_burst() -> None:
    tree = nv.build_tree([
        ev("1", "change_created", change_id=1, title="x"),
        ev("2", "phase_started", phase="design"),
        ev("3", "step_started", step="spec", model="m", provider="anthropic"),
        ev("4", "tool_call", agent_id="step:design/spec", id="q1",
           name="ask_user", input={"question": "Which port?"}),
        ev("5", "tool_result", agent_id="step:design/spec", id="q1",
           content="8080", error=False),
    ])
    step = tree.root.children[0].children[0]
    ask_nodes = [c for c in step.children if isinstance(c, nv.AskUserNode)]
    assert len(ask_nodes) == 1
    assert ask_nodes[0].question == "Which port?"
    assert ask_nodes[0].answer == "8080"
    # No spurious tool burst was created for the ask_user call/result.
    assert not any(isinstance(c, nv.ToolBurstFold) for c in step.children)


def test_ask_user_call_flushes_open_burst() -> None:
    tree = nv.build_tree([
        ev("1", "change_created", change_id=1, title="x"),
        ev("2", "phase_started", phase="design"),
        ev("3", "step_started", step="spec", model="m", provider="anthropic"),
        ev("4", "tool_call", agent_id="step:design/spec", id="b1",
           name="read_file", input={"path": "a"}),
        ev("5", "tool_result", agent_id="step:design/spec", id="b1", content="ok"),
        ev("6", "tool_call", agent_id="step:design/spec", id="q1",
           name="ask_user", input={"question": "Go?"}),
    ])
    step = tree.root.children[0].children[0]
    kinds = [type(c).__name__ for c in step.children]
    assert kinds == ["ToolBurstFold", "AskUserNode"]  # burst closed before the question


def test_current_activity_reports_open_burst() -> None:
    b = nv.NarrativeBuilder()
    for e in [
        ev("1", "change_created", change_id=1, title="x"),
        ev("2", "phase_started", phase="design"),
        ev("3", "step_started", step="spec", model="m", provider="anthropic"),
        ev("4", "tool_call", agent_id="step:design/spec", id="b1",
           name="read_file", input={"path": "config.py"}),
    ]:
        b.add(e)
    assert b.current_burst is not None
    act = b.current_activity()
    assert act is not None and "spec" in act and "read_file" in act and "1 tools" in act


def test_current_activity_idle_gap_between_nodes() -> None:
    b = nv.NarrativeBuilder()
    for e in [
        ev("1", "change_created", change_id=1, title="x"),
        ev("2", "phase_started", phase="design"),
        ev("3", "step_started", step="spec", model="m", provider="anthropic"),
    ]:
        b.add(e)
    assert b.current_burst is None
    assert b.current_activity() == "spec · working"


def test_current_activity_none_when_no_step() -> None:
    b = nv.NarrativeBuilder()
    b.add(ev("1", "change_created", change_id=1, title="x"))
    assert b.current_activity() is None


def test_subagent_current_activity_includes_nested_detail() -> None:
    b = nv.NarrativeBuilder()
    for e in [
        ev("1", "change_created", change_id=1, title="x"),
        ev("2", "phase_started", phase="design"),
        ev("3", "step_started", step="spec", model="m", provider="anthropic"),
        ev("4", "tool_call", agent_id="step:design/spec", id="t1",
           name="task", input={"subagent_name": "coder"}),
        ev("5", "tool_call", agent_id="sub:4", id="n1",
           name="edit_file", input={"path": "executor.py"}),
    ]:
        b.add(e)
    act = b.current_activity()
    assert act is not None and "task → coder" in act and "executor.py" in act


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
