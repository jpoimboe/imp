from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from imp.engine import archive
from imp.engine.archive import index, read, record
from imp.engine.archive.blobstore import LocalBlobStore, hash_content


def _setup_change(control: Path) -> tuple[Path, Path, Path]:
    events = control / "events.jsonl"
    events.write_text(
        '{"id":"e0","parent_id":null,"agent_id":null,'
        '"timestamp":"2026-06-01T00:00:01+00:00","type":"quest_created",'
        '"payload":{"id":"alpha"}}\n'
        '{"id":"e1","parent_id":"e0","agent_id":"plan-writer",'
        '"timestamp":"2026-06-01T00:00:02+00:00","type":"step_started",'
        '"payload":{"step":"plan-writer"}}\n')
    quest_json = control / "quest.json"
    quest_json.write_text(json.dumps({
        "id": "alpha", "title": "Alpha", "base_branch": "main",
        "created_at": "2026-06-01T00:00:00+00:00", "depends_on": ["beta"]}))
    state_json = control / "state.json"
    state_json.write_text(json.dumps({
        "id": "alpha", "status": "completed", "step": "finalize",
        "merge_commit": "abc123"}))
    return events, quest_json, state_json


def test_archive_quest_writes_blob_record_and_index(tmp_path: Path) -> None:
    events, quest_json, state_json = _setup_change(tmp_path)
    expected_hash = hash_content(events.read_bytes())

    archive.archive_quest(
        tmp_path, "alpha", events_path=events,
        quest_json_path=quest_json, state_json_path=state_json)

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
            "SELECT status, events_hash FROM quests WHERE id='alpha'"
        ).fetchone() == ("completed", expected_hash)
    finally:
        conn.close()


