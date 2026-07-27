"""A tagged provider-error node is withheld until the turn's outcome resolves it:
an error that carries the prose onward drops it by identity, any other resolving event
— including a text-less error — releases it, and bookkeeping rows pass through."""
from __future__ import annotations

from typing import Any

from imp.cli import narrative as nv
from imp.cli.render_text import render_tree

PROSE = "Failed to authenticate. API Error: 401 OAuth access token has been revoked."
ORDINARY = "Reading the config file and planning the edits."
AID = "step:p/s"


def _ev(eid: str, etype: str, agent_id: str | None = None, **payload: Any) -> dict[str, Any]:
    return {"id": eid, "agent_id": agent_id, "type": etype, "payload": payload}


def _msg(eid: str, body: str, *, tagged: bool, agent_id: str | None = AID) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "provider": "claude_code", "model": "m", "usage": {}, "stop_reason": None,
        "raw": {"content": [{"type": "text", "text": body}]}}
    if tagged:
        payload["provider_error"] = True
    return {"id": eid, "agent_id": agent_id, "type": "agent_message", "payload": payload}


def _step(*content: dict[str, Any]) -> list[dict[str, Any]]:
    return [_ev("s1", "step_started", step="s", kind="agent", phase="p",
                model="m", provider="claude_code"), *content]


def _texts(node: Any) -> list[str]:
    found: list[str] = []
    if isinstance(node, nv.AgentTextNode):
        found.append(node.text)
    for c in getattr(node, "children", []):
        found.extend(_texts(c))
    return found


def _render(events: list[dict[str, Any]]) -> str:
    return "\n".join(render_tree(nv.build_tree(events), full=False, color=False, width=100))


def _error_nodes(node: Any) -> list[nv.ErrorNode]:
    found: list[nv.ErrorNode] = []
    if isinstance(node, nv.ErrorNode):
        found.append(node)
    for c in getattr(node, "children", []):
        found.extend(_error_nodes(c))
    return found


def test_backoff_error_drops_the_tagged_node_across_a_query_result() -> None:
    # The drop is by node identity: detail.last_agent_text is the classifier's own
    # string here, so no text match could find the node.
    events = _step(
        _msg("a1", PROSE, tagged=True),
        _ev("q1", "query_result", agent_id=AID, usage={}, total_cost_usd=0.0),
        _ev("e1", "error", agent_id=AID, category="auth", kind="http_401",
            message="claude CLI: 401", detail={"last_agent_text": "claude CLI: 401"}),
        _ev("r1", "agent_retry", category="auth", attempt=2, backoff_seconds=2.0))
    tree = nv.build_tree(events)
    assert PROSE not in _texts(tree.root)
    out = _render(events)
    assert PROSE not in out
    assert "error: [auth]" not in out
    assert "[auth] retry 2 (2.0s)" in out


def test_codex_usage_patch_row_does_not_resolve_the_tag() -> None:
    events = _step(
        _msg("a1", PROSE, tagged=True),
        _ev("u1", "agent_message_usage_patch", agent_id=AID, usage={}),
        _ev("e1", "error", agent_id=AID, category="auth", kind="http_401",
            message="claude CLI: 401", detail={"last_agent_text": PROSE}))
    assert PROSE not in _texts(nv.build_tree(events).root)


def test_fatal_quota_error_drops_the_node_and_the_red_line_carries_the_text() -> None:
    events = _step(
        _msg("a1", PROSE, tagged=True),
        _ev("q1", "query_result", agent_id=AID, usage={}, total_cost_usd=0.0),
        _ev("e1", "error", agent_id=AID, category="quota", kind="QuotaError",
            message="claude CLI: usage limit", detail={"last_agent_text": PROSE}))
    tree = nv.build_tree(events)
    assert PROSE not in _texts(tree.root)
    reds = [n for n in _error_nodes(tree.root) if not n.is_warning]
    assert len(reds) == 1
    assert reds[0].detail == PROSE          # the red node is the surviving carrier
    assert "error: [quota] claude CLI: usage limit" in _render(events)


