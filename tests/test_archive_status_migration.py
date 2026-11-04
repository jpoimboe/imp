"""The one-shot 15 -> 16 upgrade: the stored statuses `merged` and `done` become
`completed`."""
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
from imp.engine.state import _LEGACY_STATUS, Status
from tests.conftest import BELOW_DATA_FLOOR, _seed_control

QM = "mrgd01"   # stored `merged`, with a merge commit
QD = "done01"   # stored `done`, no merge commit
QA = "arch01"   # stored `archived`: the hop must not touch it


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


def _archive_finished(control: Path, qid: str, title: str, **state: object) -> None:
    src = control / "seed" / qid
    src.mkdir(parents=True)
    (src / "events.jsonl").write_text("")
    (src / "quest.json").write_text(json.dumps({
        "id": qid, "title": title, "base_branch": "main",
        "created_at": "2026-06-01T00:00:00+00:00", "depends_on": []}))
    (src / "state.json").write_text(json.dumps(
        {"id": qid, "status": "completed", "step": "finalize", **state}))
    archive.archive_quest(
        control, qid, events_path=src / "events.jsonl",
        quest_json_path=src / "quest.json", state_json_path=src / "state.json")


def _seed_v15_archive(control: Path) -> None:
    """Real finished quests, rolled back to exactly how a v15 install left them."""
    _seed_control(control)
    _archive_finished(control, QM, "Dark mode merge", merge_commit="c0ffee1")
    _archive_finished(control, QD, "Dark mode plain")
    archive_conversation(Quest.create_draft(
        control, title="Dark mode chat", quest_id=QA, base_branch="main"))
    records = archive.records_root(control)
    for qid, legacy in ((QM, "merged"), (QD, "done")):
        rec = record.read_record(records, qid)
        assert rec is not None
        rec["final_state"]["status"] = legacy
        record.write_record(records, rec)
    archive.rebuild_index(control)
    conn = sqlite3.connect(archive.index_path(control))
    try:
        conn.execute("PRAGMA user_version = 8")     # the index floor v15 shipped
        conn.commit()
    finally:
        conn.close()
    archive._write_data_version(control, 15)


def _live_home_with_status(
        control: Path, quest_id: str, status: str, **extra: object) -> Path:
    q = Quest.create_draft(control, title=quest_id, quest_id=quest_id, base_branch="main")
    path = q.state_dir / "state.json"
    data = json.loads(path.read_text())
    data.update(status=status, **extra)
    path.write_text(json.dumps(data, indent=2, sort_keys=True))
    return path


def _healed(before: dict[str, object]) -> bytes:
    return json.dumps(
        {**before, "status": "completed"}, indent=2, sort_keys=True).encode("utf-8")


def _run(control: Path, monkeypatch: pytest.MonkeyPatch, argv: list[str]) -> str:
    monkeypatch.setattr(m, "_control_root", lambda: control)
    monkeypatch.setenv("NO_COLOR", "1")
    buf = io.StringIO()
    with redirect_stdout(buf):
        assert m.main(argv) == 0
    return buf.getvalue()


def test_a_blob_backed_read_migrates_records_and_index(tmp_path: Path) -> None:
    _seed_v15_archive(tmp_path)
    archived = archive.records_root(tmp_path) / f"{QA}.json"
    archived_bytes = archived.read_bytes()
    assert _index_status(tmp_path, QM) == "merged"      # falsifiable: stale before
    assert _index_status(tmp_path, QD) == "done"
    assert _user_version(tmp_path) == 8

    assert archive_read.status_of(tmp_path, QM) == "completed"

    for qid in (QM, QD):
        assert _record_status(tmp_path, qid) == "completed"
        assert _index_status(tmp_path, qid) == "completed"
    assert archive_read.merge_commit_of(tmp_path, QM) == "c0ffee1"
    assert archive_read.merge_commit_of(tmp_path, QD) is None
    assert archived.read_bytes() == archived_bytes     # a non-legacy record is untouched
    assert _marker(tmp_path) == "16"
    assert _user_version(tmp_path) == index._SCHEMA_VERSION == 9


def test_ensure_current_alone_runs_the_hop(tmp_path: Path) -> None:
    _seed_v15_archive(tmp_path)

    rows = archive_read.completed_rows(tmp_path)     # index-only: no check_data_floor

    assert sorted((r[0], r[1]) for r in rows) == sorted(
        [(QM, "completed"), (QD, "completed"), (QA, "archived")])
    assert _marker(tmp_path) == "16"


