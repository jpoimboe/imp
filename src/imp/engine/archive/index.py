"""Derived SQLite index/catalog: rebuildable from record files + blobs."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS changes ("
    " id TEXT PRIMARY KEY, title TEXT, base TEXT, created_at TEXT,"
    " status TEXT, step TEXT, events_hash TEXT, ingested_at TEXT,"
    " last_event_ts TEXT)",
    "CREATE TABLE IF NOT EXISTS deps ("
    " change_id TEXT, depends_on_id TEXT)",
    "CREATE TABLE IF NOT EXISTS events ("
    " change_id TEXT, seq INTEGER, event_id TEXT, parent_id TEXT,"
    " agent_id TEXT, ts TEXT, type TEXT, payload_json TEXT)",
    "CREATE INDEX IF NOT EXISTS idx_events_type ON events(type)",
    "CREATE INDEX IF NOT EXISTS idx_events_change ON events(change_id)",
    "CREATE INDEX IF NOT EXISTS idx_events_agent ON events(agent_id)",
)


def connect(index_path: Path) -> sqlite3.Connection:
    index_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(index_path, timeout=30)
    for stmt in _SCHEMA:
        conn.execute(stmt)
    conn.commit()
    return conn


def ingest(
    conn: sqlite3.Connection, record: dict[str, Any], events: list[dict[str, Any]],
) -> None:
    """Idempotently load one Change's record + events into the index."""
    cid = record["id"]
    conn.execute("DELETE FROM changes WHERE id = ?", (cid,))
    conn.execute("DELETE FROM deps WHERE change_id = ?", (cid,))
    conn.execute("DELETE FROM events WHERE change_id = ?", (cid,))
    final_state = record.get("final_state") or {}
    timestamps = [ts for ev in events if (ts := ev.get("timestamp")) is not None]
    last_event_ts = max(timestamps) if timestamps else None
    conn.execute(
        "INSERT INTO changes (id, title, base, created_at, status, step,"
        " events_hash, ingested_at, last_event_ts)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (cid, record.get("title"), record.get("base"), record.get("created_at"),
         final_state.get("status"), final_state.get("step"),
         record.get("events_hash"), datetime.now(UTC).isoformat(), last_event_ts))
    for dep in record.get("depends_on") or []:
        conn.execute("INSERT INTO deps (change_id, depends_on_id) VALUES (?, ?)",
                     (cid, dep))
    for seq, ev in enumerate(events):
        conn.execute(
            "INSERT INTO events (change_id, seq, event_id, parent_id, agent_id,"
            " ts, type, payload_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (cid, seq, ev.get("id"), ev.get("parent_id"), ev.get("agent_id"),
             ev.get("timestamp"), ev.get("type"),
             json.dumps(ev.get("payload"))))
    conn.commit()


def change_ids(conn: sqlite3.Connection) -> list[str]:
    return [r[0] for r in conn.execute("SELECT id FROM changes ORDER BY id")]


def has_change(conn: sqlite3.Connection, change_id: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM changes WHERE id = ?", (change_id,)).fetchone() is not None


def completed_rows(
    conn: sqlite3.Connection,
) -> list[tuple[str, str, str, str, str, str, str | None]]:
    rows = conn.execute(
        "SELECT id, status, step, title, created_at, last_event_ts"
        " FROM changes").fetchall()
    return [
        (cid, status or "", step or "-", last or "", title or "", created or "", None)
        for (cid, status, step, title, created, last) in rows
    ]


def search(
    conn: sqlite3.Connection, query: str,
) -> list[tuple[str, str, str, str]]:
    # Escape LIKE metacharacters so a `%`/`_`/`\` in the user's query matches
    # literally instead of acting as a wildcard.
    escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    like = f"%{escaped}%"
    rows = conn.execute(
        "SELECT id, title, status, created_at FROM changes"
        " WHERE id LIKE ? ESCAPE '\\' OR title LIKE ? ESCAPE '\\'"
        " ORDER BY created_at DESC, id",
        (like, like)).fetchall()
    return [
        (cid, title or "", status or "", created or "")
        for (cid, title, status, created) in rows
    ]