def test_first_archive_is_current_when_its_record_is_published(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    events, quest_json, state_json = _setup_change(tmp_path)
    versions_at_publication: list[int] = []
    real_write = record.write_record

    def capture_version(records_dir: Path, rec: dict) -> Path:
        path = real_write(records_dir, rec)
        versions_at_publication.append(archive._read_data_version(tmp_path))
        return path

    monkeypatch.setattr(record, "write_record", capture_version)

    archive.archive_quest(
        tmp_path, "alpha", events_path=events,
        quest_json_path=quest_json, state_json_path=state_json)

    assert versions_at_publication == [archive._DATA_VERSION]


def test_ensure_current_recovers_first_record_when_index_ingest_crashes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    events, quest_json, state_json = _setup_change(tmp_path)

    def crash_ingest(_conn, _rec, _events) -> None:
        raise OSError("injected index crash")

    with monkeypatch.context() as crash:
        crash.setattr(index, "ingest", crash_ingest)
        with pytest.raises(OSError, match="injected index crash"):
            archive.archive_quest(
                tmp_path, "alpha", events_path=events,
                quest_json_path=quest_json, state_json_path=state_json)

    db = archive.index_path(tmp_path)
    conn = sqlite3.connect(db)
    try:
        incomplete = (
            db.exists(),
            int(conn.execute("PRAGMA user_version").fetchone()[0]),
            [row[0] for row in conn.execute("SELECT id FROM quests")],
        )
    finally:
        conn.close()
    assert incomplete == (True, index._SCHEMA_VERSION, [])

    archive.ensure_current(tmp_path)

    conn = index.connect(db)
    try:
        ids = [row[0] for row in conn.execute("SELECT id FROM quests")]
    finally:
        conn.close()
    assert ids == ["alpha"]


def test_ensure_current_rebuilds_physically_corrupt_index(tmp_path: Path) -> None:
    events, quest_json, state_json = _setup_change(tmp_path)
    archive.archive_quest(
        tmp_path, "alpha", events_path=events,
        quest_json_path=quest_json, state_json_path=state_json)
    db = archive.index_path(tmp_path)
    db.write_bytes(b"not a sqlite database")

    archive.ensure_current(tmp_path)

    conn = sqlite3.connect(db)
    try:
        rebuilt = (
            int(conn.execute("PRAGMA user_version").fetchone()[0]),
            conn.execute(
                "SELECT id, title, base, created_at, status, step, last_event_ts"
                " FROM quests").fetchone(),
            conn.execute(
                "SELECT quest_id, depends_on_id FROM deps").fetchall(),
        )
    finally:
        conn.close()
    assert rebuilt == (
        index._SCHEMA_VERSION,
        ("alpha", "Alpha", "main", "2026-06-01T00:00:00+00:00", "completed",
         "finalize", "2026-06-01T00:00:02+00:00"),
        [("alpha", "beta")],
    )


@pytest.mark.parametrize(
    "schema",
    [
        ("CREATE TABLE quests (id TEXT PRIMARY KEY)",),
        (index._SCHEMA[0],),
    ],
    ids=["quests-missing-columns", "deps-missing"],
)
def test_ensure_current_rebuilds_current_but_malformed_index(
    tmp_path: Path, schema: tuple[str, ...],
) -> None:
    events, quest_json, state_json = _setup_change(tmp_path)
    archive.archive_quest(
        tmp_path, "alpha", events_path=events,
        quest_json_path=quest_json, state_json_path=state_json)
    db = archive.index_path(tmp_path)
    db.unlink()
    conn = sqlite3.connect(db)
    try:
        for statement in schema:
            conn.execute(statement)
        conn.execute("INSERT INTO quests (id) VALUES ('alpha')")
        conn.execute(f"PRAGMA user_version = {index._SCHEMA_VERSION}")
        conn.commit()
    finally:
        conn.close()

    archive.ensure_current(tmp_path)

    completed = read.completed_rows(tmp_path)
    conn = sqlite3.connect(db)
    try:
        quest = conn.execute(
            "SELECT id, title, base, created_at, status, step, last_event_ts"
            " FROM quests").fetchone()
        deps = conn.execute(
            "SELECT quest_id, depends_on_id FROM deps").fetchall()
    finally:
        conn.close()
    assert (completed, quest, deps) == (
        [("alpha", "completed", "finalize", "2026-06-01T00:00:02+00:00",
          "Alpha", "2026-06-01T00:00:00+00:00", None)],
        ("alpha", "Alpha", "main", "2026-06-01T00:00:00+00:00", "completed",
         "finalize", "2026-06-01T00:00:02+00:00"),
        [("alpha", "beta")],
    )


def test_archive_quest_is_idempotent(tmp_path: Path) -> None:
    events, quest_json, state_json = _setup_change(tmp_path)
    for _ in range(2):
        archive.archive_quest(
            tmp_path, "alpha", events_path=events,
            quest_json_path=quest_json, state_json_path=state_json)

    blobs = [p for p in archive.blobs_root(tmp_path).rglob("*") if p.is_file()]
    assert len(blobs) == 1
    conn = index.connect(archive.index_path(tmp_path))
    try:
        assert conn.execute("SELECT COUNT(*) FROM quests").fetchone()[0] == 1
    finally:
        conn.close()


def test_parse_events_preserves_malformed_lines_for_stable_ordinals() -> None:
    from imp.engine.archive import _parse_events

    raw = (
        b'{"id":"e0","type":"quest_created","payload":{}}\n'
        b'NOT_JSON\n'
        b'{"id":"e2","type":"step_started","payload":{}}\n'
    )
    events = _parse_events(raw)
    assert len(events) == 3
    assert events[0]["id"] == "e0"
    assert events[1] == {"_malformed": True, "raw": "NOT_JSON"}
    assert events[2]["id"] == "e2"


def test_archive_quest_missing_events_file(tmp_path: Path) -> None:
    _, quest_json, state_json = _setup_change(tmp_path)
    missing_events = tmp_path / "no-events.jsonl"

    archive.archive_quest(
        tmp_path, "alpha", events_path=missing_events,
        quest_json_path=quest_json, state_json_path=state_json)

    conn = index.connect(archive.index_path(tmp_path))
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM quests WHERE id='alpha'"
        ).fetchone()[0] == 1
    finally:
        conn.close()


def test_archive_quest_records_resumable_and_full_meta(tmp_path: Path) -> None:
    events, quest_json, state_json = _setup_change(tmp_path)

    archive.archive_quest(
        tmp_path, "alpha", events_path=events,
        quest_json_path=quest_json, state_json_path=state_json,
        resumable=True)

    rec = record.read_record(archive.records_root(tmp_path), "alpha")
    assert rec is not None
    assert rec["resumable"] is True
    assert rec["quest_meta"]["id"] == "alpha"
    assert rec["quest_meta"]["base_branch"] == "main"


