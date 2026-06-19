from __future__ import annotations

from imp.cli.narrative import FallbackLine, build_tree


def test_cli_attached_renders_no_fallback_breadcrumb() -> None:
    events = [{"type": "cli_attached", "agent_id": None, "payload": {}}]
    tree = build_tree(events)
    assert not any(isinstance(c, FallbackLine) for c in tree.root.children)
