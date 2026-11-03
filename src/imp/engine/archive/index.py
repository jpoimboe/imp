# SPDX-License-Identifier: MIT
# Copyright 2026 Josh Poimboeuf
"""Derived SQLite index/catalog: rebuildable from record files + blobs."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS quests ("
    " id TEXT PRIMARY KEY, title TEXT, base TEXT, created_at TEXT,"
    " status TEXT, step TEXT, events_hash TEXT, ingested_at TEXT,"
    " last_event_ts TEXT, content TEXT, events_content TEXT)",
    "CREATE TABLE IF NOT EXISTS deps ("
    " quest_id TEXT, depends_on_id TEXT)",
)

_SCHEMA_VERSION = 8


def _commit(conn: sqlite3.Connection) -> None:
    conn.commit()


def connect(index_path: Path) -> sqlite3.Connection:
    index_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(index_path, timeout=30)
    fresh = conn.execute(
        "SELECT 1 FROM sqlite_master"
        " WHERE type='table' AND name='quests'").fetchone() is None
    for stmt in _SCHEMA:
        conn.execute(stmt)
    if fresh:
        conn.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
    _commit(conn)
    return conn


def open_readonly(db_path: Path) -> sqlite3.Connection | None:
    """A read-only connection for another project's index: unlike `connect`, it
    creates no parent directory, no file and no schema. None when the index is
    absent or cannot be opened; a file that is not a database opens here and
    raises on the caller's first statement."""
    if not db_path.is_file():
        return None
    try:
        return sqlite3.connect(
            f"{db_path.resolve().as_uri()}?mode=ro", uri=True, timeout=30)
    except sqlite3.DatabaseError:
        return None


def quest_spans(conn: sqlite3.Connection) -> list[tuple[str, str | None, str | None]]:
    """(id, created_at, last_event_ts) per indexed quest: the activity window a
    span query filters on. last_event_ts is None for a log with no timestamps."""
    return [(cid, created, last)
            for cid, created, last in conn.execute(
                "SELECT id, created_at, last_event_ts FROM quests ORDER BY id")]


def stamp_version(conn: sqlite3.Connection) -> None:
    """An older writer lowers the floor, so the next current imp rebuilds its row."""
    conn.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
    _commit(conn)


def ingest(
    conn: sqlite3.Connection, record: dict[str, Any], events: list[dict[str, Any]],
) -> None:
    """Idempotently load one quest's record + events into the index."""
    cid = record["id"]
    conn.execute("DELETE FROM quests WHERE id = ?", (cid,))
    conn.execute("DELETE FROM deps WHERE quest_id = ?", (cid,))
    final_state = record.get("final_state") or {}
    timestamps = [ts for ev in events if (ts := ev.get("timestamp")) is not None]
    last_event_ts = max(timestamps) if timestamps else None
    from imp.cli.render_helpers import conversation_prose, events_content
    content = conversation_prose(events)
    events_content_text = events_content(events)
    conn.execute(
        "INSERT INTO quests (id, title, base, created_at, status, step,"
        " events_hash, ingested_at, last_event_ts, content, events_content)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (cid, record.get("title"), record.get("base"), record.get("created_at"),
         final_state.get("status"), final_state.get("step"),
         record.get("events_hash"), datetime.now(UTC).isoformat(), last_event_ts,
         content, events_content_text))
    for dep in record.get("depends_on") or []:
        conn.execute("INSERT INTO deps (quest_id, depends_on_id) VALUES (?, ?)",
                     (cid, dep))
    _commit(conn)


def quest_ids(conn: sqlite3.Connection) -> list[str]:
    return [r[0] for r in conn.execute("SELECT id FROM quests ORDER BY id")]


def has_quest(conn: sqlite3.Connection, quest_id: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM quests WHERE id = ?", (quest_id,)).fetchone() is not None


def completed_rows(
    conn: sqlite3.Connection,
) -> list[tuple[str, str, str, str, str, str, str | None]]:
    rows = conn.execute(
        "SELECT id, status, step, title, created_at, last_event_ts"
        " FROM quests").fetchall()
    return [
        (cid, status or "", step or "-", last or "", title or "", created or "", None)
        for (cid, status, step, title, created, last) in rows
    ]


def _like_pattern(query: str) -> str:
    # Escape LIKE metacharacters so a `%`/`_`/`\` in the user's query matches
    # literally instead of acting as a wildcard.
    escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def search_content(
    conn: sqlite3.Connection, query: str,
) -> list[tuple[str, str, str, str, str | None]]:
    like = _like_pattern(query)
    rows = conn.execute(
        "SELECT id, title, content, status, last_event_ts FROM quests"
        " WHERE id LIKE ? ESCAPE '\\' OR title LIKE ? ESCAPE '\\'"
        " OR content LIKE ? ESCAPE '\\'"
        " ORDER BY created_at DESC, id",
        (like, like, like)).fetchall()
    return [
        (cid, title or "", content or "", status or "", last)
        for (cid, title, content, status, last) in rows
    ]


def search_events_content(
    conn: sqlite3.Connection, query: str,
) -> list[tuple[str, str, str, str | None]]:
    like = _like_pattern(query)
    rows = conn.execute(
        "SELECT id, title, status, last_event_ts FROM quests"
        " WHERE events_content LIKE ? ESCAPE '\\'"
        " ORDER BY created_at DESC, id",
        (like,)).fetchall()
    return [
        (cid, title or "", status or "", last)
        for (cid, title, status, last) in rows
    ]
