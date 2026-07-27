"""A tagged provider-error node is withheld until the turn's outcome resolves it:
an error drops it by identity, any other ordering-bearing event releases it, and
bookkeeping rows pass through."""
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


def test_an_interrupt_error_also_drops_the_tagged_node() -> None:
    # Accepted consequence of keying on the error event rather than the category:
    # a text-less category drops the node with no carrier. Pinned so a later reader
    # sees intent rather than a bug.
    events = _step(
        _msg("a1", PROSE, tagged=True),
        _ev("e1", "error", agent_id=AID, category="config", kind="interrupted",
            message="interrupted"))
    tree = nv.build_tree(events)
    assert PROSE not in _texts(tree.root)
    assert _error_nodes(tree.root) == []      # the interrupt paints nothing itself


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
