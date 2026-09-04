"""A transient retry-with-backoff error (auth/network/rate_limit) renders quiet —
no red `error:` line, and a tagged provider-error node is dropped so the following
agent_retry breadcrumb is the single surviving one-liner."""
from __future__ import annotations

from imp.cli import narrative as nv
from imp.cli.render_text import render_tree

AUTH_PROSE = "API Error: 401 rejected by gateway"


def _ev(eid, etype, agent_id=None, **payload):
    return {"id": eid, "agent_id": agent_id, "type": etype, "payload": payload}


def _agent_texts(node) -> list[str]:
    found: list[str] = []
    if isinstance(node, nv.AgentTextNode):
        found.append(node.text)
    for c in getattr(node, "children", []):
        found.extend(_agent_texts(c))
    return found


def _lean(events) -> list[str]:
    return list(render_tree(nv.build_tree(events), full=False, color=False, width=160))


def _full(events) -> list[str]:
    return list(render_tree(nv.build_tree(events), full=True, color=False, width=160))


def _render(events) -> str:
    return "\n".join(_lean(events))


def test_auth_error_drops_matching_held_prose_and_suppresses_line() -> None:
    # The held auth prose matches detail.last_agent_text: the error line is
    # suppressed, the prose node is dropped, and the agent_retry breadcrumb
    # survives as the single line for the blip.
    events = [
        _ev("a1", "agent_message", agent_id=None,
            raw={"content": [{"type": "text", "text": AUTH_PROSE}]}, usage={},
            provider_error=True),
        _ev("e1", "error", agent_id=None,
            category="auth", kind="http_401", message="claude CLI: 401",
            detail={"last_agent_text": AUTH_PROSE}),
        _ev("r1", "agent_retry", agent_id=None,
            category="auth", attempt=2, backoff_seconds=2.0),
    ]
    tree = nv.build_tree(events)
    assert AUTH_PROSE not in _agent_texts(tree.root)   # prose node deleted
    out = _render(events)
    assert "error: [auth]" not in out                  # no red error line
    assert AUTH_PROSE not in out                        # prose never paints
    assert "warning: [auth] retry 2 (2.0s)" in out      # breadcrumb survives


def test_network_error_keeps_unrelated_narration() -> None:
    # A network error may be preceded by unrelated real narration whose text does
    # NOT match last_agent_text: suppress the error line but KEEP the narration.
    narration = "Reading the config file and planning the edits."
    events = [
        _ev("a1", "agent_message", agent_id=None,
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
        _ev("a1", "agent_message", agent_id=None,
            raw={"content": [{"type": "text", "text": narration}]}, usage={}),
        _ev("e1", "error", agent_id=None,
            category="network", message="claude CLI: EOF", detail={}),
    ]
    tree = nv.build_tree(events)
    assert narration in _agent_texts(tree.root)
    assert "error: [network]" not in _render(events)


def test_auth_error_drops_held_prose_even_behind_a_flushed_rename() -> None:
    # A RenameNode follows the tagged prose; identity drop removes prose while the rename survives.
    events = [
        _ev("n1", "quest_renamed", agent_id=None, new_title="A better title"),
        _ev("a1", "agent_message", agent_id=None,
            raw={"content": [{"type": "text", "text": AUTH_PROSE}]}, usage={},
            provider_error=True),
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


QUIET_PROSE = "API Error: 401 OAuth access token has been revoked."


def _healed_auth_blip() -> list[dict]:
    return [
        _ev("e1", "error", agent_id=None, category="auth", kind="http_401",
            message="claude CLI: 401"),
        _ev("r1", "agent_retry", agent_id=None, category="auth", attempt=1,
            backoff_seconds=1.0, message=QUIET_PROSE),
    ]


def test_first_auth_retry_renders_nothing_at_all_in_the_lean_view() -> None:
    # No warning line, no error line, and no stray blank separator either.
    assert _lean(_healed_auth_blip()) == []


def test_the_quiet_node_is_kept_in_the_tree_and_prints_under_full() -> None:
    tree = nv.build_tree(_healed_auth_blip())
    node = next(c for c in tree.root.children if isinstance(c, nv.ErrorNode))
    assert node.full_only is True
    assert (f"warning: [auth] retry 1 (1.0s) — {QUIET_PROSE}"
            in "\n".join(_full(_healed_auth_blip())))


def test_second_auth_retry_prints_unchanged_in_the_lean_view() -> None:
    out = "\n".join(_lean([
        _ev("r1", "agent_retry", agent_id=None, category="auth", attempt=2,
            backoff_seconds=2.0, message=QUIET_PROSE)]))
    assert f"warning: [auth] retry 2 (2.0s) — {QUIET_PROSE}" in out


def test_network_and_rate_limit_first_retries_are_quiet_too() -> None:
    for cat in ("network", "rate_limit"):
        assert _lean([_ev("r1", "agent_retry", agent_id=None, category=cat,
                          attempt=1, backoff_seconds=1.0)]) == []


def test_a_retry_without_a_numeric_attempt_still_prints() -> None:
    # `attempt` missing entirely, and the literal "?" — neither is the int 1.
    assert "warning: [auth] retry ? (1.0s)" in "\n".join(_lean([
        _ev("r1", "agent_retry", agent_id=None, category="auth",
            backoff_seconds=1.0)]))
    assert "warning: [auth] retry ? (1.0s)" in "\n".join(_lean([
        _ev("r2", "agent_retry", agent_id=None, category="auth", attempt="?",
            backoff_seconds=1.0)]))


def test_hinted_retry_without_a_backoff_is_quiet_at_attempt_one() -> None:
    # It falls through to the plain breadcrumb, so the rule catches it there.
    assert _lean([_ev("r1", "agent_retry", agent_id=None, category="rate_limit",
                      attempt=1, hinted=True)]) == []


def test_hinted_long_wait_still_prints_at_attempt_one() -> None:
    assert "[rate_limit] waiting for limit reset — retry 1 in 2h 13m" in "\n".join(
        _lean([_ev("r1", "agent_retry", agent_id=None, category="rate_limit",
                   attempt=1, backoff_seconds=8000.0, hinted=True)]))


def test_stalled_resume_line_still_prints_at_attempt_one() -> None:
    assert "warning: resuming — verifying prior work, not redoing it" in "\n".join(
        _lean([_ev("r1", "agent_retry", agent_id=None, category="stalled",
                   attempt=1, backoff_seconds=4.0)]))


def test_sticky_unknown_first_retry_still_prints() -> None:
    # `unknown` is not a retry-with-backoff category, so the rule never applies.
    assert "warning: [unknown] retry 1 (1.0s)" in "\n".join(
        _lean([_ev("r1", "agent_retry", agent_id=None, category="unknown",
                   attempt=1, backoff_seconds=1.0)]))
