from __future__ import annotations

import argparse
import json
from pathlib import Path

from imp.cli.main import (
    _search_active,
    _search_archived,
    _search_backlog,
    _selected_search_zones,
)
from imp.engine import archive
from imp.engine.change import Change
from tests.conftest import _seed_control


def _archive(tmp_path: Path, quest_id: str, title: str) -> None:
    events = tmp_path / f"{quest_id}.jsonl"
    events.write_text("")
    change_json = tmp_path / f"{quest_id}.change.json"
    change_json.write_text(json.dumps({
        "id": quest_id, "title": title, "base_branch": "main",
        "created_at": "2026-06-01T00:00:00+00:00", "depends_on": []}))
    state_json = tmp_path / f"{quest_id}.state.json"
    state_json.write_text(json.dumps(
        {"id": quest_id, "status": "abandoned", "step": "spec"}))
    archive.archive_change(
        tmp_path, quest_id, events_path=events,
        change_json_path=change_json, state_json_path=state_json)


def _ns(**kw: bool) -> argparse.Namespace:
    base = {"active": False, "backlog": False, "archived": False}
    base.update(kw)
    return argparse.Namespace(**base)


def test_selected_zones_defaults_to_all_three() -> None:
    assert _selected_search_zones(_ns()) == {"active", "backlog", "archive"}


def test_selected_zones_are_composable() -> None:
    assert _selected_search_zones(_ns(backlog=True)) == {"backlog"}
    assert _selected_search_zones(_ns(active=True, archived=True)) == {"active", "archive"}


def test_search_active_labels_zone_and_carries_prose(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    Change.create(tmp_path, title="Dark mode inflight",
                  quest_id="liveid", base_branch="main")
    hits = _search_active(tmp_path, "dark")
    assert [h.quest_id for h in hits] == ["liveid"]
    assert hits[0].zone == "active"
    assert hits[0].title == "Dark mode inflight"


def test_search_backlog_matches_seed_opening_body(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    Change.create_draft(
        tmp_path, title="Flaky tracker", quest_id="bk1", base_branch="main",
        origin="idea", seed_opening="Flaky tracker\n\ntracking test_widget_timeout")
    # Term lives only in the description body, not the id or title.
    hits = _search_backlog(tmp_path, "test_widget_timeout")
    assert [h.quest_id for h in hits] == ["bk1"]
    assert hits[0].zone == "backlog"
    assert "test_widget_timeout" in hits[0].content


def test_search_backlog_excludes_nonmatching(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    Change.create_draft(
        tmp_path, title="Unrelated idea", quest_id="bk2", base_branch="main",
        origin="idea", seed_opening="Unrelated idea")
    assert _search_backlog(tmp_path, "zzznope") == []


def test_search_archived_labels_zone_and_carries_content(tmp_path: Path) -> None:
    _archive(tmp_path, "arcid", "Dark archived work")
    hits = _search_archived(tmp_path, "dark")
    assert [h.quest_id for h in hits] == ["arcid"]
    assert hits[0].zone == "archive"
    assert hits[0].title == "Dark archived work"


def _user_msg_line(text: str, *, human: bool = True) -> str:
    return json.dumps({
        "type": "user_message",
        "payload": {"content": [{"type": "text", "text": text}], "human": human},
    }) + "\n"


def test_search_active_matches_conversation_prose_only(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    live = Change.create(tmp_path, title="Quiet chat", quest_id="liveid",
                         base_branch="main")
    live.events_path.write_text(_user_msg_line("please wire a 300s deadline soon"))
    # "300s" is in neither the id nor the title — only the conversation prose.
    hits = _search_active(tmp_path, "300s")
    assert [h.quest_id for h in hits] == ["liveid"]
    assert hits[0].zone == "active"
    assert "300s" in hits[0].content


def test_search_active_excludes_nonhuman_user_message(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    live = Change.create(tmp_path, title="Quiet chat", quest_id="liveid",
                         base_branch="main")
    live.events_path.write_text(_user_msg_line("cover-check injected prompt", human=False))
    # The term lives only in a non-human (injected) user_message -> not searchable,
    # and it is in neither the id "liveid" nor the title "Quiet chat".
    assert _search_active(tmp_path, "cover-check") == []
