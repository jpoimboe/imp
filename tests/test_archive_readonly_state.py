"""ensure_current on a read-only archive root: skip the rebuild, keep reading."""
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pytest

from imp.engine import archive
from imp.engine.archive import index, read
from imp.engine.errors import ArchiveMismatch

ARCH = "arqu99"


def _seed_archived_quest(control: Path) -> None:
    """Archive one merged quest so records/, blobs/, index.db, and the version marker exist."""
    events = control / "events.jsonl"
    events.write_text(json.dumps({
        "id": "e1", "timestamp": "2026-06-01T00:00:01+00:00",
        "type": "user_message",
        "payload": {"role": "user", "subtype": "intent",
                    "content": [{"type": "text", "text": "flywheel"}]}}) + "\n")
    quest_json = control / "quest.json"
    quest_json.write_text(json.dumps({
        "id": ARCH, "title": "Archived Thing", "base_branch": "main",
        "created_at": "2026-06-01T00:00:00+00:00", "depends_on": []}))
    state_json = control / "state.json"
    state_json.write_text(json.dumps({
        "id": ARCH, "status": "completed", "step": "finalize"}))
    archive.archive_quest(
        control, ARCH, events_path=events,
        quest_json_path=quest_json, state_json_path=state_json)


def _lock_file(control: Path) -> Path:
    return Path(str(archive.index_path(control)) + ".filelock")


def _clear_lock_file(control: Path) -> None:
    """Seeding took the writer lock; drop it so a re-appearance means ensure_current ran."""
    _lock_file(control).unlink(missing_ok=True)


def _user_version(control: Path) -> int:
    conn = sqlite3.connect(archive.index_path(control))
    try:
        return int(conn.execute("PRAGMA user_version").fetchone()[0])
    finally:
        conn.close()


def _stamp_user_version(control: Path, version: int) -> None:
    """Stamp user_version behind without touching the schema — the only shape the skip is valid."""
    conn = sqlite3.connect(archive.index_path(control))
    try:
        conn.execute(f"PRAGMA user_version = {version}")
        conn.commit()
    finally:
        conn.close()


# The runner may be root, where mode bits do not deny a write, so deny the probe itself.
def _deny_writes(monkeypatch: pytest.MonkeyPatch, target: Path) -> None:
    real_access = os.access
    monkeypatch.setattr(
        archive.os, "access",
        lambda p, m: False if (m == os.W_OK and str(p) == str(target)) else real_access(p, m))


def test_stale_index_stamp_on_a_read_only_archive_is_not_rebuilt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_archived_quest(tmp_path)
    _stamp_user_version(tmp_path, 4)
    _clear_lock_file(tmp_path)
    _deny_writes(monkeypatch, archive.archive_root(tmp_path))

    archive.ensure_current(tmp_path)

    assert _user_version(tmp_path) == 4          # no rebuild ran
    assert not _lock_file(tmp_path).exists()     # the writer lock was never taken
    assert [row[0] for row in read.completed_rows(tmp_path)] == [ARCH]


def test_missing_data_version_marker_on_a_read_only_archive_is_left_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_archived_quest(tmp_path)
    marker = archive.data_version_path(tmp_path)
    marker.unlink()                              # no marker: the data version reads as 0
    _clear_lock_file(tmp_path)
    _deny_writes(monkeypatch, archive.archive_root(tmp_path))

    archive.ensure_current(tmp_path)

    assert not marker.exists()                   # nothing stamped the marker
    assert not _lock_file(tmp_path).exists()
    assert [row[0] for row in read.completed_rows(tmp_path)] == [ARCH]


def test_a_writable_stale_index_stamp_is_rebuilt(tmp_path: Path) -> None:
    """The index half of the self-healing contract, gated on a current blob floor."""
    _seed_archived_quest(tmp_path)
    _stamp_user_version(tmp_path, 4)
    assert archive._read_data_version(tmp_path) == archive._DATA_VERSION

    archive.ensure_current(tmp_path)

    assert _user_version(tmp_path) == index._SCHEMA_VERSION   # rebuilt and re-stamped current


def test_a_newer_read_only_archive_still_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The floor check precedes ensure_current's writability early return."""
    _seed_archived_quest(tmp_path)
    ahead = index._SCHEMA_VERSION + 1
    _stamp_user_version(tmp_path, ahead)
    _clear_lock_file(tmp_path)
    _deny_writes(monkeypatch, archive.archive_root(tmp_path))

    with pytest.raises(ArchiveMismatch) as excinfo:
        read.completed_rows(tmp_path)

    assert (excinfo.value.floor, excinfo.value.stored) == ("index", ahead)
