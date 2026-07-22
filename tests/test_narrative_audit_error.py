"""A per-subagent terminal audit `error` event (payload marker `audit: True`) is
recorded so a give-up's category stays queryable via `imp events`, but it must be
SILENT in the live narrative — no spurious red failure line inside a cleanly
retried / handed-back subagent's fold. The control test proves the marker is the
only thing suppressing the line (the same event without it still renders red)."""
from __future__ import annotations

from imp.cli import narrative as nv
from imp.cli.render_text import render_tree


def _ev(eid, etype, agent_id=None, **payload):
    return {"id": eid, "agent_id": agent_id, "type": etype, "payload": payload}


def _render(events) -> str:
    return "\n".join(
        render_tree(nv.build_tree(events), full=False, color=False, width=80))


def test_audit_error_renders_no_red_line() -> None:
    out = _render([
        _ev("e1", "error", agent_id="sub:abc", category="terminated",
            kind="terminated",
            message="subagent 'coder' failed (terminated)", audit=True),
    ])
    assert "error:" not in out
    assert "terminated" not in out


def test_non_audit_terminated_error_still_renders_red_line() -> None:
    # Falsifiability control: the SAME event without the `audit` marker still
    # renders its red line, so the marker is the sole suppressor.
    out = _render([
        _ev("e1", "error", agent_id="sub:abc", category="terminated",
            kind="terminated",
            message="subagent 'coder' failed (terminated)"),
    ])
    assert "error: [terminated] subagent 'coder' failed (terminated)" in out
