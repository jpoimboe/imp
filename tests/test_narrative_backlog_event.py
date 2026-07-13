from __future__ import annotations

from imp.cli.narrative import build_tree


def test_started_quest_created_does_not_clobber_root() -> None:
    events = [
        {"type": "quest_created",
         "payload": {"id": "par111", "title": "Parent"}},
        {"type": "quest_created",
         "payload": {"id": "2026-06-16-new999", "title": "Record a preference",
                     "kind": "started", "depends_on": []}},
    ]
    tree = build_tree(events)
    assert tree.root.id == "par111"
    assert tree.root.title == "Parent"


def test_backlog_quest_created_does_not_clobber_root() -> None:
    events = [
        {"type": "quest_created",
         "payload": {"id": "par111", "title": "Parent"}},
        {"type": "quest_created",
         "payload": {"id": "2026-06-16-new999", "title": "Follow-up",
                     "kind": "backlog", "depends_on": ["2026-06-16-par111"]}},
    ]
    tree = build_tree(events)
    assert tree.root.id == "par111"
    assert tree.root.title == "Parent"