def test_first_imp_list_completed_lists_the_migrated_quests(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_v15_archive(tmp_path)

    out = _run(tmp_path, monkeypatch, ["list", "--completed"])

    lines = out.splitlines()
    for qid in (QM, QD):
        assert any(ln.startswith(qid) and "completed" in ln for ln in lines), qid
    assert QA not in out
    assert _marker(tmp_path) == "16"


def test_first_imp_search_completed_finds_the_migrated_quests(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_v15_archive(tmp_path)

    out = _run(tmp_path, monkeypatch, ["search", "--completed", "--list", "dark"])

    assert QM in out and QD in out
    assert QA not in out
    assert _marker(tmp_path) == "16"
    assert _index_status(tmp_path, QD) == "completed"


def test_the_same_pass_rewrites_merged_and_done_live_homes(tmp_path: Path) -> None:
    _seed_v15_archive(tmp_path)
    merged = _live_home_with_status(tmp_path, "strm01", "merged", merge_commit="c0ffee2")
    done = _live_home_with_status(tmp_path, "strd01", "done")
    paused = _live_home_with_status(tmp_path, "paus01", "paused")
    befores = {p: json.loads(p.read_text()) for p in (merged, done)}
    paused_bytes = paused.read_bytes()

    archive_read.status_of(tmp_path, QM)

    for path, before in befores.items():
        assert path.read_bytes() == _healed(before)   # nothing else in the home moves
    assert json.loads(merged.read_text())["merge_commit"] == "c0ffee2"
    assert paused.read_bytes() == paused_bytes         # a non-legacy home is untouched


def test_heal_live_status_rewrites_only_the_retired_words(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    done = _live_home_with_status(tmp_path, "strd01", "done")
    paused = _live_home_with_status(tmp_path, "paus01", "paused")
    completed = _live_home_with_status(tmp_path, "cmpl01", "completed")
    broken = _live_home_with_status(tmp_path, "brok01", "paused")
    broken.write_bytes(b"{not json")
    before = json.loads(done.read_text())
    keep = {p: p.read_bytes() for p in (paused, completed, broken)}

    healed = archive.heal_live_status(done)

    assert healed == done.read_bytes() == _healed(before)
    for path, raw in keep.items():
        assert archive.heal_live_status(path) is None
        assert path.read_bytes() == raw


def test_an_unreadable_live_home_does_not_block_the_hop(tmp_path: Path) -> None:
    _seed_v15_archive(tmp_path)
    broken = _live_home_with_status(tmp_path, "brok01", "paused")
    broken.write_bytes(b"{not json")
    stranded = _live_home_with_status(tmp_path, "strd01", "done")
    assert "brok01" in live_quest_ids(tmp_path)

    assert archive_read.status_of(tmp_path, QD) == "completed"

    assert _marker(tmp_path) == "16"
    assert broken.read_bytes() == b"{not json"
    assert json.loads(stranded.read_text())["status"] == "completed"


def test_a_crash_after_the_rebuild_leaves_the_marker_and_retries(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_v15_archive(tmp_path)
    real_rebuild = archive.rebuild_index

    def rebuilt_then_crash(root: Path) -> None:
        real_rebuild(root)
        raise RuntimeError("boom")

    monkeypatch.setattr(archive, "rebuild_index", rebuilt_then_crash)
    with pytest.raises(RuntimeError):
        archive_read.status_of(tmp_path, QM)
    assert _marker(tmp_path) == "15"
    assert archive._read_data_version(tmp_path) == 15   # never read as v0

    monkeypatch.setattr(archive, "rebuild_index", real_rebuild)  # undo() would drop IMP_HOME
    assert archive_read.status_of(tmp_path, QM) == "completed"
    assert _marker(tmp_path) == "16"
    assert _index_status(tmp_path, QM) == "completed"


def test_a_second_pass_does_no_work(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_v15_archive(tmp_path)
    archive_read.status_of(tmp_path, QM)
    record_path = archive.records_root(tmp_path) / f"{QM}.json"
    first = record_path.read_bytes()

    calls: list[int] = []
    monkeypatch.setattr(archive, "rebuild_index", lambda _root: calls.append(1))
    archive_read.status_of(tmp_path, QM)
    archive_read.completed_rows(tmp_path)

    assert calls == []                               # marker current -> fast path
    assert record_path.read_bytes() == first


def test_a_read_only_archive_is_left_untouched(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_v15_archive(tmp_path)
    # The runner may be root, where mode bits deny nothing: deny the probe itself.
    real_access = os.access
    target = str(archive.archive_root(tmp_path))
    monkeypatch.setattr(
        archive.os, "access",
        lambda p, mode: False if (mode == os.W_OK and str(p) == target)
        else real_access(p, mode))

    with pytest.raises(ArchiveMismatch) as excinfo:
        archive_read.status_of(tmp_path, QM)
    archive_read.completed_rows(tmp_path)

    assert (excinfo.value.floor, excinfo.value.stored) == ("data", 15)
    assert _marker(tmp_path) == "15"
    assert _record_status(tmp_path, QM) == "merged"


def test_any_other_below_floor_version_still_raises(tmp_path: Path) -> None:
    _seed_v15_archive(tmp_path)
    archive._write_data_version(tmp_path, BELOW_DATA_FLOOR)

    with pytest.raises(ArchiveMismatch) as excinfo:
        archive_read.status_of(tmp_path, QM)

    assert (excinfo.value.floor, excinfo.value.stored) == ("data", BELOW_DATA_FLOOR)
    assert _marker(tmp_path) == str(BELOW_DATA_FLOOR)
    assert _record_status(tmp_path, QM) == "merged"


def test_no_layout_bump_and_no_reader_accepts_merged_or_done() -> None:
    assert LAYOUT_VERSION == 23
    for word in ("merged", "done"):
        assert word not in get_args(Status), word
        assert word not in _LEGACY_STATUS, word


def test_the_earlier_hops_are_retired() -> None:
    for name in ("_backfill_commits", "_commits_of", "_serialize_events",
                 "_rename_live_status"):
        assert not hasattr(archive, name), name
    assert not (Path(__file__).parent / "test_archive_commits_migration.py").exists()
