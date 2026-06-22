from __future__ import annotations

import json
from pathlib import Path

from imp.engine import archive
from imp.engine.archive import index, record
from imp.engine.archive.blobstore import LocalBlobStore, hash_content


def _setup_change(control: Path) -> tuple[Path, Path, Path]:
    events = control / "events.jsonl"
    events.write_text(
        '{"id":"e0","parent_id":null,"agent_id":null,'
        '"timestamp":"2026-06-01T00:00:01+00:00","type":"change_created",'
        '"payload":{"id":"alpha"}}\n'
        '{"id":"e1","parent_id":"e0","agent_id":"plan-writer",'
        '"timestamp":"2026-06-01T00:00:02+00:00","type":"step_started",'
        '"payload":{"step":"plan-writer"}}\n')
    change_json = control / "change.json"
    change_json.write_text(json.dumps({
        "id": "alpha", "title": "Alpha", "base_branch": "main",
        "created_at": "2026-06-01T00:00:00+00:00", "depends_on": ["beta"]}))
    state_json = control / "state.json"
    state_json.write_text(json.dumps({
        "id": "alpha", "status": "completed", "step": "finalize",
        "merge_commit": "abc123"}))
    return events, change_json, state_json


def test_archive_change_writes_blob_record_and_index(tmp_path: Path) -> None:
    events, change_json, state_json = _setup_change(tmp_path)
    expected_hash = hash_content(events.read_bytes())

    archive.archive_change(
        tmp_path, "alpha", events_path=events,
        change_json_path=change_json, state_json_path=state_json)

    # Blob stored under the content hash, retrievable byte-for-byte.
    store = LocalBlobStore(archive.blobs_root(tmp_path))
    assert store.get(expected_hash) == events.read_bytes()

    # Record file carries the matching events_hash and reconciled fields.
    rec = record.read_record(archive.records_root(tmp_path), "alpha")
    assert rec is not None
    assert rec["events_hash"] == expected_hash
    assert rec["base"] == "main"
    assert rec["final_state"]["status"] == "completed"

    # Index has the change row + one row per event line.
    conn = index.connect(archive.index_path(tmp_path))
    try:
        assert conn.execute(
            "SELECT status, events_hash FROM changes WHERE id='alpha'"
        ).fetchone() == ("completed", expected_hash)
        assert conn.execute(
            "SELECT COUNT(*) FROM events WHERE change_id='alpha'"
        ).fetchone()[0] == 2
    finally:
        conn.close()


def test_archive_change_is_idempotent(tmp_path: Path) -> None:
    events, change_json, state_json = _setup_change(tmp_path)
    for _ in range(2):
        archive.archive_change(
            tmp_path, "alpha", events_path=events,
            change_json_path=change_json, state_json_path=state_json)

    blobs = [p for p in archive.blobs_root(tmp_path).rglob("*") if p.is_file()]
    assert len(blobs) == 1
    conn = index.connect(archive.index_path(tmp_path))
    try:
        assert conn.execute("SELECT COUNT(*) FROM changes").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 2
    finally:
        conn.close()


def test_parse_events_preserves_malformed_lines_for_stable_ordinals() -> None:
    from imp.engine.archive import _parse_events

    raw = (
        b'{"id":"e0","type":"change_created","payload":{}}\n'
        b'NOT_JSON\n'
        b'{"id":"e2","type":"step_started","payload":{}}\n'
    )
    events = _parse_events(raw)
    assert len(events) == 3
    assert events[0]["id"] == "e0"
    assert events[1] == {"_malformed": True, "raw": "NOT_JSON"}
    assert events[2]["id"] == "e2"


def test_archive_change_missing_events_file(tmp_path: Path) -> None:
    _, change_json, state_json = _setup_change(tmp_path)
    missing_events = tmp_path / "no-events.jsonl"

    archive.archive_change(
        tmp_path, "alpha", events_path=missing_events,
        change_json_path=change_json, state_json_path=state_json)

    conn = index.connect(archive.index_path(tmp_path))
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM changes WHERE id='alpha'"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM events WHERE change_id='alpha'"
        ).fetchone()[0] == 0
    finally:
        conn.close()


def test_archive_change_records_resumable_and_full_meta(tmp_path: Path) -> None:
    events, change_json, state_json = _setup_change(tmp_path)

    archive.archive_change(
        tmp_path, "alpha", events_path=events,
        change_json_path=change_json, state_json_path=state_json,
        resumable=True)

    rec = record.read_record(archive.records_root(tmp_path), "alpha")
    assert rec is not None
    assert rec["resumable"] is True
    assert rec["change_meta"]["id"] == "alpha"
    assert rec["change_meta"]["base_branch"] == "main"


def test_archive_change_defaults_not_resumable(tmp_path: Path) -> None:
    events, change_json, state_json = _setup_change(tmp_path)
    archive.archive_change(
        tmp_path, "alpha", events_path=events,
        change_json_path=change_json, state_json_path=state_json)
    rec = record.read_record(archive.records_root(tmp_path), "alpha")
    assert rec is not None and rec["resumable"] is False
