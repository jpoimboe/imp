from __future__ import annotations

import json
from pathlib import Path

import imp.cli.main as m
from imp.engine import archive
from imp.engine.abandon import archive_conversation
from imp.engine.archive import archive_quest
from imp.engine.archive import read as archive_read
from imp.engine.errors import ArchiveMismatch
from imp.engine.quest import Quest
from imp.paths import project_state_dir
from tests.conftest import _seed_control


def _archive_terminal(control: Path, cid: str, status: str) -> None:
    """An archived, non-resumable (completed/abandoned) change — no live home,
    archive record resumable defaults False."""
    events = control / f"{cid}.jsonl"
    events.write_text("")
    cj = control / f"{cid}.quest.json"
    cj.write_text(json.dumps({
        "id": cid, "title": cid.title(), "base_branch": "main",
        "created_at": "2026-06-01T00:00:00+00:00", "depends_on": []}))
    sj = control / f"{cid}.state.json"
    sj.write_text(json.dumps({"id": cid, "status": status, "step": "merge"}))
    archive_quest(control, cid, events_path=events,
                   quest_json_path=cj, state_json_path=sj)


def _live_and_shelved(control: Path) -> None:
    _seed_control(control)
    Quest.create_draft(control, title="Live", quest_id="lodo40",
                       base_branch="main", origin="idea")
    shelved = Quest.create_draft(control, title="Shelved", quest_id="lodo77",
                                 base_branch="main")
    archive_conversation(shelved)
    assert not (project_state_dir(control) / "quests" / "lodo77").exists()


def _data_reason(stored: int) -> str:
    return str(ArchiveMismatch(floor="data", stored=stored,
                               known=archive._DATA_VERSION))


def test_match_rows_include_live_resumable(tmp_path: Path) -> None:
    """Two live resumable quests sharing a stem are both matches."""
    _seed_control(tmp_path)
    Quest.create_draft(tmp_path, title="One", quest_id="lodo40",
                        base_branch="main", origin="idea")
    Quest.create_draft(tmp_path, title="Two", quest_id="lodo77",
                        base_branch="main", origin="idea")
    rows, unreadable = m._resumable_matches(tmp_path, "lodo")
    assert {r[0] for r in rows} == {"lodo40", "lodo77"}
    assert unreadable is None


def test_match_rows_include_archived_resumable(tmp_path: Path) -> None:
    """An archived-but-resumable "shelved" conversation (live home deleted,
    archive record resumable=True) is a match by its stem."""
    _seed_control(tmp_path)
    ch = Quest.create_draft(tmp_path, title="Left", quest_id="lodo40",
                             base_branch="main")
    archive_conversation(ch)
    assert not (project_state_dir(tmp_path) / "quests" / "lodo40").exists()
    rows, unreadable = m._resumable_matches(tmp_path, "lodo")
    assert {r[0] for r in rows} == {"lodo40"}
    assert unreadable is None


def test_match_rows_exclude_archived_terminal(tmp_path: Path) -> None:
    """A live resumable + an archived completed quest share the stem, but only
    the resumable one is a match (the terminal archived id is excluded)."""
    _seed_control(tmp_path)
    Quest.create_draft(tmp_path, title="Live", quest_id="lodo40",
                        base_branch="main", origin="idea")
    _archive_terminal(tmp_path, "lodo77", "completed")
    assert "lodo77" in archive_read.archived_ids(tmp_path)   # guard: it IS archived
    rows, unreadable = m._resumable_matches(tmp_path, "lodo")
    assert {r[0] for r in rows} == {"lodo40"}
    assert unreadable is None


def test_match_rows_empty_when_token_matches_nothing(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    Quest.create_draft(tmp_path, title="One", quest_id="lodo40",
                        base_branch="main", origin="idea")
    assert m._resumable_matches(tmp_path, "zzzz") == ([], None)


def test_above_the_data_floor_reports_the_unreadable_archived_candidate(
    tmp_path: Path,
) -> None:
    """Floor skew ahead: the live row is still returned AND the archived
    candidate is named with the floor text that blocked it."""
    _live_and_shelved(tmp_path)
    ahead = archive._DATA_VERSION + 1
    archive.data_version_path(tmp_path).write_text(str(ahead))

    rows, unreadable = m._resumable_matches(tmp_path, "lodo")

    assert {r[0] for r in rows} == {"lodo40"}
    assert unreadable == m._UnreadableMatches(
        ids=("lodo77",), reason=_data_reason(ahead))


def test_below_the_data_floor_reports_the_unreadable_archived_candidate(
    tmp_path: Path,
) -> None:
    _live_and_shelved(tmp_path)
    below = archive._DATA_VERSION - 1
    archive._write_data_version(tmp_path, below)

    rows, unreadable = m._resumable_matches(tmp_path, "lodo")

    assert {r[0] for r in rows} == {"lodo40"}
    assert unreadable == m._UnreadableMatches(
        ids=("lodo77",), reason=_data_reason(below))
