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
