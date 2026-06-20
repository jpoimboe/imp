"""Derived SQLite index/catalog: rebuildable from record files + blobs."""

from __future__ import annotations

import sqlite3
from pathlib import Path

_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS changes ("
    " id TEXT PRIMARY KEY, title TEXT, base TEXT, created_at TEXT,"
    " status TEXT, step TEXT, events_hash TEXT, ingested_at TEXT)",
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
    conn = sqlite3.connect(index_path)
    for stmt in _SCHEMA:
        conn.execute(stmt)
    conn.commit()
    return conn
