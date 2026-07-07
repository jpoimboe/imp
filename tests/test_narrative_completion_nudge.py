"""completion_nudge is telemetry-only: it renders nothing in the narrative."""

from __future__ import annotations

from imp.cli import narrative as nv
from imp.cli.narrative import NarrativeBuilder
from imp.cli.render_text import render_tree


def test_completion_nudge_handler_registered() -> None:
    assert "completion_nudge" in NarrativeBuilder._HANDLERS


def test_completion_nudge_renders_nothing() -> None:
    tree = nv.build_tree([
        {"id": "e1", "parent_id": None, "agent_id": None,
         "type": "completion_nudge",
         "payload": {"message": "Your turn ended without complete_step..."}},
    ])
    out = "\n".join(render_tree(tree, full=False, color=False, width=80))
    assert "nudged" not in out
    assert "bare turn-end" not in out
    assert "warning:" not in out
    assert out == ""
