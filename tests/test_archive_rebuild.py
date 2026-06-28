from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from imp.engine import archive
from imp.engine.archive import index


def _archive_one(control: Path, cid: str, dep: str, etype: str) -> None:
    events = control / f"{cid}-events.jsonl"
    events.write_text(
        f'{{"id":"{cid}-e0","parent_id":null,"agent_id":null,'
        f'"timestamp":"2026-06-01T00:00:01+00:00","type":"{etype}",'
        f'"payload":{{}}}}\n')
    change_json = control / f"{cid}-change.json"
    change_json.write_text(json.dumps({
        "id": cid, "title": cid.title(), "base_branch": "main",
        "created_at": "2026-06-01T00:00:00+00:00", "depends_on": [dep]}))
    state_json = control / f"{cid}-state.json"
    state_json.write_text(json.dumps({
        "id": cid, "status": "completed", "step": "finalize"}))
    archive.archive_change(
        control, cid, events_path=events,
        change_json_path=change_json, state_json_path=state_json)


def _snapshot(conn) -> dict:
    return {
        "changes": sorted(conn.execute(
            "SELECT id, title, base, created_at, status, step, events_hash,"
            " last_event_ts FROM changes").fetchall()),
        "deps": sorted(conn.execute(
            "SELECT change_id, depends_on_id FROM deps").fetchall()),
    }


def test_rebuild_reproduces_index_contents(tmp_path: Path) -> None:
    _archive_one(tmp_path, "alpha", "beta", "change_created")
    _archive_one(tmp_path, "gamma", "alpha", "change_merged")

    conn = index.connect(archive.index_path(tmp_path))
    try:
        before = _snapshot(conn)
    finally:
        conn.close()

    # Drop the derived index entirely, then rebuild from records + blobs.
    archive.index_path(tmp_path).unlink()
    archive.rebuild_index(tmp_path)

    conn = index.connect(archive.index_path(tmp_path))
    try:
        after = _snapshot(conn)
    finally:
        conn.close()

    assert after == before
    assert after["changes"]  # non-empty: the rebuild actually loaded rows
    assert all(row[7] for row in after["changes"])  # last_event_ts populated


def test_rebuild_keeps_original_rows_queryable_during_ingest(
    tmp_path: Path, monkeypatch,
) -> None:
    _archive_one(tmp_path, "alpha", "beta", "change_created")
    _archive_one(tmp_path, "gamma", "alpha", "change_merged")

    observed: dict[str, set[str]] = {}
    real_ingest = index.ingest

    def spy(conn, rec, events):
        # First time the rebuild ingests a row, query the LIVE index path with a
        # separate connection. The atomic rebuild builds into a temp db and only
        # os.replaces it at the very end, so the original committed rows must
        # still be fully visible on disk here.
        if "ids" not in observed:
            reader = sqlite3.connect(archive.index_path(tmp_path))
            try:
                observed["ids"] = {
                    r[0] for r in reader.execute("SELECT id FROM changes")}
            finally:
                reader.close()
        return real_ingest(conn, rec, events)

    monkeypatch.setattr(index, "ingest", spy)
    archive.rebuild_index(tmp_path)

    # Mid-rebuild, the live index still held BOTH original rows (not an empty,
    # just-recreated db).
    assert observed["ids"] == {"alpha", "gamma"}
    # No temp db is left behind in the archive dir after the swap.
    assert not list(archive.archive_root(tmp_path).glob("*.db.tmp"))
    # Final content reproduced from records + blobs.
    conn = index.connect(archive.index_path(tmp_path))
    try:
        ids = {r[0] for r in conn.execute("SELECT id FROM changes")}
    finally:
        conn.close()
    assert ids == {"alpha", "gamma"}
