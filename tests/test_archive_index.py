from __future__ import annotations

from pathlib import Path

from imp.engine.archive import index


def _table_names(conn) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    return {r[0] for r in rows}


def _index_names(conn) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index'").fetchall()
    return {r[0] for r in rows}


def test_connect_creates_schema(tmp_path: Path) -> None:
    conn = index.connect(tmp_path / "index.db")
    try:
        assert {"changes", "deps", "events"} <= _table_names(conn)
        assert {
            "idx_events_type", "idx_events_change", "idx_events_agent",
        } <= _index_names(conn)
    finally:
        conn.close()


def test_connect_is_idempotent(tmp_path: Path) -> None:
    db = tmp_path / "sub" / "index.db"
    index.connect(db).close()  # also creates the parent dir
    conn = index.connect(db)   # second connect must not raise on existing tables
    try:
        assert {"changes", "deps", "events"} <= _table_names(conn)
    finally:
        conn.close()
