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
from imp.engine import archive
from imp.engine.archive import read as archive_read
from imp.engine.archive import record, records_root
from imp.engine.archive.maintenance import reap_archived_homes
from imp.engine.quest import LAYOUT_VERSION, Quest
from imp.engine.state import State
from imp.paths import project_state_dir
from tests.conftest import _seed_control


def _home(control: Path, cid: str, status: str, **state: object) -> Path:
    home = project_state_dir(control) / "quests" / cid
    (home / "state").mkdir(parents=True)
    (home / "quest.json").write_text(json.dumps({
        "id": cid, "title": cid.title(), "base_branch": "main",
        "created_at": "2026-06-01T00:00:00+00:00",
        "layout_version": LAYOUT_VERSION}))
    (home / "state" / "state.json").write_text(json.dumps({
        "id": cid, "status": status, "step": None, **state}))
    (home / "events.jsonl").write_text(
        '{"id":"e0","parent_id":null,"agent_id":null,'
        '"timestamp":"2026-06-01T00:00:01+00:00","type":"quest_created",'
        '"payload":{"id":"' + cid + '"}}\n')
    return home


def _state_bytes(home: Path) -> bytes:
    return (home / "state" / "state.json").read_bytes()


def test_helper_archives_merged_completed_home(tmp_path: Path) -> None:
    home = _home(tmp_path, "mrgd", "completed")
    _archive_stranded_home(tmp_path, "mrgd", home, _state_bytes(home))
    rows = make_store(tmp_path).completed_rows()
    assert [(r.quest_id, r.status) for r in rows] == [("mrgd", "completed")]


def test_helper_archives_completed_home(tmp_path: Path) -> None:
    home = _home(tmp_path, "done", "completed")
    _archive_stranded_home(tmp_path, "done", home, _state_bytes(home))
    rows = make_store(tmp_path).completed_rows()
    assert [(r.quest_id, r.status) for r in rows] == [("done", "completed")]


def test_helper_archives_archived_home_resumable_with_steps(tmp_path: Path) -> None:
    home = _home(tmp_path, "shlv", "archived")
    (home / "steps" / "plan").mkdir(parents=True)
    (home / "steps" / "plan" / "1.md").write_text("draft plan")
    _archive_stranded_home(tmp_path, "shlv", home, _state_bytes(home))
    rec = record.read_record(records_root(tmp_path), "shlv")
    assert rec is not None and rec["resumable"] is True
    assert archive_read.read_step_blobs(tmp_path, "shlv") == {"plan/1.md": b"draft plan"}


def test_helper_archives_abandoned_home_as_tombstone(tmp_path: Path) -> None:
    home = _home(tmp_path, "abnd", "abandoned")
    (home / "steps").mkdir()
    (home / "steps" / "1.md").write_text("x")
    _archive_stranded_home(tmp_path, "abnd", home, _state_bytes(home))
    rec = record.read_record(records_root(tmp_path), "abnd")
    assert rec is not None and rec["resumable"] is False
    assert archive_read.read_step_blobs(tmp_path, "abnd") == {}


@pytest.mark.parametrize("status", ["running", "paused", "brand_new_status"])
def test_helper_skips_unclassified_status(tmp_path: Path, status: str) -> None:
    home = _home(tmp_path, "keep", status)
    _archive_stranded_home(tmp_path, "keep", home, _state_bytes(home))
    assert not archive_read.is_archived(tmp_path, "keep")


def test_helper_does_not_rearchive(tmp_path: Path) -> None:
    home = _home(tmp_path, "once", "completed")
    _archive_stranded_home(tmp_path, "once", home, _state_bytes(home))
    (home / "state" / "state.json").write_text(json.dumps({
        "id": "once", "status": "abandoned", "step": None}))
    _archive_stranded_home(tmp_path, "once", home, _state_bytes(home))
    assert archive_read.status_of(tmp_path, "once") == "completed"


def test_scan_archives_stranded_merged_home(tmp_path: Path) -> None:
    _home(tmp_path, "mrgd", "completed")
    _home(tmp_path, "runn", "running")
    ids = {h.quest_id for h in make_store(tmp_path).scan_homes()}
    assert ids == {"mrgd", "runn"}
    assert archive_read.is_archived(tmp_path, "mrgd")
    assert not archive_read.is_archived(tmp_path, "runn")


def test_live_locked_terminal_home_is_skipped(tmp_path: Path) -> None:
    home = _home(tmp_path, "busy", "completed")
    (home / "lock").write_text(str(os.getpid()))
    digest = next(h for h in make_store(tmp_path).scan_homes() if h.quest_id == "busy")
    assert digest.lock_live is True
    assert not archive_read.is_archived(tmp_path, "busy")


def test_list_shows_stranded_home_under_completed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_control(tmp_path)
    _home(tmp_path, "mrgd", "completed")
    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)
    monkeypatch.setenv("NO_COLOR", "1")
    buf = io.StringIO()
    with redirect_stdout(buf):
        assert main(["list"]) == 0
    out = buf.getvalue()
    assert "completed:" in out
    assert "mrgd" in out.split("completed:")[1]


