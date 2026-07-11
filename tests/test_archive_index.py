from __future__ import annotations

import sqlite3
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
        assert {"changes", "deps"} <= _table_names(conn)
        assert "events" not in _table_names(conn)
        assert not any(name.startswith("idx_events_") for name in _index_names(conn))
    finally:
        conn.close()


def test_connect_is_idempotent(tmp_path: Path) -> None:
    db = tmp_path / "sub" / "index.db"
    index.connect(db).close()  # also creates the parent dir
    conn = index.connect(db)   # second connect must not raise on existing tables
    try:
        assert {"changes", "deps"} <= _table_names(conn)
        assert "events" not in _table_names(conn)
    finally:
        conn.close()


def _sample_record() -> dict:
    return {
        "id": "alpha",
        "title": "Alpha",
        "base": "main",
        "created_at": "2026-06-01T00:00:00+00:00",
        "depends_on": ["beta", "gamma"],
        "final_state": {"status": "completed", "step": "finalize"},
        "events_hash": "deadbeef",
    }


def _sample_events() -> list[dict]:
    return [
        {"id": "e0", "parent_id": None, "agent_id": None,
         "timestamp": "2026-06-01T00:00:01+00:00", "type": "change_created",
         "payload": {"id": "alpha"}},
        {"id": "e1", "parent_id": "e0", "agent_id": "plan-writer",
         "timestamp": "2026-06-01T00:00:02+00:00", "type": "step_started",
         "payload": {"step": "plan-writer"}},
    ]


def test_ingest_populates_changes_deps(tmp_path: Path) -> None:
    conn = index.connect(tmp_path / "index.db")
    try:
        index.ingest(conn, _sample_record(), _sample_events())

        crow = conn.execute(
            "SELECT id, title, base, created_at, status, step, events_hash,"
            " last_event_ts FROM changes").fetchall()
        assert crow == [(
            "alpha", "Alpha", "main", "2026-06-01T00:00:00+00:00",
            "completed", "finalize", "deadbeef", "2026-06-01T00:00:02+00:00")]
        assert conn.execute(
            "SELECT ingested_at FROM changes WHERE id='alpha'"
        ).fetchone()[0] is not None

        deps = conn.execute(
            "SELECT depends_on_id FROM deps WHERE change_id='alpha'"
            " ORDER BY depends_on_id").fetchall()
        assert deps == [("beta",), ("gamma",)]
    finally:
        conn.close()


def _prose_events() -> list[dict]:
    return [
        {"id": "e0", "parent_id": None, "agent_id": None,
         "timestamp": "2026-06-01T00:00:01+00:00", "type": "opening_intent",
         "payload": {"text": "wire a 300s deadline"}},
        {"id": "e1", "parent_id": "e0", "agent_id": None,
         "timestamp": "2026-06-01T00:00:02+00:00", "type": "tool_call",
         "payload": {"name": "bash", "input": {"cmd": "SECRETNOISE"}}},
    ]


def test_ingest_populates_content_prose(tmp_path: Path) -> None:
    conn = index.connect(tmp_path / "index.db")
    try:
        index.ingest(conn, _sample_record(), _prose_events())
        content = conn.execute(
            "SELECT content FROM changes WHERE id='alpha'").fetchone()[0]
        assert "300s" in content            # opening_intent prose materialized
        assert "SECRETNOISE" not in content  # tool_call payload excluded
    finally:
        conn.close()


def test_ingest_is_idempotent(tmp_path: Path) -> None:
    conn = index.connect(tmp_path / "index.db")
    try:
        index.ingest(conn, _sample_record(), _sample_events())
        index.ingest(conn, _sample_record(), _sample_events())
        assert conn.execute("SELECT COUNT(*) FROM changes").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM deps").fetchone()[0] == 2
    finally:
        conn.close()


def test_ingest_denormalizes_last_event_ts(tmp_path: Path) -> None:
    conn = index.connect(tmp_path / "index.db")
    try:
        index.ingest(conn, _sample_record(), _sample_events())
        assert conn.execute(
            "SELECT last_event_ts FROM changes WHERE id='alpha'"
        ).fetchone()[0] == "2026-06-01T00:00:02+00:00"
    finally:
        conn.close()


def test_ingest_last_event_ts_empty_events(tmp_path: Path) -> None:
    conn = index.connect(tmp_path / "index.db")
    try:
        index.ingest(conn, _sample_record(), [])
        assert conn.execute(
            "SELECT last_event_ts FROM changes WHERE id='alpha'"
        ).fetchone()[0] is None
    finally:
        conn.close()


def test_ingest_last_event_ts_skips_malformed(tmp_path: Path) -> None:
    conn = index.connect(tmp_path / "index.db")
    try:
        events = [
            {"id": "e0", "parent_id": None, "agent_id": None,
             "timestamp": "2026-06-01T00:00:05+00:00", "type": "change_created",
             "payload": {"id": "alpha"}},
            {"_malformed": True, "raw": "NOT_JSON"},
        ]
        index.ingest(conn, _sample_record(), events)
        assert conn.execute(
            "SELECT last_event_ts FROM changes WHERE id='alpha'"
        ).fetchone()[0] == "2026-06-01T00:00:05+00:00"
    finally:
        conn.close()


