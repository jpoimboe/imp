"""The one-shot 14 -> 15 upgrade: the stored status `shelved` becomes `archived`."""
from __future__ import annotations

import io
import json
import os
import sqlite3
from contextlib import redirect_stdout
from pathlib import Path
from typing import get_args

import pytest

import imp.cli.main as m
from imp.engine import archive
from imp.engine.abandon import archive_conversation
from imp.engine.archive import index, record
from imp.engine.archive import read as archive_read
from imp.engine.errors import ArchiveMismatch
from imp.engine.quest import LAYOUT_VERSION, Quest, live_quest_ids
from imp.engine.state import _LEGACY_STATUS, State, Status
from tests.conftest import _seed_control

QID = "shlv01"


def _user_version(control: Path) -> int:
    conn = sqlite3.connect(archive.index_path(control))
    try:
        return int(conn.execute("PRAGMA user_version").fetchone()[0])
    finally:
        conn.close()


def _index_status(control: Path, quest_id: str) -> str:
    conn = sqlite3.connect(archive.index_path(control))
    try:
        row = conn.execute(
            "SELECT status FROM quests WHERE id = ?", (quest_id,)).fetchone()
        return str(row[0])
    finally:
        conn.close()


def _record_status(control: Path, quest_id: str) -> str:
    rec = record.read_record(archive.records_root(control), quest_id)
    assert rec is not None
    return str(rec["final_state"]["status"])


def _marker(control: Path) -> str:
    return archive.data_version_path(control).read_text().strip()


def _seed_v14_archive(control: Path) -> None:
    """A real archived conversation, rolled back to exactly how a v14 install left it."""
    _seed_control(control)
    archive_conversation(Quest.create_draft(
        control, title="Dark mode chat", quest_id=QID, base_branch="main"))
    records = archive.records_root(control)
    rec = record.read_record(records, QID)
    assert rec is not None
    rec["final_state"]["status"] = "shelved"
    record.write_record(records, rec)
    archive.rebuild_index(control)
    conn = sqlite3.connect(archive.index_path(control))
    try:
        conn.execute("PRAGMA user_version = 7")     # the index floor v14 shipped
        conn.commit()
    finally:
        conn.close()
    archive._write_data_version(control, 14)


def _live_home_with_status(control: Path, quest_id: str, status: str) -> Path:
    q = Quest.create_draft(control, title=quest_id, quest_id=quest_id, base_branch="main")
    path = q.state_dir / "state.json"
    data = json.loads(path.read_text())
    data["status"] = status
    path.write_text(json.dumps(data, indent=2, sort_keys=True))
    return path


def _run(control: Path, monkeypatch: pytest.MonkeyPatch, argv: list[str]) -> str:
    monkeypatch.setattr(m, "_control_root", lambda: control)
    monkeypatch.setenv("NO_COLOR", "1")
    buf = io.StringIO()
    with redirect_stdout(buf):
        assert m.main(argv) == 0
    return buf.getvalue()


def test_a_blob_backed_read_migrates_record_and_index(tmp_path: Path) -> None:
    _seed_v14_archive(tmp_path)
    assert _index_status(tmp_path, QID) == "shelved"     # falsifiable: stale before
    assert _user_version(tmp_path) == 7

    assert archive_read.status_of(tmp_path, QID) == "archived"

    assert _record_status(tmp_path, QID) == "archived"
    assert _index_status(tmp_path, QID) == "archived"
    assert _marker(tmp_path) == "15"
    assert _user_version(tmp_path) == index._SCHEMA_VERSION == 8


def test_ensure_current_alone_runs_the_hop(tmp_path: Path) -> None:
    _seed_v14_archive(tmp_path)

    rows = archive_read.completed_rows(tmp_path)     # index-only: no check_data_floor

    assert [(r[0], r[1]) for r in rows] == [(QID, "archived")]
    assert _marker(tmp_path) == "15"