def test_stranded_archived_home_resumes_after_reap(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    q = Quest.create_draft(tmp_path, title="Conv", quest_id="conv", base_branch="main")
    st = State.load(q.state_dir)
    st.status = "archived"
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


def test_failed_archiving_does_not_abort_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _home(tmp_path, "brokn", "completed")
    _home(tmp_path, "fine", "completed")
    real = archive.archive_quest

    def _boom(control: Path, quest_id: str, **kw: object) -> None:
        if quest_id == "brokn":
            raise RuntimeError("blob store unavailable")
        real(control, quest_id, **kw)  # type: ignore[arg-type]

    monkeypatch.setattr(archive, "archive_quest", _boom)
    ids = {h.quest_id for h in make_store(tmp_path).scan_homes()}
    assert ids == {"brokn", "fine"}
    assert archive_read.is_archived(tmp_path, "fine")
    assert not archive_read.is_archived(tmp_path, "brokn")


def _parked(control: Path, cid: str) -> Path:
    """A stranded `archived` home a pre-17 run left on the retired slow gate."""
    home = _home(control, cid, "archived", step="converge-verify",
                 resume_pending="converge-verify", step_label="waiting to test")
    meta = json.loads((home / "quest.json").read_text())
    (home / "quest.json").write_text(json.dumps({**meta, "workflow": "default"}))
    return home


def test_a_failed_archive_after_a_heal_still_hands_over_the_healed_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _parked(tmp_path, "hlth")

    def _boom(control: Path, quest_id: str, **kw: object) -> None:
        raise RuntimeError("blob store unavailable")

    monkeypatch.setattr(archive, "archive_quest", _boom)
    digest = next(h for h in make_store(tmp_path).scan_homes() if h.quest_id == "hlth")
    assert digest.state_json is not None
    assert json.loads(digest.state_json)["step"] == "wait-for-head"
    assert digest.state_json == _state_bytes(home)
    assert not archive_read.is_archived(tmp_path, "hlth")


def _list(control: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setattr(m, "_control_root", lambda: control)
    monkeypatch.setenv("NO_COLOR", "1")
    buf = io.StringIO()
    with redirect_stdout(buf):
        assert main(["list"]) == 0
    return buf.getvalue()


def test_list_heals_and_archives_a_parked_home_in_a_markerless_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_control(tmp_path)
    home = _parked(tmp_path, "nomg")
    assert not archive.data_version_path(tmp_path).exists()

    _list(tmp_path, monkeypatch)

    assert json.loads(_state_bytes(home))["step"] == "wait-for-head"
    rec = record.read_record(records_root(tmp_path), "nomg")
    assert rec is not None and rec["resumable"] is True
    assert rec["final_state"]["step"] == "wait-for-head"
    assert rec["final_state"]["step_label"] == "waiting in merge queue"
    assert archive.data_version_path(tmp_path).read_text().strip() == str(
        archive._DATA_VERSION)


def test_list_heals_a_home_a_stale_process_finished_after_the_upgrade(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_control(tmp_path)
    first = _home(tmp_path, "frst", "completed")
    _archive_stranded_home(tmp_path, "frst", first, _state_bytes(first))
    assert archive.data_version_path(tmp_path).read_text().strip() == str(
        archive._DATA_VERSION)
    home = _parked(tmp_path, "strg")

    _list(tmp_path, monkeypatch)

    assert json.loads(_state_bytes(home))["step"] == "wait-for-head"
    rec = record.read_record(records_root(tmp_path), "strg")
    assert rec is not None
    assert rec["final_state"]["step"] == "wait-for-head"
    assert rec["final_state"]["resume_pending"] is None


def test_live_locked_parked_home_is_not_rewritten(tmp_path: Path) -> None:
    home = _parked(tmp_path, "busy")
    (home / "lock").write_text(str(os.getpid()))
    before = _state_bytes(home)

    digest = next(h for h in make_store(tmp_path).scan_homes() if h.quest_id == "busy")

    assert digest.lock_live is True
    assert digest.state_json == before
    assert _state_bytes(home) == before
    assert not archive_read.is_archived(tmp_path, "busy")


def test_an_unwritable_parked_home_is_left_as_is(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rdon = _parked(tmp_path, "rdon")
    _home(tmp_path, "fine", "completed")
    before = _state_bytes(rdon)
    target = rdon / "state" / "state.json"
    real = archive.durable.durable_replace_bytes

    def _refuse(path: Path, data: bytes) -> None:
        if Path(path) == target:
            raise PermissionError(13, "read-only", str(path))
        real(path, data)

    monkeypatch.setattr(archive.durable, "durable_replace_bytes", _refuse)
    digests = {h.quest_id: h for h in make_store(tmp_path).scan_homes()}

    assert set(digests) == {"rdon", "fine"}
    assert _state_bytes(rdon) == before
    assert digests["rdon"].state_json == before
    assert not archive_read.is_archived(tmp_path, "rdon")
    assert archive_read.is_archived(tmp_path, "fine")