def test_ingest_last_event_ts_all_malformed(tmp_path: Path) -> None:
    conn = index.connect(tmp_path / "index.db")
    try:
        events = [
            {"_malformed": True, "raw": "NOT_JSON"},
            {"_malformed": True, "raw": "ALSO_NOT_JSON"},
        ]
        index.ingest(conn, _sample_record(), events)
        assert conn.execute(
            "SELECT last_event_ts FROM changes WHERE id='alpha'"
        ).fetchone()[0] is None
    finally:
        conn.close()


def test_connect_stamps_fresh_db_user_version(tmp_path: Path) -> None:
    conn = index.connect(tmp_path / "index.db")
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 4
    finally:
        conn.close()


def test_connect_pre_existing_db_keeps_user_version(tmp_path: Path) -> None:
    db = tmp_path / "index.db"
    raw = sqlite3.connect(db)
    raw.execute("CREATE TABLE changes (id TEXT PRIMARY KEY)")
    raw.commit()
    raw.close()
    # Precondition: a legacy db reads back as version 0.
    pre = sqlite3.connect(db)
    assert pre.execute("PRAGMA user_version").fetchone()[0] == 0
    pre.close()

    conn = index.connect(db)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 0
    finally:
        conn.close()


def test_completed_rows_reads_denormalized_last_event_ts(tmp_path: Path) -> None:
    conn = index.connect(tmp_path / "index.db")
    try:
        conn.execute(
            "INSERT INTO changes (id, status, step, title, created_at,"
            " last_event_ts) VALUES (?, ?, ?, ?, ?, ?)",
            ("alpha", "completed", "finalize", "Alpha",
             "2026-06-01T00:00:00+00:00", "2026-06-01T09:00:00+00:00"))
        conn.commit()
        assert index.completed_rows(conn) == [(
            "alpha", "completed", "finalize", "2026-06-01T09:00:00+00:00",
            "Alpha", "2026-06-01T00:00:00+00:00", None)]
    finally:
        conn.close()


def test_most_recent_id_orders_by_last_event_ts_desc(tmp_path: Path) -> None:
    conn = index.connect(tmp_path / "index.db")
    try:
        conn.executemany(
            "INSERT INTO changes (id, last_event_ts) VALUES (?, ?)",
            [("older", "2026-06-01T00:00:00+00:00"),
             ("newer", "2026-06-02T00:00:00+00:00")])
        conn.commit()
        assert index.most_recent_id(conn) == "newer"
    finally:
        conn.close()


def test_most_recent_id_ignores_null_last_event_ts(tmp_path: Path) -> None:
    conn = index.connect(tmp_path / "index.db")
    try:
        conn.executemany(
            "INSERT INTO changes (id, last_event_ts) VALUES (?, ?)",
            [("has-ts", "2026-06-01T00:00:00+00:00"), ("no-ts", None)])
        conn.commit()
        assert index.most_recent_id(conn) == "has-ts"
    finally:
        conn.close()


def test_most_recent_id_none_when_no_timestamped_rows(tmp_path: Path) -> None:
    conn = index.connect(tmp_path / "index.db")
    try:
        conn.execute(
            "INSERT INTO changes (id, last_event_ts) VALUES (?, NULL)", ("only",))
        conn.commit()
        assert index.most_recent_id(conn) is None
    finally:
        conn.close()


def test_most_recent_id_breaks_tie_on_highest_id(tmp_path: Path) -> None:
    conn = index.connect(tmp_path / "index.db")
    try:
        conn.executemany(
            "INSERT INTO changes (id, last_event_ts) VALUES (?, ?)",
            [("aaa", "2026-06-01T00:00:00+00:00"),
             ("zzz", "2026-06-01T00:00:00+00:00")])
        conn.commit()
        assert index.most_recent_id(conn) == "zzz"
    finally:
        conn.close()


def test_last_event_ts_returns_denormalized_value(tmp_path: Path) -> None:
    conn = index.connect(tmp_path / "index.db")
    try:
        conn.execute(
            "INSERT INTO changes (id, last_event_ts) VALUES (?, ?)",
            ("alpha", "2026-06-01T09:00:00+00:00"))
        conn.commit()
        assert index.last_event_ts(conn, "alpha") == "2026-06-01T09:00:00+00:00"
    finally:
        conn.close()


def test_last_event_ts_none_when_id_absent(tmp_path: Path) -> None:
    conn = index.connect(tmp_path / "index.db")
    try:
        assert index.last_event_ts(conn, "ghost") is None
    finally:
        conn.close()


def test_last_event_ts_none_when_column_null(tmp_path: Path) -> None:
    conn = index.connect(tmp_path / "index.db")
    try:
        conn.execute(
            "INSERT INTO changes (id, last_event_ts) VALUES (?, NULL)", ("alpha",))
        conn.commit()
        assert index.last_event_ts(conn, "alpha") is None
    finally:
        conn.close()


def test_search_matches_content_and_still_matches_id_title(tmp_path: Path) -> None:
    conn = index.connect(tmp_path / "index.db")
    try:
        index.ingest(conn, _sample_record(), _prose_events())
        # body term present only in the conversation prose, not id/title
        assert [r[0] for r in index.search(conn, "300s")] == ["alpha"]
        # id and title still match (regression guard)
        assert [r[0] for r in index.search(conn, "alph")] == ["alpha"]
        assert [r[0] for r in index.search(conn, "Alpha")] == ["alpha"]
        # a term in none of id/title/content does not match
        assert index.search(conn, "SECRETNOISE") == []
    finally:
        conn.close()
