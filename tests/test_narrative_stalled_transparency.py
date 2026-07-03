"""Fix #3: stall transparency — calm teardown warning (_h_error) and verification
re-entry breadcrumb (_h_agent_retry), render-layer only."""
from __future__ import annotations

from imp.cli import narrative as nv
from imp.cli.render_text import render_tree

TEARDOWN = "warning: no response for 300s — resuming; work already saved is preserved"


def _render(events: list[dict[str, object]]) -> str:
    tree = nv.build_tree(events)
    return "\n".join(render_tree(tree, full=False, color=False, width=80))


def test_stalled_error_alone_renders_calm_warning() -> None:
    """The always-present stalled `error` (also the budget-exhausted terminal case,
    where no agent_retry follows) renders as a calm preservation-guarantee WARNING —
    not the alarming hard `error: no events for 300s` line the goal exists to kill."""
    out = _render([
        {"id": "e1", "parent_id": None, "agent_id": "step:implement/coder",
         "type": "error",
         "payload": {"category": "stalled", "kind": "stalled",
                     "message": "no events for 300s"}},
    ])
    assert TEARDOWN in out
    assert "error:" not in out            # no hard error node
    assert "no events for 300s" not in out  # the raw emitter message is reworded away


def test_non_stalled_error_keeps_hard_error_line() -> None:
    """A non-stalled error category is untouched — still a hard `error:` line."""
    out = _render([
        {"id": "e1", "parent_id": None, "agent_id": "step:implement/coder",
         "type": "error",
         "payload": {"category": "api", "message": "500 upstream"}},
    ])
    assert "error: [api] 500 upstream" in out
    assert "resuming" not in out          # the stalled rewording never leaks here
