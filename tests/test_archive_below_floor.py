"""Below the blob floor: an archive an older imp wrote refuses reads and writes."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from imp.engine import archive
from imp.engine.archive import record, records_root
from imp.engine.errors import ArchiveMismatch

ARCH = "arqu99"


def _triad(control: Path, quest_id: str) -> tuple[Path, Path, Path]:
    events = control / f"{quest_id}.jsonl"
    events.write_text("")
    quest_json = control / f"{quest_id}-quest.json"
    quest_json.write_text(json.dumps({
        "id": quest_id, "title": "Archived Thing", "base_branch": "main",
        "created_at": "2026-06-01T00:00:00+00:00", "depends_on": []}))
    state_json = control / f"{quest_id}-state.json"
    state_json.write_text(json.dumps({
        "id": quest_id, "status": "merged", "step": "finalize"}))
    return events, quest_json, state_json


def _archive_one(control: Path, quest_id: str) -> None:
    events, quest_json, state_json = _triad(control, quest_id)
    archive.archive_quest(
        control, quest_id, events_path=events,
        quest_json_path=quest_json, state_json_path=state_json)


def test_archive_quest_refuses_an_archive_with_no_data_version_marker(
    tmp_path: Path,
) -> None:
    _archive_one(tmp_path, ARCH)
    marker = archive.data_version_path(tmp_path)
    marker.unlink()                  # records but no marker: _read_data_version -> 0
    events, quest_json, state_json = _triad(tmp_path, "newqu1")

    with pytest.raises(ArchiveMismatch) as excinfo:
        archive.archive_quest(
            tmp_path, "newqu1", events_path=events,
            quest_json_path=quest_json, state_json_path=state_json)

    exc = excinfo.value
    assert (exc.floor, exc.stored, exc.known) == ("data", 0, archive._DATA_VERSION)
    assert not marker.exists()       # the stale archive is never stamped current
    assert record.read_record(records_root(tmp_path), "newqu1") is None


def test_archive_quest_into_an_empty_archive_stamps_the_marker(tmp_path: Path) -> None:
    assert not archive.archive_root(tmp_path).exists()

    _archive_one(tmp_path, ARCH)

    assert archive._DATA_VERSION == 15
    assert archive.data_version_path(tmp_path).read_text().strip() == "15"
    assert record.read_record(records_root(tmp_path), ARCH) is not None


def test_archive_is_current_reads_the_marker_of_a_bare_directory(
    tmp_path: Path,
) -> None:
    _archive_one(tmp_path, ARCH)
    adir = archive.archive_root(tmp_path)
    assert archive.archive_is_current(adir)

    archive.data_version_path(tmp_path).write_text("1\n")
    assert not archive.archive_is_current(adir)

    archive.data_version_path(tmp_path).unlink()
    assert not archive.archive_is_current(adir)
    assert not archive.archive_is_current(tmp_path / "no-such-archive")


def test_require_current_refuses_a_below_floor_archive(tmp_path: Path) -> None:
    _archive_one(tmp_path, ARCH)
    archive.data_version_path(tmp_path).write_text("1")

    with pytest.raises(ArchiveMismatch) as excinfo:
        archive.require_current(tmp_path)

    assert (excinfo.value.floor, excinfo.value.stored) == ("data", 1)