def test_archive_quest_defaults_not_resumable(tmp_path: Path) -> None:
    events, quest_json, state_json = _setup_change(tmp_path)
    archive.archive_quest(
        tmp_path, "alpha", events_path=events,
        quest_json_path=quest_json, state_json_path=state_json)
    rec = record.read_record(archive.records_root(tmp_path), "alpha")
    assert rec is not None and rec["resumable"] is False


def test_archive_quest_resumable_captures_steps_subtree(tmp_path: Path) -> None:
    events, quest_json, state_json = _setup_change(tmp_path)
    steps = tmp_path / "steps"
    (steps / "p" / "s").mkdir(parents=True)
    (steps / "p" / "s" / "transcript.jsonl").write_bytes(b'{"m":1}\n')
    (steps / "p" / "s" / "claude_session" / "x").mkdir(parents=True)
    (steps / "p" / "s" / "claude_session" / "x" / "sess.jsonl").write_bytes(b"SESSION")

    archive.archive_quest(
        tmp_path, "alpha", events_path=events,
        quest_json_path=quest_json, state_json_path=state_json,
        steps_dir=steps, resumable=True)

    rec = record.read_record(archive.records_root(tmp_path), "alpha")
    assert rec is not None
    manifest = rec["step_blobs"]
    assert set(manifest) == {
        "p/s/transcript.jsonl", "p/s/claude_session/x/sess.jsonl"}
    store = LocalBlobStore(archive.blobs_root(tmp_path))
    assert store.get(manifest["p/s/transcript.jsonl"]) == b'{"m":1}\n'
    assert store.get(manifest["p/s/claude_session/x/sess.jsonl"]) == b"SESSION"


def test_archive_quest_non_resumable_captures_no_steps(tmp_path: Path) -> None:
    events, quest_json, state_json = _setup_change(tmp_path)
    steps = tmp_path / "steps"
    (steps / "p" / "s").mkdir(parents=True)
    (steps / "p" / "s" / "transcript.jsonl").write_bytes(b'{"m":1}\n')

    archive.archive_quest(
        tmp_path, "alpha", events_path=events,
        quest_json_path=quest_json, state_json_path=state_json,
        steps_dir=steps, resumable=False)

    rec = record.read_record(archive.records_root(tmp_path), "alpha")
    assert rec is not None and rec["step_blobs"] == {}
    # Only the events blob is stored; no step blobs.
    blobs = [p for p in archive.blobs_root(tmp_path).rglob("*") if p.is_file()]
    assert len(blobs) == 1


def test_archive_quest_resumable_missing_steps_dir_captures_nothing(tmp_path: Path) -> None:
    events, quest_json, state_json = _setup_change(tmp_path)
    missing = tmp_path / "nonexistent-steps"

    archive.archive_quest(
        tmp_path, "alpha", events_path=events,
        quest_json_path=quest_json, state_json_path=state_json,
        steps_dir=missing, resumable=True)

    rec = record.read_record(archive.records_root(tmp_path), "alpha")
    assert rec is not None and rec["step_blobs"] == {}


def test_archive_quest_resumable_steps_idempotent(tmp_path: Path) -> None:
    events, quest_json, state_json = _setup_change(tmp_path)
    steps = tmp_path / "steps"
    (steps / "p" / "s").mkdir(parents=True)
    (steps / "p" / "s" / "transcript.jsonl").write_bytes(b'{"m":1}\n')

    for _ in range(2):
        archive.archive_quest(
            tmp_path, "alpha", events_path=events,
            quest_json_path=quest_json, state_json_path=state_json,
            steps_dir=steps, resumable=True)

    # events blob + one step blob, content-addressed dedup -> exactly 2 files.
    blobs = [p for p in archive.blobs_root(tmp_path).rglob("*") if p.is_file()]
    assert len(blobs) == 2
    conn = index.connect(archive.index_path(tmp_path))
    try:
        assert conn.execute("SELECT COUNT(*) FROM quests").fetchone()[0] == 1
    finally:
        conn.close()
