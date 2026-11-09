from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

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
        assert {"quests", "deps"} <= _table_names(conn)
        assert "events" not in _table_names(conn)
        assert not any(name.startswith("idx_events_") for name in _index_names(conn))
    finally:
        conn.close()


def test_connect_is_idempotent(tmp_path: Path) -> None:
    db = tmp_path / "sub" / "index.db"
    index.connect(db).close()  # also creates the parent dir
    conn = index.connect(db)   # second connect must not raise on existing tables
    try:
        assert {"quests", "deps"} <= _table_names(conn)
        assert "events" not in _table_names(conn)
    finally:
        conn.close()


def test_open_readonly_creates_nothing_and_refuses_writes(tmp_path: Path) -> None:
    absent = tmp_path / "gone" / "index.db"
    assert index.open_readonly(absent) is None
    assert not (tmp_path / "gone").exists()   # connect() would have made it

    db = tmp_path / "index.db"
    index.connect(db).close()
    conn = index.open_readonly(db)
    assert conn is not None
    try:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute(
                "INSERT INTO quests (id) VALUES ('x')")
    finally:
        conn.close()


def test_open_readonly_defers_the_header_check_to_the_first_statement(
    tmp_path: Path,
) -> None:
    db = tmp_path / "index.db"
    db.write_bytes(b"not a database at all")
    conn = index.open_readonly(db)
    # sqlite defers the header check to the first statement, so the caller's
    # query is where a foreign file surfaces.
    assert conn is not None
    try:
        with pytest.raises(sqlite3.DatabaseError):
            index.quest_spans(conn)
    finally:
        conn.close()


def test_quest_spans_reports_each_quests_activity_window(tmp_path: Path) -> None:
    conn = index.connect(tmp_path / "index.db")
    try:
        conn.execute(
            "INSERT INTO quests (id, created_at, last_event_ts)"
            " VALUES ('alpha', '2026-06-01T00:00:00+00:00',"
            " '2026-06-01T09:00:00+00:00')")
        conn.execute(
            "INSERT INTO quests (id, created_at, last_event_ts)"
            " VALUES ('beta', '2026-06-02T00:00:00+00:00', NULL)")
        conn.commit()
        assert sorted(index.quest_spans(conn)) == [
            ("alpha", "2026-06-01T00:00:00+00:00", "2026-06-01T09:00:00+00:00"),
            ("beta", "2026-06-02T00:00:00+00:00", None)]
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
         "timestamp": "2026-06-01T00:00:01+00:00", "type": "quest_created",
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
            " last_event_ts FROM quests").fetchall()
        assert crow == [(
            "alpha", "Alpha", "main", "2026-06-01T00:00:00+00:00",
            "completed", "finalize", "deadbeef", "2026-06-01T00:00:02+00:00")]
        assert conn.execute(
            "SELECT ingested_at FROM quests WHERE id='alpha'"
        ).fetchone()[0] is not None

        deps = conn.execute(
            "SELECT depends_on_id FROM deps WHERE quest_id='alpha'"
            " ORDER BY depends_on_id").fetchall()
        assert deps == [("beta",), ("gamma",)]
    finally:
        conn.close()


def _prose_events() -> list[dict]:
    return [
        {"id": "e0", "parent_id": None, "agent_id": None,
         "timestamp": "2026-06-01T00:00:01+00:00", "type": "user_message",
         "payload": {"role": "user", "subtype": "intent",
                     "content": [{"type": "text", "text": "wire a 300s deadline"}]}},
        {"id": "e1", "parent_id": "e0", "agent_id": None,
         "timestamp": "2026-06-01T00:00:02+00:00", "type": "tool_call",
         "payload": {"name": "bash", "input": {"cmd": "SECRETNOISE"}}},
    ]


def test_ingest_populates_content_prose(tmp_path: Path) -> None:
    conn = index.connect(tmp_path / "index.db")
    try:
        index.ingest(conn, _sample_record(), _prose_events())
        content = conn.execute(
            "SELECT content FROM quests WHERE id='alpha'").fetchone()[0]
        assert "300s" in content            # user_message/intent prose materialized
        assert "SECRETNOISE" not in content  # tool_call payload excluded
    finally:
        conn.close()


def test_ingest_is_idempotent(tmp_path: Path) -> None:
    conn = index.connect(tmp_path / "index.db")
    try:
        index.ingest(conn, _sample_record(), _sample_events())
        index.ingest(conn, _sample_record(), _sample_events())
        assert conn.execute("SELECT COUNT(*) FROM quests").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM deps").fetchone()[0] == 2
    finally:
        conn.close()


