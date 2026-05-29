# tests/test_narrative.py
"""Tests for the narrative-tree model (src/imp/cli/narrative.py)."""
from __future__ import annotations

from imp.cli import narrative as nv


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
    ctx = [c for c in step.children if isinstance(c, nv.StepContextNode)][0]
    assert ctx.payload["system_prompt"] == "SYS"
    think = [c for c in step.children if isinstance(c, nv.ThinkingFold)][0]
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
    at = [c for c in step.children if isinstance(c, nv.AgentTextNode)][0]
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
    burst = [c for c in step.children if isinstance(c, nv.ToolBurstFold)][0]
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
    sub = [c for c in step.children if isinstance(c, nv.SubagentFold)][0]
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
    sub = [c for c in step.children if isinstance(c, nv.SubagentFold)][0]
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
    sub = [c for c in step.children if isinstance(c, nv.SubagentFold)][0]
    assert sub.running is True
    assert sub.outcome is None
