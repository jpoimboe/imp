"""`imp resume <token>` refuses when a matching archived candidate is unreadable,
naming it — while a quest with a live home on disk still resolves at any floor."""
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import anyio
import pytest

import imp.cli.main as m
from imp.cli.store import _quest_home
from imp.engine import archive
from imp.engine.abandon import archive_conversation
from imp.engine.archive import index as archive_index
from imp.engine.archive import read as archive_read
from imp.engine.archive import record, records_root
from imp.engine.errors import ArchiveMismatch
from imp.engine.quest import Quest
from tests.conftest import _seed_control

LIVE = "lodo40"
SHELVED = "lodo77"


def _live(control: Path, quest_id: str, title: str = "Live") -> None:
    Quest.create_draft(control, title=title, quest_id=quest_id,
                       base_branch="main", origin="idea")


def _shelve(control: Path, quest_id: str) -> None:
    archive_conversation(Quest.create_draft(
        control, title=f"Shelved {quest_id}", quest_id=quest_id,
        base_branch="main"))


def _raise_data_floor(control: Path) -> int:
    """Stamp the blob floor one ahead — a newer imp's archive."""
    ahead = archive._DATA_VERSION + 1
    archive.data_version_path(control).write_text(str(ahead))
    return ahead


def _data_reason(stored: int) -> str:
    return str(ArchiveMismatch(floor="data", stored=stored,
                               known=archive._DATA_VERSION))


def _resolve(control: Path, token: str) -> str | int:
    async def run() -> str | int:
        return await m._resolve_resume_target(control, token, non_interactive=True)
    return anyio.run(run)


def test_zero_readable_matches_refuses_without_a_hint_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    """AC 5: today this token resolves and the floor refusal arrives later,
    naming no quest. It now refuses at resolution, naming the id."""
    _seed_control(tmp_path)
    _shelve(tmp_path, SHELVED)
    ahead = _raise_data_floor(tmp_path)

    rc = _resolve(tmp_path, SHELVED)

    captured = capsys.readouterr()
    assert rc == 2
    assert captured.err == (
        f"imp: {SHELVED!r} matches an archived quest this imp cannot read: "
        f"{SHELVED}\n"
        f"  {_data_reason(ahead)}\n")
    assert captured.out == ""


