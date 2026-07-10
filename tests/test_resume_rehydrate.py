from __future__ import annotations

import json
from pathlib import Path

import imp.cli.main as m
from imp.engine import archive
from imp.engine.abandon import archive_conversation
from imp.engine.change import Change
from imp.engine.state import State
from tests.conftest import _seed_control


def _archive_terminal(tmp_path: Path, quest_id: str, status: str) -> None:
    events = tmp_path / f"{quest_id}.jsonl"
    events.write_text("")
    cj = tmp_path / f"{quest_id}.change.json"
    cj.write_text(json.dumps({
        "id": quest_id, "title": quest_id.title(), "base_branch": "main",
        "created_at": "2026-06-01T00:00:00+00:00", "depends_on": []}))
    sj = tmp_path / f"{quest_id}.state.json"
    sj.write_text(json.dumps({"id": quest_id, "status": status, "step": "merge"}))
    archive.archive_change(
        tmp_path, quest_id, events_path=events,
        change_json_path=cj, state_json_path=sj)          # resumable defaults False


def test_rehydrate_resumable_rebuilds_and_loads(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = Change.create_draft(tmp_path, title="Conv", quest_id="conv",
                             base_branch="main")
    archive_conversation(ch)                              # resumable, live home dropped
    assert not (tmp_path / ".imp" / "changes" / "conv").exists()

    loaded = m._rehydrate_resumable(tmp_path, "conv")
    assert loaded is not None
    assert loaded.id == "conv"
    assert (tmp_path / ".imp" / "changes" / "conv").exists()   # live home rebuilt
    assert State.load(loaded.state_dir).status == "paused"     # the resumable resting status


def test_rehydrate_resumable_returns_none_for_terminal_record(tmp_path: Path) -> None:
    _archive_terminal(tmp_path, "done", "completed")
    assert m._rehydrate_resumable(tmp_path, "done") is None
    assert not (tmp_path / ".imp" / "changes" / "done").exists()   # never rebuilt


def test_rehydrate_resumable_unknown_returns_none(tmp_path: Path) -> None:
    assert m._rehydrate_resumable(tmp_path, "nope") is None


def test_load_or_rehydrate_returns_resume_gone_rc_for_terminal(tmp_path: Path) -> None:
    _archive_terminal(tmp_path, "done", "completed")
    # No live home and a non-resumable record → _resume_gone path (rc 0, "already
    # completed"), NOT a rehydrate.
    result = m._load_or_rehydrate(tmp_path, "done")
    assert result == 0
    assert not (tmp_path / ".imp" / "changes" / "done").exists()


def test_load_or_rehydrate_returns_loaded_change_for_resumable(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = Change.create_draft(tmp_path, title="Conv", quest_id="conv",
                             base_branch="main")
    archive_conversation(ch)
    result = m._load_or_rehydrate(tmp_path, "conv")
    assert isinstance(result, Change)                     # resumed, not an rc
    assert result.id == "conv"


def test_load_or_rehydrate_returns_loaded_change_when_live(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    Change.create_draft(tmp_path, title="Conv", quest_id="conv",
                        base_branch="main")
    result = m._load_or_rehydrate(tmp_path, "conv")
    assert isinstance(result, Change)
    assert result.id == "conv"