def test_fatal_quota_error_drops_the_node_across_a_codex_usage_patch() -> None:
    # AC 8(b) under codex: same fatal drop, with codex's bookkeeping row between.
    events = _step(
        _msg("a1", PROSE, tagged=True),
        _ev("u1", "agent_message_usage_patch", agent_id=AID, usage={}),
        _ev("e1", "error", agent_id=AID, category="quota", kind="QuotaError",
            message="claude CLI: usage limit", detail={"last_agent_text": PROSE}))
    tree = nv.build_tree(events)
    assert PROSE not in _texts(tree.root)
    reds = [n for n in _error_nodes(tree.root) if not n.is_warning]
    assert len(reds) == 1 and reds[0].detail == PROSE


def test_untagged_prose_survives_a_backoff_error_that_names_it() -> None:
    # AC 12: non-anchoring prose is the agent's own text — it renders in both views.
    events = _step(
        _msg("a1", ORDINARY, tagged=False),
        _ev("e1", "error", agent_id=AID, category="network", kind="terminated",
            message="claude_code: terminated", detail={"last_agent_text": ORDINARY}),
        _ev("r1", "agent_retry", category="network", attempt=1, backoff_seconds=1.0))
    assert ORDINARY in _texts(nv.build_tree(events).root)
    assert ORDINARY in _render(events)


def test_a_tool_call_releases_the_tag_and_a_later_error_keeps_the_prose() -> None:
    events = _step(
        _msg("a1", PROSE, tagged=True),
        _ev("t1", "tool_call", agent_id=AID, id="b1", name="bash", input={"cmd": "x"}),
        _ev("t2", "tool_result", agent_id=AID, id="b1", content="ok", error=False),
        _ev("e1", "error", agent_id=AID, category="auth", kind="http_401",
            message="claude CLI: 401", detail={"last_agent_text": PROSE}))
    tree = nv.build_tree(events)
    assert PROSE in _texts(tree.root)       # released, and never retro-deleted


def test_with_mixed_flush_only_the_tagged_node_drops_and_ordinary_prose_still_renders() -> None:
    # The split fix: ordinary node (untagged) + error node (tagged); on error only
    # the tagged node is dropped — ordinary prose must survive in both views.
    events = _step(
        _msg("a1", ORDINARY, tagged=False),
        _msg("a2", PROSE, tagged=True),
        _ev("q1", "query_result", agent_id=AID, usage={}, total_cost_usd=0.0),
        _ev("e1", "error", agent_id=AID, category="auth", kind="http_401",
            message="claude CLI: 401", detail={"last_agent_text": "claude CLI: 401"}),
        _ev("r1", "agent_retry", category="auth", attempt=2, backoff_seconds=2.0))
    tree = nv.build_tree(events)
    assert ORDINARY in _texts(tree.root)
    assert PROSE not in _texts(tree.root)
    out = _render(events)
    assert ORDINARY in out
    assert PROSE not in out


def test_a_second_prose_group_releases_the_first_and_only_the_trailing_one_drops() -> None:
    events = _step(
        _msg("a1", ORDINARY, tagged=True),
        _msg("a2", PROSE, tagged=True),
        _ev("q1", "query_result", agent_id=AID, usage={}, total_cost_usd=0.0),
        _ev("e1", "error", agent_id=AID, category="auth", kind="http_401",
            message="claude CLI: 401", detail={"last_agent_text": "claude CLI: 401"}))
    texts = _texts(nv.build_tree(events).root)
    assert ORDINARY in texts
    assert PROSE not in texts


def test_awaiting_user_input_releases_the_tag_before_the_prompt() -> None:
    events = _step(
        _msg("a1", PROSE, tagged=True),
        _ev("w1", "awaiting_user_input", agent_id=AID, prompt="your turn"),
        _ev("e1", "error", agent_id=AID, category="auth", kind="http_401",
            message="claude CLI: 401", detail={"last_agent_text": PROSE}))
    assert PROSE in _texts(nv.build_tree(events).root)


