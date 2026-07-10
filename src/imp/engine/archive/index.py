"""Derived SQLite index/catalog: rebuildable from record files + blobs."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS changes ("
    " id TEXT PRIMARY KEY, title TEXT, base TEXT, created_at TEXT,"
    " status TEXT, step TEXT, events_hash TEXT, ingested_at TEXT,"
    " last_event_ts TEXT, content TEXT)",
    "CREATE TABLE IF NOT EXISTS deps ("
    " quest_id TEXT, depends_on_id TEXT)",
)

_SCHEMA_VERSION = 4


def connect(index_path: Path) -> sqlite3.Connection:
    index_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(index_path, timeout=30)
    fresh = conn.execute(
        "SELECT 1 FROM sqlite_master"
        " WHERE type='table' AND name='changes'").fetchone() is None
    for stmt in _SCHEMA:
        conn.execute(stmt)
    if fresh:
        conn.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
    conn.commit()
    return conn


def ingest(
    conn: sqlite3.Connection, record: dict[str, Any], events: list[dict[str, Any]],
) -> None:
    """Idempotently load one Change's record + events into the index."""
    cid = record["id"]
    conn.execute("DELETE FROM changes WHERE id = ?", (cid,))
    conn.execute("DELETE FROM deps WHERE quest_id = ?", (cid,))
    final_state = record.get("final_state") or {}
    timestamps = [ts for ev in events if (ts := ev.get("timestamp")) is not None]
    last_event_ts = max(timestamps) if timestamps else None
    from imp.cli.render_helpers import conversation_prose
    content = conversation_prose(events)
    conn.execute(
        "INSERT INTO changes (id, title, base, created_at, status, step,"
        " events_hash, ingested_at, last_event_ts, content)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (cid, record.get("title"), record.get("base"), record.get("created_at"),
         final_state.get("status"), final_state.get("step"),
         record.get("events_hash"), datetime.now(UTC).isoformat(), last_event_ts,
         content))
    for dep in record.get("depends_on") or []:
        conn.execute("INSERT INTO deps (quest_id, depends_on_id) VALUES (?, ?)",
                     (cid, dep))
    conn.commit()


def quest_ids(conn: sqlite3.Connection) -> list[str]:
    return [r[0] for r in conn.execute("SELECT id FROM changes ORDER BY id")]


def has_change(conn: sqlite3.Connection, quest_id: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM changes WHERE id = ?", (quest_id,)).fetchone() is not None


def most_recent_id(conn: sqlite3.Connection) -> str | None:
    row = conn.execute(
        "SELECT id FROM changes WHERE last_event_ts IS NOT NULL"
        " ORDER BY last_event_ts DESC, id DESC LIMIT 1").fetchone()
    return row[0] if row else None


def last_event_ts(conn: sqlite3.Connection, quest_id: str) -> str | None:
    row = conn.execute(
        "SELECT last_event_ts FROM changes WHERE id = ?", (quest_id,)).fetchone()
    return row[0] if row and row[0] else None


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


def _like_pattern(query: str) -> str:
    # Escape LIKE metacharacters so a `%`/`_`/`\` in the user's query matches
    # literally instead of acting as a wildcard.
    escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def search(
    conn: sqlite3.Connection, query: str,
) -> list[tuple[str, str, str, str]]:
    like = _like_pattern(query)
    rows = conn.execute(
        "SELECT id, title, status, created_at FROM changes"
        " WHERE id LIKE ? ESCAPE '\\' OR title LIKE ? ESCAPE '\\'"
        " OR content LIKE ? ESCAPE '\\'"
        " ORDER BY created_at DESC, id",
        (like, like, like)).fetchall()
    return [
        (cid, title or "", status or "", created or "")
        for (cid, title, status, created) in rows
    ]


def search_content(
    conn: sqlite3.Connection, query: str,
) -> list[tuple[str, str, str]]:
    like = _like_pattern(query)
    rows = conn.execute(
        "SELECT id, title, content FROM changes"
        " WHERE id LIKE ? ESCAPE '\\' OR title LIKE ? ESCAPE '\\'"
        " OR content LIKE ? ESCAPE '\\'"
        " ORDER BY created_at DESC, id",
        (like, like, like)).fetchall()
    return [
        (cid, title or "", content or "")
        for (cid, title, content) in rows
    ]