def test_a_mid_loop_row_failure_refuses_with_its_own_floor_text(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC 6: both floors current, the floor moves mid-loop — the candidate is
    reported, not skipped."""
    _seed_control(tmp_path)
    _live(tmp_path, LIVE)
    _shelve(tmp_path, SHELVED)
    archive.require_current(tmp_path)         # guard: both floors are current
    archive.check_index_floor(tmp_path)
    race = ArchiveMismatch(floor="data", stored=archive._DATA_VERSION + 1,
                           known=archive._DATA_VERSION)

    def boom(control: Path, quest_id: str) -> None:
        raise race

    monkeypatch.setattr("imp.cli.store._row_for_id", boom)

    rc = _resolve(tmp_path, "lodo")

    captured = capsys.readouterr()
    assert rc == 2
    assert captured.err == (
        f"imp: 'lodo' matches an archived quest this imp cannot read: {SHELVED}\n"
        f"  {race}\n"
        f"  hint: readable match {LIVE} — pass its full id to resume it.\n")


def test_an_ahead_index_floor_refuses_with_the_index_floor_text(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    """AC 7: an ahead index floor makes the candidate unrenderable too (via
    _journal_last_iso), so it refuses identically."""
    _seed_control(tmp_path)
    _live(tmp_path, LIVE)
    _shelve(tmp_path, SHELVED)
    ahead = archive_index._SCHEMA_VERSION + 1
    conn = sqlite3.connect(archive.index_path(tmp_path))
    try:
        conn.execute(f"PRAGMA user_version = {ahead}")
        conn.commit()
    finally:
        conn.close()

    rc = _resolve(tmp_path, "lodo")

    captured = capsys.readouterr()
    reason = str(ArchiveMismatch(floor="index", stored=ahead,
                                 known=archive_index._SCHEMA_VERSION))
    assert rc == 2
    assert captured.err == (
        f"imp: 'lodo' matches an archived quest this imp cannot read: {SHELVED}\n"
        f"  {reason}\n"
        f"  hint: readable match {LIVE} — pass its full id to resume it.\n")


def test_two_unreadable_candidates_render_the_plural_head_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    # AC 8(a)
    _seed_control(tmp_path)
    _live(tmp_path, LIVE)
    _shelve(tmp_path, SHELVED)
    _shelve(tmp_path, "lodo88")
    ahead = _raise_data_floor(tmp_path)

    rc = _resolve(tmp_path, "lodo")

    captured = capsys.readouterr()
    assert rc == 2
    assert captured.err == (
        "imp: 'lodo' matches archived quests this imp cannot read: "
        "lodo77, lodo88\n"
        f"  {_data_reason(ahead)}\n"
        f"  hint: readable match {LIVE} — pass its full id to resume it.\n")


def test_two_readable_matches_render_the_plural_hint_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    # AC 8(b)
    _seed_control(tmp_path)
    _live(tmp_path, LIVE)
    _live(tmp_path, "lodo41", title="Live two")
    _shelve(tmp_path, SHELVED)
    ahead = _raise_data_floor(tmp_path)

    rc = _resolve(tmp_path, "lodo")

    captured = capsys.readouterr()
    assert rc == 2
    assert captured.err == (
        f"imp: 'lodo' matches an archived quest this imp cannot read: {SHELVED}\n"
        f"  {_data_reason(ahead)}\n"
        "  hint: readable matches lodo40, lodo41 — pass a full id to resume one.\n")


def test_below_the_floor_with_no_index_still_names_the_record(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    """AC 9: the case that separates the two enumeration sources — an archive
    below the blob floor is never reindexed, so the records are the source."""
    _seed_control(tmp_path)
    _live(tmp_path, LIVE)
    _shelve(tmp_path, SHELVED)
    below = archive._DATA_VERSION - 1
    archive._write_data_version(tmp_path, below)
    archive.index_path(tmp_path).unlink()
    assert archive_read.archived_ids(tmp_path) == []                  # guard
    assert record.list_record_ids(records_root(tmp_path)) == [SHELVED]  # guard

    rc = _resolve(tmp_path, "lodo")

    captured = capsys.readouterr()
    assert rc == 2
    assert captured.err == (
        f"imp: 'lodo' matches an archived quest this imp cannot read: {SHELVED}\n"
        f"  {_data_reason(below)}\n"
        f"  hint: readable match {LIVE} — pass its full id to resume it.\n")


def test_a_live_locked_rehydrated_conversation_still_resolves(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    """AC 4(b): rehydrate_quest leaves the archive record in place and a
    live-locked quest displays `running`, so it is in neither set — yet it is
    perfectly readable, and both its full id and the partial token must resolve
    silently. The regression guard for the quest-home exclusion."""
    _seed_control(tmp_path)
    _live(tmp_path, LIVE)
    _shelve(tmp_path, SHELVED)
    archive_read.rehydrate_quest(tmp_path, SHELVED)
    state_path = _quest_home(tmp_path, SHELVED) / "state" / "state.json"
    state = json.loads(state_path.read_text())
    state["status"] = "paused"
    state_path.write_text(json.dumps(state))
    (_quest_home(tmp_path, SHELVED) / "lock").write_text(str(os.getpid()))
    _raise_data_floor(tmp_path)
    rec = record.read_record(records_root(tmp_path), SHELVED)
    assert rec is not None and rec["resumable"]      # guard: record outlives rehydrate
    assert SHELVED not in {r[0] for r in m._resumable_rows(tmp_path)}  # guard: `running`

    assert _resolve(tmp_path, SHELVED) == SHELVED
    assert _resolve(tmp_path, "lodo") == LIVE
    assert capsys.readouterr().err == ""