def test_an_audit_error_passes_through_and_keeps_the_prose() -> None:
    events = _step(
        _msg("a1", PROSE, tagged=True),
        _ev("e1", "error", agent_id=AID, category="terminated", kind="terminated",
            message="subagent 'coder' failed (terminated)", audit=True))
    assert PROSE in _texts(nv.build_tree(events).root)


def test_an_interrupt_error_releases_the_tagged_node_because_nothing_carries_it() -> None:
    # `config`/`interrupted` paints nothing, so it carries nothing: dropping here
    # would be prose vanishing from both views with no carrier.
    events = _step(
        _msg("a1", PROSE, tagged=True),
        _ev("e1", "error", agent_id=AID, category="config", kind="interrupted",
            message="interrupted"))
    tree = nv.build_tree(events)
    assert PROSE in _texts(tree.root)
    assert PROSE in _render(events)
    assert _error_nodes(tree.root) == []      # the interrupt still paints nothing itself


def test_a_stalled_error_releases_the_tagged_node_and_the_prose_still_renders() -> None:
    # The 401-then-wedge shape: the provider printed a genuine 401 and hung, so the
    # stall watchdog fires instead of the classified error. Its payload has no
    # detail, so the user must still see the 401 and not only the stall warning.
    events = _step(
        _msg("a1", PROSE, tagged=True),
        _ev("q1", "query_result", agent_id=AID, usage={}, total_cost_usd=0.0),
        _ev("e1", "error", agent_id=AID, category="stalled", kind="stalled",
            message="no events for 300s"),
        _ev("r1", "agent_retry", category="stalled", attempt=1, backoff_seconds=1.0))
    tree = nv.build_tree(events)
    assert PROSE in _texts(tree.root)
    out = _render(events)
    assert PROSE in out
    assert "no response for" in out           # the stall warning paints alongside it


def test_one_category_both_drops_and_releases_by_whether_the_error_carries_the_text() -> None:
    # The predicate reads the payload, not the category: agent_output_missing arrives both ways.
    carried = _step(
        _msg("a1", PROSE, tagged=True),
        _ev("e1", "error", agent_id=AID, category="agent_output_missing",
            kind="no_complete_step", message="agent finished without calling complete_step",
            detail={"last_agent_text": PROSE}))
    bare = _step(
        _msg("a1", PROSE, tagged=True),
        _ev("e1", "error", agent_id=AID, category="agent_output_missing",
            kind="output_validation", message="output validation: missing status"))
    assert PROSE not in _texts(nv.build_tree(carried).root)
    assert PROSE in _texts(nv.build_tree(bare).root)


def test_a_backoff_error_with_no_retry_behind_it_still_shows_no_red_line() -> None:
    # Budget exhaustion returns Pause before any agent_retry is emitted: the
    # branch's early return is the only thing suppressing the red line there.
    events = _step(
        _msg("a1", PROSE, tagged=True),
        _ev("q1", "query_result", agent_id=AID, usage={}, total_cost_usd=0.0),
        _ev("e1", "error", agent_id=AID, category="auth", kind="http_401",
            message="claude CLI: 401", detail={"last_agent_text": PROSE}))
    tree = nv.build_tree(events)
    assert [n for n in _error_nodes(tree.root) if not n.is_warning] == []
    assert "error: [auth]" not in _render(events)


def test_a_closing_fold_releases_its_subagents_pending_tag() -> None:
    b = nv.NarrativeBuilder()
    for e in _step(
        _ev("tk1", "tool_call", agent_id=AID, id="b1", name="sub_agent",
            input={"subagent_name": "reviewer", "task_description": "go"}),
        _msg("su1", PROSE, tagged=True, agent_id="sub:tk1"),
    ):
        b.add(e)
    fold = next(c for c in b.tree.root.children[0].children[0].children
                if isinstance(c, nv.SubagentFold))
    node = next(c for c in fold.tree.root.children if isinstance(c, nv.AgentTextNode))
    assert node.error_pending is True                    # withheld while the fold runs

    b.add(_ev("r1", "tool_result", agent_id=AID, id="b1",
              content='{"status":"Success"}', error=False))
    assert node.error_pending is False                   # released by the fold close
    assert PROSE in _texts(fold.tree.root)
    b.finalize()
    assert PROSE in "\n".join(
        render_tree(b.tree, full=True, color=False, width=100))


