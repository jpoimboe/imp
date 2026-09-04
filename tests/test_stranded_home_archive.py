from __future__ import annotations

import io
import json
import os
from contextlib import redirect_stdout
from pathlib import Path

import pytest

import imp.cli.main as m
from imp.cli.main import main
from imp.cli.store import _archive_stranded_home, make_store
from imp.engine.archive import read as archive_read
from imp.engine.archive.maintenance import reap_archived_homes
from imp.engine.quest import LAYOUT_VERSION, Quest
from imp.engine.state import State
from imp.paths import project_state_dir
from tests.conftest import _seed_control


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


def test_scan_archives_stranded_merged_home(tmp_path: Path) -> None:
    _home(tmp_path, "mrgd", "merged")
    _home(tmp_path, "runn", "running")
    ids = {h.quest_id for h in make_store(tmp_path).scan_homes()}
    assert ids == {"mrgd", "runn"}
    assert archive_read.is_archived(tmp_path, "mrgd")
    assert not archive_read.is_archived(tmp_path, "runn")


def test_live_locked_terminal_home_is_skipped(tmp_path: Path) -> None:
    home = _home(tmp_path, "busy", "merged")
    (home / "lock").write_text(str(os.getpid()))
    digest = next(h for h in make_store(tmp_path).scan_homes() if h.quest_id == "busy")
    assert digest.lock_live is True
    assert not archive_read.is_archived(tmp_path, "busy")


def test_list_shows_stranded_home_under_completed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_control(tmp_path)
    _home(tmp_path, "mrgd", "merged")
    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)
    monkeypatch.setenv("NO_COLOR", "1")
    buf = io.StringIO()
    with redirect_stdout(buf):
        assert main(["list"]) == 0
    out = buf.getvalue()
    assert "completed:" in out
    assert "mrgd" in out.split("completed:")[1]


def test_stranded_shelved_home_resumes_after_reap(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    q = Quest.create_draft(tmp_path, title="Conv", quest_id="conv", base_branch="main")
    st = State.load(q.state_dir)
    st.status = "shelved"
    st.save(q.state_dir)
    (q.quest_dir / "steps" / "plan").mkdir(parents=True, exist_ok=True)
    (q.quest_dir / "steps" / "plan" / "1.md").write_text("draft plan")

    make_store(tmp_path).scan_homes()
    assert archive_read.is_archived(tmp_path, "conv")
    assert reap_archived_homes(tmp_path) == 1
    assert not (project_state_dir(tmp_path) / "quests" / "conv").exists()

    loaded = m._rehydrate_resumable(tmp_path, "conv")
    assert loaded is not None and loaded.id == "conv"
    assert (loaded.quest_dir / "steps" / "plan" / "1.md").read_text() == "draft plan"


def _seed_v11_terminal(control: Path, cid: str) -> Quest:
    q = Quest.create(control, title="T", quest_id=cid, base_branch="main")
    (q.state_dir / "state.json").write_text(json.dumps({
        "id": cid, "step": "code", "step_attempt": 0, "status": "merged",
        "resume_pending": None, "resume_mode": None,
        "vars": {}, "loop_counts": {}, "current_step": None, "retry_feedback": None,
    }, indent=2, sort_keys=True))
    meta_path = q.quest_dir / "quest.json"
    meta = json.loads(meta_path.read_text())
    meta["layout_version"] = 11
    meta_path.write_text(json.dumps(meta, indent=2, sort_keys=True))
    return q


def test_stale_layout_home_migrates_then_archives(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    _seed_v11_terminal(tmp_path, "stale")
    digest = next(h for h in make_store(tmp_path).scan_homes() if h.quest_id == "stale")
    assert digest.meta["layout_version"] == LAYOUT_VERSION
    assert archive_read.is_archived(tmp_path, "stale")
    assert json.loads(
        archive_read.read_quest_json(tmp_path, "stale") or b"{}"
    )["layout_version"] == LAYOUT_VERSION
