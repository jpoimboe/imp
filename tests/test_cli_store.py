from __future__ import annotations

import json
from pathlib import Path

from imp.cli.store import LocalStore, make_store
from imp.engine import archive, worktree
from imp.engine.quest import Quest
from imp.engine.state import State
from tests.conftest import _seed_control


def _append_event(quest: Quest, event: dict) -> None:
    with quest.events_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(event) + "\n")


def _archive(control: Path, quest_id: str, status: str = "completed") -> None:
    ch = Quest.load(control, quest_id)
    st = State.load(ch.state_dir)
    st.status = status
    st.save(ch.state_dir)
    archive.archive_quest(
        control, quest_id,
        events_path=ch.events_path,
        quest_json_path=ch.quest_dir / "quest.json",
        state_json_path=ch.state_dir / "state.json")
    worktree.remove_worktree(control, quest_id)


def test_make_store_returns_localstore(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    assert isinstance(make_store(tmp_path), LocalStore)


def test_get_events_live_archived_and_unknown(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = Quest.create(tmp_path, title="Live", quest_id="liveq", base_branch="main")
    _append_event(ch, {
        "id": "e1", "timestamp": "2026-06-01T00:00:00+00:00",
        "type": "user_message",
        "payload": {"human": True, "content": [{"type": "text", "text": "hi there"}]}})
    store = make_store(tmp_path)

    live = store.get_events("liveq")
    assert live is not None
    assert any(e.get("type") == "user_message" for e in live)

    Quest.create(tmp_path, title="Gone", quest_id="goneq", base_branch="main")
    _archive(tmp_path, "goneq")
    archived = store.get_events("goneq")
    assert archived is not None and len(archived) >= 1

    assert store.get_events("nosuch") is None