def test_finalize_releases_a_tag_no_event_ever_resolved() -> None:
    b = nv.NarrativeBuilder()
    for e in _step(_msg("a1", PROSE, tagged=True)):
        b.add(e)
    node = next(c for c in b.tree.root.children[0].children[0].children
                if isinstance(c, nv.AgentTextNode))
    assert node.error_pending is True

    b.finalize()
    assert node.error_pending is False
    # The flag is not the point: the prose must still be there to render.
    assert PROSE in "\n".join(render_tree(b.tree, full=False, color=False, width=100))


def test_finalize_releases_a_tag_inside_a_fold_that_never_closed() -> None:
    b = nv.NarrativeBuilder()
    for e in _step(
        _ev("tk1", "tool_call", agent_id=AID, id="b1", name="sub_agent",
            input={"subagent_name": "reviewer", "task_description": "go"}),
        _msg("su1", PROSE, tagged=True, agent_id="sub:tk1"),
    ):
        b.add(e)
    fold = next(c for c in b.tree.root.children[0].children[0].children
                if isinstance(c, nv.SubagentFold))
    node = next(c for c in fold.tree.root.children if isinstance(c, nv.AgentTextNode))

    b.finalize()
    assert node.error_pending is False
    # A running fold still expands under --full, so the prose is reachable there.
    assert PROSE in "\n".join(render_tree(b.tree, full=True, color=False, width=100))


def test_workflow_resumed_releases_a_tag_in_a_dangling_fold() -> None:
    b = nv.NarrativeBuilder()
    for e in _step(
        _ev("tk1", "tool_call", agent_id=AID, id="b1", name="sub_agent",
            input={"subagent_name": "reviewer", "task_description": "go"}),
        _msg("su1", PROSE, tagged=True, agent_id="sub:tk1"),
    ):
        b.add(e)
    fold = next(c for c in b.tree.root.children[0].children[0].children
                if isinstance(c, nv.SubagentFold))
    node = next(c for c in fold.tree.root.children if isinstance(c, nv.AgentTextNode))

    # The resume finalizes the fold as interrupted and drops its membership entry,
    # so this is the last chance to release its tag.
    b.add(_ev("w1", "workflow_resumed"))
    assert node.error_pending is False
    # Flag alone can't distinguish release (node stays) from drop (node gone); check render.
    assert PROSE in "\n".join(render_tree(b.tree, full=True, color=False, width=100))


def test_a_failing_subagents_audit_error_leaves_its_closing_prose_rendered() -> None:
    # subagents.py appends `audit` rows for a fatal/budget-exhausted subagent; they
    # paint nothing, so they must not delete prose that paints.
    events = _step(
        _ev("tk1", "tool_call", agent_id=AID, id="b1", name="sub_agent",
            input={"subagent_name": "reviewer", "task_description": "go"}),
        _msg("su1", PROSE, tagged=True, agent_id="sub:tk1"),
        _ev("se1", "error", agent_id="sub:tk1", category="terminated", kind="terminated",
            message="subagent 'reviewer' failed (terminated)", audit=True),
        _ev("r1", "tool_result", agent_id=AID, id="b1",
            content='{"status":"Error"}', error=True))
    tree = nv.build_tree(events)
    # A fold is a LEAF in the top-level tree (SubagentFold has no `children`), so
    # the subagent's prose is reachable only through its own nested tree.
    fold = next(c for c in tree.root.children[0].children[0].children
                if isinstance(c, nv.SubagentFold))
    assert PROSE in _texts(fold.tree.root)
    assert PROSE in "\n".join(
        render_tree(tree, full=True, color=False, width=100))
