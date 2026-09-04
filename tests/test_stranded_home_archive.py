from __future__ import annotations

import json
from pathlib import Path

import pytest

from imp.cli.store import _archive_stranded_home, make_store
from imp.engine.archive import read as archive_read
from imp.engine.quest import LAYOUT_VERSION
from imp.paths import project_state_dir


def _home(control: Path, cid: str, status: str) -> Path:
    home = project_state_dir(control) / "quests" / cid
    (home / "state").mkdir(parents=True)
    (home / "quest.json").write_text(json.dumps({
        "id": cid, "title": cid.title(), "base_branch": "main",
        "created_at": "2026-06-01T00:00:00+00:00",
        "layout_version": LAYOUT_VERSION}))
    (home / "state" / "state.json").write_text(json.dumps({
        "id": cid, "status": status, "step": None}))
    (home / "events.jsonl").write_text(
        '{"id":"e0","parent_id":null,"agent_id":null,'
        '"timestamp":"2026-06-01T00:00:01+00:00","type":"quest_created",'
        '"payload":{"id":"' + cid + '"}}\n')
    return home


def _state_bytes(home: Path) -> bytes:
    return (home / "state" / "state.json").read_bytes()


def test_helper_archives_merged_home(tmp_path: Path) -> None:
    home = _home(tmp_path, "mrgd", "merged")
    _archive_stranded_home(tmp_path, "mrgd", home, _state_bytes(home))
    rows = make_store(tmp_path).completed_rows()
    assert [(r.quest_id, r.status) for r in rows] == [("mrgd", "merged")]


def test_helper_archives_done_home(tmp_path: Path) -> None:
    home = _home(tmp_path, "done", "done")
    _archive_stranded_home(tmp_path, "done", home, _state_bytes(home))
    rows = make_store(tmp_path).completed_rows()
    assert [(r.quest_id, r.status) for r in rows] == [("done", "done")]


def test_helper_archives_shelved_home_resumable_with_steps(tmp_path: Path) -> None:
    home = _home(tmp_path, "shlv", "shelved")
    (home / "steps" / "plan").mkdir(parents=True)
    (home / "steps" / "plan" / "1.md").write_text("draft plan")
    _archive_stranded_home(tmp_path, "shlv", home, _state_bytes(home))
    from imp.engine.archive import record, records_root
    rec = record.read_record(records_root(tmp_path), "shlv")
    assert rec is not None and rec["resumable"] is True
    assert archive_read.read_step_blobs(tmp_path, "shlv") == {"plan/1.md": b"draft plan"}


def test_helper_archives_abandoned_home_as_tombstone(tmp_path: Path) -> None:
    home = _home(tmp_path, "abnd", "abandoned")
    (home / "steps").mkdir()
    (home / "steps" / "1.md").write_text("x")
    _archive_stranded_home(tmp_path, "abnd", home, _state_bytes(home))
    from imp.engine.archive import record, records_root
    rec = record.read_record(records_root(tmp_path), "abnd")
    assert rec is not None and rec["resumable"] is False
    assert archive_read.read_step_blobs(tmp_path, "abnd") == {}


@pytest.mark.parametrize("status", ["running", "paused", "brand_new_status"])
def test_helper_skips_unclassified_status(tmp_path: Path, status: str) -> None:
    home = _home(tmp_path, "keep", status)
    _archive_stranded_home(tmp_path, "keep", home, _state_bytes(home))
    assert not archive_read.is_archived(tmp_path, "keep")


def test_helper_does_not_rearchive(tmp_path: Path) -> None:
    home = _home(tmp_path, "once", "merged")
    _archive_stranded_home(tmp_path, "once", home, _state_bytes(home))
    (home / "state" / "state.json").write_text(json.dumps({
        "id": "once", "status": "done", "step": None}))
    _archive_stranded_home(tmp_path, "once", home, _state_bytes(home))
    assert archive_read.status_of(tmp_path, "once") == "merged"
