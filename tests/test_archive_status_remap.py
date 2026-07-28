from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from imp.engine import archive
from imp.engine.archive import record
from imp.engine.archive.blobstore import LocalBlobStore

_TRIAD = [
    {"id": "t0", "parent_id": None, "agent_id": None,
     "timestamp": "2026-06-02T00:00:00+00:00",
     "type": "quest_created", "payload": {"id": "x", "title": ""}},
    {"id": "t1", "parent_id": None, "agent_id": "A",
     "timestamp": "2026-06-02T03:00:00+00:00",
     "type": "user_message",
     "payload": {"role": "user", "subtype": "intent",
                 "content": [{"type": "text", "text": "hi"}]}},
]


def _jsonl(events: list[dict]) -> bytes:
    return "".join(json.dumps(e) + "\n" for e in events).encode("utf-8")


def _seed_pre_migration_record(control: Path, cid: str, status: str) -> None:
    """A record with the given pre-migration final_state.status and NO data-version
    marker (so ensure_current runs the migration pass)."""
    store = LocalBlobStore(archive.blobs_root(control))
    events_hash = store.put(_jsonl(_TRIAD))
    rec = record.build_record(
        {"id": cid, "title": "", "base_branch": "main",
         "created_at": "2026-06-01T00:00:00+00:00", "depends_on": []},
        {"id": cid, "status": status, "step": "finalize"},
        events_hash)
    record.write_record(archive.records_root(control), rec)
    archive.rebuild_index(control)


def _final_status(control: Path, cid: str) -> str:
    rec = record.read_record(archive.records_root(control), cid)
    assert rec is not None
    return rec["final_state"]["status"]


def _index_status(control: Path, cid: str) -> str:
    conn = sqlite3.connect(archive.index_path(control))
    try:
        return conn.execute(
            "SELECT status FROM quests WHERE id = ?", (cid,)).fetchone()[0]
    finally:
        conn.close()


def test_completed_record_remaps_to_merged(tmp_path: Path) -> None:
    _seed_pre_migration_record(tmp_path, "rm-c", "completed")
    archive.ensure_current(tmp_path)
    assert _final_status(tmp_path, "rm-c") == "merged"
    assert _index_status(tmp_path, "rm-c") == "merged"


def test_paused_record_remaps_to_shelved(tmp_path: Path) -> None:
    _seed_pre_migration_record(tmp_path, "rm-p", "paused")
    archive.ensure_current(tmp_path)
    assert _final_status(tmp_path, "rm-p") == "shelved"
    assert _index_status(tmp_path, "rm-p") == "shelved"


def test_marker_reads_current_after_migration(tmp_path: Path) -> None:
    _seed_pre_migration_record(tmp_path, "rm-m", "completed")
    archive.ensure_current(tmp_path)
    assert archive.data_version_path(tmp_path).read_text().strip() == str(archive._DATA_VERSION)


def test_remap_is_idempotent_on_already_merged(tmp_path: Path) -> None:
    _seed_pre_migration_record(tmp_path, "rm-i", "merged")
    archive.ensure_current(tmp_path)
    assert _final_status(tmp_path, "rm-i") == "merged"


def test_remap_is_idempotent_on_already_shelved(tmp_path: Path) -> None:
    _seed_pre_migration_record(tmp_path, "rm-s", "shelved")
    archive.ensure_current(tmp_path)
    assert _final_status(tmp_path, "rm-s") == "shelved"
