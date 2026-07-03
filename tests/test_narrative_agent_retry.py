"""Fix A: agent_retry renders inline (self-healed retry visibility)."""

from __future__ import annotations

import json

from imp.cli import narrative as nv
from imp.cli.narrative import NarrativeBuilder


def test_agent_retry_handler_registered() -> None:
    assert "agent_retry" in NarrativeBuilder._HANDLERS


def test_agent_retry_renders_category_and_attempt() -> None:
    tree = nv.build_tree([
        {"id": "e1", "parent_id": None, "agent_id": None,
         "type": "agent_retry",
         "payload": {"category": "network", "attempt": 1, "backoff_seconds": 1.0}},
    ])
    blob = json.dumps(nv.structural_repr(tree.root), default=str)
    assert "network" in blob
    assert "retry" in blob.lower()


def test_stalled_agent_retry_renders_verification_breadcrumb() -> None:
    from imp.cli.render_text import render_tree

    tree = nv.build_tree([
        {"id": "e1", "parent_id": None, "agent_id": None,
         "type": "agent_retry",
         "payload": {"category": "stalled", "attempt": 2, "backoff_seconds": 4.0}},
    ])
    out = "\n".join(render_tree(tree, full=False, color=False, width=80))

    assert "warning: resuming — verifying prior work, not redoing it" in out
    assert "[stalled] retry" not in out   # the old technical retry line is gone
    assert "error:" not in out
    assert "[config]" not in out
