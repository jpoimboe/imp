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
