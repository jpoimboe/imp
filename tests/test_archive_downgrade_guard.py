"""Read-side floor guards: an archive floor this install cannot vouch for refuses the read."""
from __future__ import annotations

import json
import os
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


def _stale_index_floor(control: Path) -> None:
    """Stamp the index floor one behind: stale enough to earn a rebuild, not ahead
    enough to refuse a read."""
    behind = index._SCHEMA_VERSION - 1
    conn = sqlite3.connect(archive.index_path(control))
    try:
        conn.execute(f"PRAGMA user_version = {behind}")
        conn.commit()
    finally:
        conn.close()


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

# Record/blob reads: all five consume record and blob bytes this install cannot
# vouch for below the floor, so the data floor is the right gate.
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


def test_lower_data_floor_raises_instead_of_migrating(tmp_path: Path) -> None:
    """A floor *behind* this install refuses too: no migration can upgrade it."""
    _seed_archived_quest(tmp_path)
    archive.data_version_path(tmp_path).write_text("1")

    with pytest.raises(ArchiveMismatch) as excinfo:
        archive.check_data_floor(tmp_path)

    exc = excinfo.value
    assert (exc.floor, exc.stored, exc.known) == ("data", 1, archive._DATA_VERSION)
    assert [row[0] for row in read.completed_rows(tmp_path)] == [ARCH]
    assert archive.data_version_path(tmp_path).read_text().strip() == "1"


def test_ensure_current_leaves_a_below_floor_archive_alone(tmp_path: Path) -> None:
    """rebuild_index derives its columns from the blobs, so a below-floor archive is
    never reindexed — even with a deliberately stale index, which keeps answering."""
    _seed_archived_quest(tmp_path)
    _stale_index_floor(tmp_path)
    archive.data_version_path(tmp_path).write_text("1")
    before = archive.index_path(tmp_path).read_bytes()
    assert os.access(archive.archive_root(tmp_path), os.W_OK)

    archive.ensure_current(tmp_path)

    assert archive.index_path(tmp_path).read_bytes() == before
    assert [row[0] for row in read.completed_rows(tmp_path)] == [ARCH]


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
