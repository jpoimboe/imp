from __future__ import annotations

import json
from pathlib import Path

from imp.cli import main as m
from imp.engine.archive import read as archive_read
from imp.engine.archive import record, records_root
from imp.engine.change import Change
from imp.engine.state import State


def _draft(control: Path, *, has_started: bool) -> Change:
    quest_dir = control / ".imp" / "change"
    (quest_dir / "state").mkdir(parents=True, exist_ok=True)
    State(id="c", step="chat", step_attempt=0, status="new",
          has_started=has_started).save(quest_dir / "state")
    return Change(
        id="c", title="t", base_branch="main",
        control_root=control, project_root=control, quest_dir=quest_dir,
        materialized=False, seed_opening="do the thing")


def test_resume_replays_conversed_draft_after_archive_roundtrip(
        tmp_path: Path) -> None:
    # A draft that ran >=1 step (has_started True), archived resumable and
    # rehydrated. No events.jsonl exists at all, so the outcome cannot depend on
    # which conversational event types are present.
    change = _draft(tmp_path, has_started=True)
    final_state = json.loads(
        (change.state_dir / "state.json").read_text())
    record.write_record(records_root(tmp_path), record.build_record(
        {"id": "c", "title": "t", "base_branch": "main"},
        final_state, events_hash="", resumable=True))
    # Rehydrate: restore state.json from the archived final_state verbatim.
    (change.state_dir / "state.json").write_bytes(
        archive_read.read_state_json(tmp_path, "c"))

    assert m._resume_opens_fresh(change) is False   # replays, never restarts


def test_resume_opens_fresh_for_never_run_draft(tmp_path: Path) -> None:
    change = _draft(tmp_path, has_started=False)
    assert m._resume_opens_fresh(change) is True    # genuinely never ran
