from __future__ import annotations

import json
from pathlib import Path

import imp.cli.main as m
from imp.engine.abandon import archive_conversation
from imp.engine.archive import archive_change
from imp.engine.archive import read as archive_read
from imp.engine.change import Change
from tests.conftest import _seed_control


def _archive_terminal(control: Path, cid: str, status: str) -> None:
    """An archived, non-resumable (completed/abandoned) change — no live home,
    archive record resumable defaults False."""
    events = control / f"{cid}.jsonl"
    events.write_text("")
    cj = control / f"{cid}.change.json"
    cj.write_text(json.dumps({
        "id": cid, "title": cid.title(), "base_branch": "main",
        "created_at": "2026-06-01T00:00:00+00:00", "depends_on": []}))
    sj = control / f"{cid}.state.json"
    sj.write_text(json.dumps({"id": cid, "status": status, "step": "merge"}))
    archive_change(control, cid, events_path=events,
                   change_json_path=cj, state_json_path=sj)


def test_match_rows_include_live_resumable(tmp_path: Path) -> None:
    # AC 8: two live resumable Changes sharing a stem are both matches.
    _seed_control(tmp_path)
    Change.create_draft(tmp_path, title="One", change_id="lodo40",
                        base_branch="main", origin="idea")
    Change.create_draft(tmp_path, title="Two", change_id="lodo77",
                        base_branch="main", origin="idea")
    ids = {r[0] for r in m._resumable_match_rows(tmp_path, "lodo")}
    assert ids == {"lodo40", "lodo77"}


def test_match_rows_include_archived_resumable(tmp_path: Path) -> None:
    # AC 9: an archived-but-resumable "left" conversation (live home deleted,
    # archive record resumable=True) is a match by its stem.
    _seed_control(tmp_path)
    ch = Change.create_draft(tmp_path, title="Left", change_id="lodo40",
                             base_branch="main")
    archive_conversation(ch)
    assert not (tmp_path / ".imp" / "changes" / "lodo40").exists()
    ids = {r[0] for r in m._resumable_match_rows(tmp_path, "lodo")}
    assert ids == {"lodo40"}


def test_match_rows_exclude_archived_terminal(tmp_path: Path) -> None:
    # AC 10: a live resumable + an archived completed change share the stem, but
    # only the resumable one is a match (the terminal archived id is excluded).
    _seed_control(tmp_path)
    Change.create_draft(tmp_path, title="Live", change_id="lodo40",
                        base_branch="main", origin="idea")
    _archive_terminal(tmp_path, "lodo77", "completed")
    assert "lodo77" in archive_read.archived_ids(tmp_path)   # guard: it IS archived
    ids = {r[0] for r in m._resumable_match_rows(tmp_path, "lodo")}
    assert ids == {"lodo40"}


def test_match_rows_empty_when_token_matches_nothing(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    Change.create_draft(tmp_path, title="One", change_id="lodo40",
                        base_branch="main", origin="idea")
    assert m._resumable_match_rows(tmp_path, "zzzz") == []
