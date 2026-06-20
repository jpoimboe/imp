from __future__ import annotations

import json
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
            "SELECT id, title, base, created_at, status, step, events_hash"
            " FROM changes").fetchall()),
        "deps": sorted(conn.execute(
            "SELECT change_id, depends_on_id FROM deps").fetchall()),
        "events": sorted(conn.execute(
            "SELECT change_id, seq, event_id, type FROM events").fetchall()),
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
    assert len(after["events"]) == 2
