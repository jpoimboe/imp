from __future__ import annotations

import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from imp.engine import archive
from imp.engine.archive import read


def _seed_change(control: Path, cid: str, last_ts: str) -> None:
    """Archive one change normally so its record + blob + a v2 index.db exist."""
    events = control / f"{cid}.jsonl"
    events.write_text(
        f'{{"id":"{cid}-e0","timestamp":"{last_ts}","type":"change_created",'
        f'"payload":{{}}}}\n')
    change_json = control / f"{cid}-change.json"
    change_json.write_text(json.dumps({
        "id": cid, "title": cid.title(), "base_branch": "main",
        "created_at": "2026-06-01T00:00:00+00:00", "depends_on": []}))
    state_json = control / f"{cid}-state.json"
    state_json.write_text(json.dumps({
        "id": cid, "status": "completed", "step": "finalize"}))
    archive.archive_change(
        control, cid, events_path=events,
        change_json_path=change_json, state_json_path=state_json)


def _write_legacy_v0_db(db_path: Path) -> None:
    """Overwrite index.db with a synthesized v0 db: old schema (no last_event_ts),
    a populated events table + idx_events_*, a bogus 'stale' row, user_version 0.
    Records/blobs on disk are untouched, so a rebuild reconstructs the real rows."""
    db_path.unlink(missing_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "CREATE TABLE changes (id TEXT PRIMARY KEY, title TEXT, base TEXT,"
            " created_at TEXT, status TEXT, step TEXT, events_hash TEXT,"
            " ingested_at TEXT)")
        conn.execute("CREATE TABLE deps (change_id TEXT, depends_on_id TEXT)")
        conn.execute(
            "CREATE TABLE events (change_id TEXT, seq INTEGER, ts TEXT,"
            " type TEXT, payload_json TEXT)")
        conn.execute(
            "CREATE INDEX idx_events_change_seq ON events(change_id, seq)")
        conn.execute(
            "INSERT INTO changes (id, title, status) VALUES"
            " ('stale', 'Stale Bogus', 'completed')")
        conn.execute(
            "INSERT INTO events (change_id, seq, ts) VALUES ('stale', 0, 'x')")
        conn.commit()
    finally:
        conn.close()
    # user_version defaults to 0 -> reads back as a stale legacy db.


def _user_version(db: Path) -> int:
    conn = sqlite3.connect(db)
    try:
        return int(conn.execute("PRAGMA user_version").fetchone()[0])
    finally:
        conn.close()


def _table_names(db: Path) -> set[str]:
    conn = sqlite3.connect(db)
    try:
        return {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()


def _rows_by_id(db: Path) -> dict[str, str | None]:
    conn = sqlite3.connect(db)
    try:
        try:
            return {r[0]: r[1] for r in conn.execute(
                "SELECT id, last_event_ts FROM changes")}
        except sqlite3.OperationalError:
            # Legacy v0 schema lacks last_event_ts; return ids with None values.
            return {r[0]: None for r in conn.execute("SELECT id FROM changes")}
    finally:
        conn.close()


def test_ensure_current_fast_path_skips_rebuild(tmp_path: Path, monkeypatch) -> None:
    _seed_change(tmp_path, "alpha", "2026-06-01T00:00:01+00:00")
    assert _user_version(archive.index_path(tmp_path)) == 2  # fresh ingest stamped v2

    calls: list[int] = []
    monkeypatch.setattr(archive, "rebuild_index", lambda _root: calls.append(1))

    archive.ensure_current(tmp_path)

    assert calls == []  # current db -> lock-free fast path, no rebuild


def test_ensure_current_migrates_legacy_v0_db(tmp_path: Path) -> None:
    _seed_change(tmp_path, "alpha", "2026-06-01T00:00:01+00:00")
    db = archive.index_path(tmp_path)
    _write_legacy_v0_db(db)
    # Precondition: a stale v0 db with an events table and a bogus row.
    assert _user_version(db) == 0
    assert "events" in _table_names(db)
    assert "stale" in _rows_by_id(db)

    archive.ensure_current(tmp_path)

    assert "events" not in _table_names(db)
    assert _user_version(db) == 2
    rows = _rows_by_id(db)
    assert "stale" not in rows                       # rebuilt from records, not the old db
    assert rows["alpha"] == "2026-06-01T00:00:01+00:00"  # last_event_ts denormalized
