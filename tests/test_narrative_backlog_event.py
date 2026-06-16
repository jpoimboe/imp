from __future__ import annotations

from imp.cli.narrative import build_tree


def test_backlog_change_created_does_not_clobber_root() -> None:
    events = [
        {"type": "change_created",
         "payload": {"id": "2026-06-16-par111", "slug": "2026-06-16-parent",
                     "title": "Parent"}},
        {"type": "change_created",
         "payload": {"id": "2026-06-16-new999", "title": "Follow-up",
                     "kind": "backlog", "depends_on": ["2026-06-16-par111"]}},
    ]
    tree = build_tree(events)
    assert tree.root.change_id == "2026-06-16-par111"
    assert tree.root.slug == "2026-06-16-parent"
    assert tree.root.title == "Parent"