def test_ingest_denormalizes_last_event_ts(tmp_path: Path) -> None:
    conn = index.connect(tmp_path / "index.db")
    try:
        index.ingest(conn, _sample_record(), _sample_events())
        assert conn.execute(
            "SELECT last_event_ts FROM quests WHERE id='alpha'"
        ).fetchone()[0] == "2026-06-01T00:00:02+00:00"
    finally:
        conn.close()


def test_ingest_last_event_ts_empty_events(tmp_path: Path) -> None:
    conn = index.connect(tmp_path / "index.db")
    try:
        index.ingest(conn, _sample_record(), [])
        assert conn.execute(
            "SELECT last_event_ts FROM quests WHERE id='alpha'"
        ).fetchone()[0] is None
    finally:
        conn.close()


def test_ingest_last_event_ts_skips_malformed(tmp_path: Path) -> None:
    conn = index.connect(tmp_path / "index.db")
    try:
        events = [
            {"id": "e0", "parent_id": None, "agent_id": None,
             "timestamp": "2026-06-01T00:00:05+00:00", "type": "quest_created",
             "payload": {"id": "alpha"}},
            {"_malformed": True, "raw": "NOT_JSON"},
        ]
        index.ingest(conn, _sample_record(), events)
        assert conn.execute(
            "SELECT last_event_ts FROM quests WHERE id='alpha'"
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
            "SELECT last_event_ts FROM quests WHERE id='alpha'"
        ).fetchone()[0] is None
    finally:
        conn.close()


def test_connect_stamps_fresh_db_user_version(tmp_path: Path) -> None:
    conn = index.connect(tmp_path / "index.db")
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 10
    finally:
        conn.close()


def test_connect_pre_existing_db_keeps_user_version(tmp_path: Path) -> None:
    db = tmp_path / "index.db"
    raw = sqlite3.connect(db)
    raw.execute("CREATE TABLE quests (id TEXT PRIMARY KEY)")
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
            "INSERT INTO quests (id, status, step, title, created_at,"
            " last_event_ts) VALUES (?, ?, ?, ?, ?, ?)",
            ("alpha", "completed", "finalize", "Alpha",
             "2026-06-01T00:00:00+00:00", "2026-06-01T09:00:00+00:00"))
        conn.commit()
        assert index.completed_rows(conn) == [(
            "alpha", "completed", "finalize", "2026-06-01T09:00:00+00:00",
            "Alpha", "2026-06-01T00:00:00+00:00", None)]
    finally:
        conn.close()


def test_ingest_populates_events_content(tmp_path: Path) -> None:
    conn = index.connect(tmp_path / "index.db")
    try:
        events = [
            {"id": "e0", "type": "tool_result",
             "payload": {"id": "t1", "content": "STACKTRACE_XYZ"}},
            {"id": "e1", "type": "step_context",
             "payload": {"provider": "anthropic", "model": "m",
                         "system_prompt": "PROMPTNOISE", "tools_schema": []}},
        ]
        index.ingest(conn, _sample_record(), events)
        ev = conn.execute(
            "SELECT events_content FROM quests WHERE id='alpha'").fetchone()[0]
        assert "STACKTRACE_XYZ" in ev
        assert "PROMPTNOISE" not in ev
    finally:
        conn.close()


def test_search_events_content_matches_structured_events_only(tmp_path: Path) -> None:
    conn = index.connect(tmp_path / "index.db")
    try:
        events = [
            {"id": "e0", "type": "user_message",
             "payload": {"subtype": "intent",
                         "content": [{"type": "text", "text": "add a deadline"}]}},
            {"id": "e1", "type": "tool_call",
             "payload": {"name": "bash", "input": {"cmd": "GREPTARGET"}}},
        ]
        index.ingest(conn, _sample_record(), events)
        assert [r[0] for r in index.search_events_content(conn, "GREPTARGET")] == ["alpha"]
        assert [r[0] for r in index.search_events_content(conn, "greptarget")] == ["alpha"]
        assert index.search_events_content(conn, "nonexistent-zzz") == []
        assert index.search_events_content(conn, "alpha") == []   # id not searched
        assert index.search_events_content(conn, "Alpha") == []   # title not searched
        assert index.search_events_content(conn, "GREPTARGET")[0] == (
            "alpha", "Alpha", "completed", None)
    finally:
        conn.close()

