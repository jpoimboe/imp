"""output_validation_nudge renders as a low-noise warning, not an error."""

from __future__ import annotations

from imp.cli import narrative as nv
from imp.cli.narrative import NarrativeBuilder
from imp.cli.render_text import render_tree


def test_output_validation_nudge_handler_registered() -> None:
    assert "output_validation_nudge" in NarrativeBuilder._HANDLERS


def test_output_validation_nudge_renders_as_warning_not_error() -> None:
    tree = nv.build_tree([
        {"id": "e1", "parent_id": None, "agent_id": None,
         "type": "output_validation_nudge",
         "payload": {"message": "missing required field 'blocking'",
                     "attempt": 1}},
    ])
    out = "\n".join(render_tree(tree, full=False, color=False, width=80))
    assert "warning:" in out
    assert "missing required field 'blocking'" in out
    assert "error:" not in out
