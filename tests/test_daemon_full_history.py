from __future__ import annotations

from pathlib import Path

from imp.engine.quest import Quest
from imp.remote.daemon.discover import full_history
from tests.conftest import _seed_control


def test_full_history_returns_ordered_augmented_events(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    change = Quest.create(
        tmp_path, title="Dark mode", quest_id="alpha", base_branch="main")
    log = change.event_log()
    log.append(agent_id=None, type="notice", payload={"text": "hi"})
    log.append(agent_id=None, type="awaiting_user_input", payload={"prompt": "go?"})

    events = full_history(tmp_path, "alpha")

    # Last two in order; Quest.create may seed earlier lifecycle events ahead.
    assert [e["type"] for e in events][-2:] == ["notice", "awaiting_user_input"]
    last = events[-1]
    assert last["change"]["id"] == "alpha"
    assert last["change"]["title"] == "Dark mode"
    assert "step" in last["change"] and "status" in last["change"]
    assert last["payload"]["prompt"] == "go?"     # raw payload preserved
    assert "id" in last and "timestamp" in last   # raw id/timestamp preserved


def test_full_history_unknown_change_is_empty(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    assert full_history(tmp_path, "ghost") == []