def test_first_imp_list_archived_lists_the_migrated_quest(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_v14_archive(tmp_path)

    out = _run(tmp_path, monkeypatch, ["list", "--archived"])

    assert any(ln.startswith(QID) and "archived" in ln for ln in out.splitlines())
    assert _marker(tmp_path) == "15"


def test_first_imp_search_archived_finds_the_migrated_quest(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_v14_archive(tmp_path)

    out = _run(tmp_path, monkeypatch, ["search", "--archived", "--list", "dark"])

    assert QID in out
    assert _marker(tmp_path) == "15"
    assert _index_status(tmp_path, QID) == "archived"


def test_the_migrated_conversation_still_resumes(tmp_path: Path) -> None:
    _seed_v14_archive(tmp_path)

    loaded = m._rehydrate_resumable(tmp_path, QID)

    assert loaded is not None
    status = State.load(loaded.state_dir).status
    assert status == "archived"
    assert status in m.RESUMABLE


def test_the_same_pass_rewrites_a_shelved_live_home(tmp_path: Path) -> None:
    _seed_v14_archive(tmp_path)
    stranded = _live_home_with_status(tmp_path, "strd01", "shelved")
    paused = _live_home_with_status(tmp_path, "paus01", "paused")
    before = json.loads(stranded.read_text())
    paused_bytes = paused.read_bytes()

    archive_read.status_of(tmp_path, QID)

    after = json.loads(stranded.read_text())
    assert after == {**before, "status": "archived"}   # nothing else in the home moves
    assert stranded.read_bytes() == json.dumps(
        {**before, "status": "archived"}, indent=2, sort_keys=True).encode("utf-8")
    assert paused.read_bytes() == paused_bytes         # a non-shelved home is untouched


def test_an_unreadable_live_home_does_not_block_the_hop(tmp_path: Path) -> None:
    _seed_v14_archive(tmp_path)
    broken = _live_home_with_status(tmp_path, "brok01", "paused")
    broken.write_bytes(b"{not json")
    stranded = _live_home_with_status(tmp_path, "strd01", "shelved")
    assert "brok01" in live_quest_ids(tmp_path)

    assert archive_read.status_of(tmp_path, QID) == "archived"

    assert _marker(tmp_path) == "15"
    assert broken.read_bytes() == b"{not json"
    assert json.loads(stranded.read_text())["status"] == "archived"


def test_a_crash_mid_hop_leaves_the_marker_and_retries(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_v14_archive(tmp_path)
    real_rebuild = archive.rebuild_index

    def boom(_root: Path) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(archive, "rebuild_index", boom)
    with pytest.raises(RuntimeError):
        archive_read.status_of(tmp_path, QID)
    assert _marker(tmp_path) == "14"

    monkeypatch.setattr(archive, "rebuild_index", real_rebuild)  # undo() would drop IMP_HOME
    assert archive_read.status_of(tmp_path, QID) == "archived"
    assert _marker(tmp_path) == "15"
    assert _index_status(tmp_path, QID) == "archived"


def test_a_second_pass_does_no_work(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_v14_archive(tmp_path)
    archive_read.status_of(tmp_path, QID)
    record_path = archive.records_root(tmp_path) / f"{QID}.json"
    first = record_path.read_bytes()

    calls: list[int] = []
    monkeypatch.setattr(archive, "rebuild_index", lambda _root: calls.append(1))
    archive_read.status_of(tmp_path, QID)
    archive_read.completed_rows(tmp_path)

    assert calls == []                               # marker current -> fast path
    assert record_path.read_bytes() == first


def test_a_read_only_archive_is_left_untouched(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_v14_archive(tmp_path)
    # The runner may be root, where mode bits deny nothing: deny the probe itself.
    real_access = os.access
    target = str(archive.archive_root(tmp_path))
    monkeypatch.setattr(
        archive.os, "access",
        lambda p, mode: False if (mode == os.W_OK and str(p) == target)
        else real_access(p, mode))

    with pytest.raises(ArchiveMismatch) as excinfo:
        archive_read.status_of(tmp_path, QID)
    archive_read.completed_rows(tmp_path)

    assert (excinfo.value.floor, excinfo.value.stored) == ("data", 14)
    assert _marker(tmp_path) == "14"
    assert _record_status(tmp_path, QID) == "shelved"


def test_any_other_below_floor_version_still_raises(tmp_path: Path) -> None:
    _seed_v14_archive(tmp_path)
    archive._write_data_version(tmp_path, 13)

    with pytest.raises(ArchiveMismatch) as excinfo:
        archive_read.status_of(tmp_path, QID)

    assert (excinfo.value.floor, excinfo.value.stored) == ("data", 13)
    assert _marker(tmp_path) == "13"
    assert _record_status(tmp_path, QID) == "shelved"


def test_no_layout_bump_and_no_reader_accepts_shelved() -> None:
    assert LAYOUT_VERSION == 23
    assert "shelved" not in get_args(Status)
    assert "shelved" not in _LEGACY_STATUS


def test_the_commits_backfill_hop_is_retired() -> None:
    for name in ("_backfill_commits", "_commits_of", "_serialize_events"):
        assert not hasattr(archive, name), name
    assert not (Path(__file__).parent / "test_archive_commits_migration.py").exists()
