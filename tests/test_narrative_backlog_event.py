from __future__ import annotations

from imp.cli.narrative import build_tree


def test_backlog_change_created_does_not_clobber_root() -> None:
    events = [
        {"type": "change_created",
         "payload": {"id": "par111", "handle": "parent-par111",
                     "title": "Parent"}},
        {"type": "change_created",
         "payload": {"id": "2026-06-16-new999", "title": "Follow-up",
                     "kind": "backlog", "depends_on": ["2026-06-16-par111"]}},
    ]
    tree = build_tree(events)
    assert tree.root.change_id == "par111"
    assert tree.root.slug == "parent-par111"
    assert tree.root.title == "Parent"
