"""Read-side downgrade guard: an archive floor newer than this install refuses to be read."""
from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from imp.engine import archive
from imp.engine.archive import index, read
from imp.engine.errors import ArchiveMismatch

ARCH = "arqu99"

# Prose term -> the `content` column (conversation_prose); GREPTARGET -> `events_content`.
_EVENTS = (
    json.dumps({"id": "e0", "timestamp": "2026-06-01T00:00:01+00:00",
                "type": "user_message",
                "payload": {"role": "user", "subtype": "intent",
                            "content": [{"type": "text", "text": "flywheel"}]}}) + "\n"
    + json.dumps({"id": "e1", "timestamp": "2026-06-01T00:00:02+00:00",
                  "type": "tool_call",
                  "payload": {"name": "bash", "input": {"cmd": "GREPTARGET"}}}) + "\n"
)


def _seed_archived_quest(control: Path) -> None:
    """Archive one merged quest so records/, blobs/, index.db and the marker exist."""
    events = control / "events.jsonl"
    events.write_text(_EVENTS)
    quest_json = control / "quest.json"
    quest_json.write_text(json.dumps({
        "id": ARCH, "title": "Archived Thing", "base_branch": "main",
        "created_at": "2026-06-01T00:00:00+00:00", "depends_on": []}))
    state_json = control / "state.json"
    state_json.write_text(json.dumps({
        "id": ARCH, "status": "merged", "step": "finalize"}))
    archive.archive_quest(
        control, ARCH, events_path=events,
        quest_json_path=quest_json, state_json_path=state_json)


def _raise_data_floor(control: Path) -> int:
    """Stamp the blob floor one ahead of this install — a newer imp's archive."""
    ahead = archive._DATA_VERSION + 1
    archive.data_version_path(control).write_text(str(ahead))
    return ahead


def _raise_index_floor(control: Path) -> int:
    ahead = index._SCHEMA_VERSION + 1
    conn = sqlite3.connect(archive.index_path(control))
    try:
        conn.execute(f"PRAGMA user_version = {ahead}")
        conn.commit()
    finally:
        conn.close()
    return ahead


# Index-only reads: they open index.db and run a query, touching no record or blob.
_INDEX_ENTRY_POINTS: list[tuple[str, Callable[[Path], Any]]] = [
    ("completed_rows", read.completed_rows),
    ("search_archive_content",
     lambda c: read.search_archive_content(c, "flywheel")),
    ("search_archive_events_content",
     lambda c: read.search_archive_events_content(c, "GREPTARGET")),
]
_INDEX_IDS = [name for name, _ in _INDEX_ENTRY_POINTS]
_INDEX_CALLS = [fn for _, fn in _INDEX_ENTRY_POINTS]

# Record/blob reads: _migrate_archive_blobs rewrites the records these read, so the
# data floor is the right gate for all five.
_RECORD_ENTRY_POINTS: list[tuple[str, Callable[[Path], Any]]] = [
    ("read_events", lambda c: read.read_events(c, ARCH)),
    ("read_quest_json", lambda c: read.read_quest_json(c, ARCH)),
    ("read_state_json", lambda c: read.read_state_json(c, ARCH)),
    ("read_step_blobs", lambda c: read.read_step_blobs(c, ARCH)),
    ("merge_commit_of", lambda c: read.merge_commit_of(c, ARCH)),
]
_RECORD_IDS = [name for name, _ in _RECORD_ENTRY_POINTS]
_RECORD_CALLS = [fn for _, fn in _RECORD_ENTRY_POINTS]


@pytest.mark.parametrize("call", _RECORD_CALLS, ids=_RECORD_IDS)
def test_a_record_read_refuses_an_ahead_data_floor(
    tmp_path: Path, call: Callable[[Path], Any],
) -> None:
    _seed_archived_quest(tmp_path)
    ahead = _raise_data_floor(tmp_path)

    with pytest.raises(ArchiveMismatch) as excinfo:
        call(tmp_path)

    exc = excinfo.value
    assert (exc.floor, exc.stored, exc.known) == ("data", ahead, archive._DATA_VERSION)


@pytest.mark.parametrize("call", _INDEX_CALLS, ids=_INDEX_IDS)
def test_an_index_read_refuses_an_ahead_index_floor(
    tmp_path: Path, call: Callable[[Path], Any],
) -> None:
    _seed_archived_quest(tmp_path)
    ahead = _raise_index_floor(tmp_path)

    with pytest.raises(ArchiveMismatch) as excinfo:
        call(tmp_path)

    exc = excinfo.value
    assert (exc.floor, exc.stored, exc.known) == (
        "index", ahead, index._SCHEMA_VERSION)


@pytest.mark.parametrize("call", _INDEX_CALLS, ids=_INDEX_IDS)
def test_an_index_read_ignores_an_ahead_data_floor(
    tmp_path: Path, call: Callable[[Path], Any],
) -> None:
    """An index-only query touches no blob, so a newer blob schema cannot affect it."""
    _seed_archived_quest(tmp_path)
    _raise_data_floor(tmp_path)

    assert [row[0] for row in call(tmp_path)] == [ARCH]


@pytest.mark.parametrize("call", _INDEX_CALLS, ids=_INDEX_IDS)
def test_equal_floors_still_read(
    tmp_path: Path, call: Callable[[Path], Any],
) -> None:
    _seed_archived_quest(tmp_path)

    assert [row[0] for row in call(tmp_path)] == [ARCH]


def test_lower_data_floor_migrates_instead_of_raising(tmp_path: Path) -> None:
    """A floor *behind* this install is the ordinary migrate-up case, not a refusal."""
    _seed_archived_quest(tmp_path)
    archive.data_version_path(tmp_path).write_text("1")

    rows = read.completed_rows(tmp_path)

    assert [row[0] for row in rows] == [ARCH]
    assert (archive.data_version_path(tmp_path).read_text().strip()
            == str(archive._DATA_VERSION))


def test_both_floors_ahead_report_the_floor_each_read_touches(tmp_path: Path) -> None:
    """With both floors ahead, the reported floor names what the read actually opened."""
    _seed_archived_quest(tmp_path)
    data_ahead = _raise_data_floor(tmp_path)
    index_ahead = _raise_index_floor(tmp_path)

    with pytest.raises(ArchiveMismatch) as index_exc:
        read.completed_rows(tmp_path)
    with pytest.raises(ArchiveMismatch) as data_exc:
        read.read_events(tmp_path, ARCH)

    assert (index_exc.value.floor, index_exc.value.stored) == ("index", index_ahead)
    assert (data_exc.value.floor, data_exc.value.stored) == ("data", data_ahead)
