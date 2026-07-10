"""A transient retry-with-backoff error (auth/network/rate_limit)
renders quiet — no red `error:` line, and the held scary auth prose is dropped so
the following agent_retry breadcrumb is the single surviving one-liner."""
from __future__ import annotations

from imp.cli import narrative as nv
from imp.cli.render_text import render_tree

AUTH_PROSE = "Failed to authenticate. API Error: 401 Invalid authentication credentials"


def _ev(eid, etype, agent_id=None, **payload):
    return {"id": eid, "agent_id": agent_id, "type": etype, "payload": payload}


def _agent_texts(node) -> list[str]:
    found: list[str] = []
    if isinstance(node, nv.AgentTextNode):
        found.append(node.text)
    for c in getattr(node, "children", []):
        found.extend(_agent_texts(c))
    return found


def _render(events) -> str:
    return "\n".join(render_tree(nv.build_tree(events), full=False, color=False, width=80))


def test_auth_error_drops_matching_held_prose_and_suppresses_line() -> None:
    # The held auth prose matches detail.last_agent_text: the error line is
    # suppressed, the prose node is dropped, and the agent_retry breadcrumb
    # survives as the single line for the blip.
    events = [
        _ev("a1", "llm_response", agent_id=None,
            raw={"content": [{"type": "text", "text": AUTH_PROSE}]}, usage={}),
        _ev("e1", "error", agent_id=None,
            category="auth", kind="http_401", message="claude CLI: 401",
            detail={"last_agent_text": AUTH_PROSE}),
        _ev("r1", "agent_retry", agent_id=None,
            category="auth", attempt=1, backoff_seconds=1.0),
    ]
    tree = nv.build_tree(events)
    assert AUTH_PROSE not in _agent_texts(tree.root)   # prose node deleted
    out = _render(events)
    assert "error: [auth]" not in out                  # no red error line
    assert AUTH_PROSE not in out                        # prose never paints
    assert "warning: [auth] retry 1 (1.0s)" in out      # breadcrumb survives


def test_auth_error_prefix_match_drops_prose_over_500_chars() -> None:
    # detail.last_agent_text is stored truncated to [:500]; the held node holds the
    # FULL prose. A stripped PREFIX compare (not equality) must still drop it.
    full = (AUTH_PROSE + " ") * 12          # > 500 chars
    assert len(full) > 500
    events = [
        _ev("a1", "llm_response", agent_id=None,
            raw={"content": [{"type": "text", "text": full}]}, usage={}),
        _ev("e1", "error", agent_id=None,
            category="auth", message="claude CLI: 401",
            detail={"last_agent_text": full[:500]}),
    ]
    tree = nv.build_tree(events)
    assert _agent_texts(tree.root) == []    # dropped despite the truncated match value


def test_network_error_keeps_unrelated_narration() -> None:
    # A network error may be preceded by unrelated real narration whose text does
    # NOT match last_agent_text: suppress the error line but KEEP the narration.
    narration = "Reading the config file and planning the edits."
    events = [
        _ev("a1", "llm_response", agent_id=None,
            raw={"content": [{"type": "text", "text": narration}]}, usage={}),
        _ev("e1", "error", agent_id=None,
            category="network", message="claude CLI: connection reset",
            detail={"last_agent_text": "an entirely different error string"}),
    ]
    tree = nv.build_tree(events)
    assert narration in _agent_texts(tree.root)   # real narration preserved
    out = _render(events)
    assert "error: [network]" not in out          # error line still suppressed
    assert narration in out


def test_network_error_without_last_agent_text_keeps_narration() -> None:
    # No last_agent_text at all: the non-empty guard means nothing is dropped, and
    # the error line is still suppressed.
    narration = "Analyzing the failing test output."
    events = [
        _ev("a1", "llm_response", agent_id=None,
            raw={"content": [{"type": "text", "text": narration}]}, usage={}),
        _ev("e1", "error", agent_id=None,
            category="network", message="claude CLI: EOF", detail={}),
    ]
    tree = nv.build_tree(events)
    assert narration in _agent_texts(tree.root)
    assert "error: [network]" not in _render(events)


def test_auth_error_drops_held_prose_even_behind_a_flushed_rename() -> None:
    # A change_renamed stashes a pending rename that _h_llm_response flushes as a
    # RenameNode right AFTER the held auth narration, so the container tail is
    # [held_auth_prose, RenameNode]. The held-prose lookup must skip that trailing
    # RenameNode and still drop the prose; the rename line itself survives.
    events = [
        _ev("n1", "change_renamed", agent_id=None, new_title="A better title"),
        _ev("a1", "llm_response", agent_id=None,
            raw={"content": [{"type": "text", "text": AUTH_PROSE}]}, usage={}),
        _ev("e1", "error", agent_id=None,
            category="auth", kind="http_401", message="claude CLI: 401",
            detail={"last_agent_text": AUTH_PROSE}),
        _ev("r1", "agent_retry", agent_id=None,
            category="auth", attempt=1, backoff_seconds=1.0),
    ]
    tree = nv.build_tree(events)
    assert AUTH_PROSE not in _agent_texts(tree.root)           # prose still dropped
    rename_nodes = [c for c in tree.root.children if isinstance(c, nv.RenameNode)]
    assert len(rename_nodes) == 1                               # rename line preserved
    out = _render(events)
    assert "error: [auth]" not in out
    assert AUTH_PROSE not in out


def test_api_error_still_renders_hard_error_line() -> None:
    # Boundary: `api` is NOT a backoff-retry category — its hard error line stays.
    out = _render([
        _ev("e1", "error", agent_id=None,
            category="api", message="500 upstream"),
    ])
    assert "error: [api] 500 upstream" in out
