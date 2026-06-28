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


def test_ingest_populates_changes_deps_events(tmp_path: Path) -> None:
    conn = index.connect(tmp_path / "index.db")
    try:
        index.ingest(conn, _sample_record(), _sample_events())

        crow = conn.execute(
            "SELECT id, title, base, created_at, status, step, events_hash"
            " FROM changes").fetchall()
        assert crow == [(
            "alpha", "Alpha", "main", "2026-06-01T00:00:00+00:00",
            "completed", "finalize", "deadbeef")]
        assert conn.execute(
            "SELECT ingested_at FROM changes WHERE id='alpha'"
        ).fetchone()[0] is not None

        deps = conn.execute(
            "SELECT depends_on_id FROM deps WHERE change_id='alpha'"
            " ORDER BY depends_on_id").fetchall()
        assert deps == [("beta",), ("gamma",)]

        evs = conn.execute(
            "SELECT seq, event_id, parent_id, agent_id, ts, type, payload_json"
            " FROM events WHERE change_id='alpha' ORDER BY seq").fetchall()
        assert evs == [
            (0, "e0", None, None, "2026-06-01T00:00:01+00:00",
             "change_created", '{"id": "alpha"}'),
            (1, "e1", "e0", "plan-writer", "2026-06-01T00:00:02+00:00",
             "step_started", '{"step": "plan-writer"}'),
        ]
    finally:
        conn.close()


def test_ingest_is_idempotent(tmp_path: Path) -> None:
    conn = index.connect(tmp_path / "index.db")
    try:
        index.ingest(conn, _sample_record(), _sample_events())
        index.ingest(conn, _sample_record(), _sample_events())
        assert conn.execute("SELECT COUNT(*) FROM changes").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM deps").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 2
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
