from __future__ import annotations

from imp.cli import narrative as nv
from imp.cli.leave import _was_renamed


class _Log:
    def __init__(self, events: list[dict]) -> None:
        self._events = events

    def read(self) -> list[dict]:
        return list(self._events)


def test_quest_created_sets_root_id_and_title() -> None:
    tree = nv.build_tree([
        {"id": "e1", "agent_id": None, "type": "quest_created",
         "payload": {"id": "abc123", "title": "add auth"}},
    ])
    assert tree.root.id == "abc123"
    assert tree.root.title == "add auth"


def test_quest_renamed_updates_root_title() -> None:
    tree = nv.build_tree([
        {"id": "e1", "agent_id": None, "type": "quest_created",
         "payload": {"id": "abc123", "title": "old"}},
        {"id": "e2", "agent_id": None, "type": "quest_renamed",
         "payload": {"id": "abc123", "old_title": "old", "new_title": "new"}},
    ])
    assert tree.root.title == "new"


def test_was_renamed_true_for_quest_renamed() -> None:
    assert _was_renamed(_Log([{"type": "quest_renamed"}])) is True


def test_legacy_event_keys_match_no_handler() -> None:
    # No-backcompat (§6 / AC #7): an old archived log's pre-quest created/renamed
    # event keys now match no registered handler, so they set no root identity
    # (build_tree renders an unregistered type as a fallback line, not as a title).
    # The legacy type strings are assembled at runtime so the deterministic token
    # sweep never rewrites this guard.
    legacy_created = "change" + "_created"
    legacy_renamed = "change" + "_renamed"
    tree = nv.build_tree([
        {"id": "e1", "agent_id": None, "type": legacy_created,
         "payload": {"id": "abc123", "title": "add auth"}},
    ])
    assert tree.root.id is None
    assert tree.root.title == ""
    assert _was_renamed(_Log([{"type": legacy_renamed}])) is False
