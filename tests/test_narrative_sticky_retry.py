"""A step-level sticky retry (unknown/terminated ridden out inside a transient
window) renders like every other backoff retry: the streamed provider error's
red line is dropped by the following agent_retry breadcrumb, leaving only the
calm breadcrumb. A cold give-up (no following agent_retry) keeps its red line.
The event sequences below mirror what the step-level loop emits — the streamed
`error` (emit_events=True) immediately followed by `agent_retry`."""
from __future__ import annotations

from imp.cli import narrative as nv
from imp.cli.render_text import render_tree


def _ev(eid, etype, agent_id=None, **payload):
    return {"id": eid, "agent_id": agent_id, "type": etype, "payload": payload}


def _render(events) -> str:
    return "\n".join(
        render_tree(nv.build_tree(events), full=False, color=False, width=80))


def test_sticky_retry_drops_preceding_unknown_error_line() -> None:
    events = [
        _ev("e1", "error", category="unknown",
            message="provider died mid-outage"),
        _ev("r1", "agent_retry", category="unknown", attempt=2,
            backoff_seconds=2.0),
    ]
    out = _render(events)
    assert "error: [unknown]" not in out            # red line dropped
    assert "[unknown] retry 2 (2.0s)" in out         # calm breadcrumb survives


def test_sticky_retry_drops_preceding_terminated_error_line() -> None:
    events = [
        _ev("e1", "error", category="terminated", message="agent terminated"),
        _ev("r1", "agent_retry", category="terminated", attempt=2,
            backoff_seconds=2.0),
    ]
    out = _render(events)
    assert "error: [terminated]" not in out
    assert "[terminated] retry 2 (2.0s)" in out


def test_cold_unknown_give_up_keeps_red_line() -> None:
    # No following agent_retry -> a cold give-up (prior_backoff_attempts == 0 ->
    # Pause) keeps its hard error line untouched.
    out = _render([
        _ev("e1", "error", category="unknown", message="provider died"),
    ])
    assert "error: [unknown] provider died" in out


def test_agent_retry_does_not_drop_different_category_error() -> None:
    # Same-category only: a preceding `api` hard error survives an `unknown`
    # retry breadcrumb.
    events = [
        _ev("e1", "error", category="api", message="500 upstream"),
        _ev("r1", "agent_retry", category="unknown", attempt=2,
            backoff_seconds=2.0),
    ]
    out = _render(events)
    assert "error: [api] 500 upstream" in out


def test_agent_retry_does_not_drop_preceding_warning_node() -> None:
    # is_warning guard: a warning ErrorNode with matching category must survive.
    # _h_error renders the stalled teardown as a warning; the following stalled
    # agent_retry must not swallow it.
    events = [
        _ev("e1", "error", category="stalled", message="no events for 120s"),
        _ev("r1", "agent_retry", category="stalled", attempt=1,
            backoff_seconds=30.0),
    ]
    out = _render(events)
    assert "no response for" in out   # stalled warning line preserved


def test_network_retry_breadcrumb_unaffected() -> None:
    # Uniform: network's red line was already suppressed by _h_error, so the drop
    # is a no-op and the breadcrumb still renders.
    events = [
        _ev("e1", "error", category="network", message="connection reset"),
        _ev("r1", "agent_retry", category="network", attempt=1,
            backoff_seconds=1.0),
    ]
    out = _render(events)
    assert "error: [network]" not in out
    assert "warning: [network] retry 1 (1.0s)" in out
