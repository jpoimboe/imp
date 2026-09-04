from __future__ import annotations

import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from imp.engine import archive
from imp.engine.archive import index, read, record
from imp.engine.archive.blobstore import LocalBlobStore


def _seed_change(control: Path, cid: str, last_ts: str) -> None:
    """Archive one change normally so its record + blob + a v2 index.db exist."""
    events = control / f"{cid}.jsonl"
    events.write_text(
        f'{{"id":"{cid}-e0","timestamp":"{last_ts}","type":"quest_created",'
        f'"payload":{{}}}}\n')
    quest_json = control / f"{cid}-quest.json"
    quest_json.write_text(json.dumps({
        "id": cid, "title": cid.title(), "base_branch": "main",
        "created_at": "2026-06-01T00:00:00+00:00", "depends_on": []}))
    state_json = control / f"{cid}-state.json"
    state_json.write_text(json.dumps({
        "id": cid, "status": "merged", "step": "finalize"}))
    archive.archive_quest(
        control, cid, events_path=events,
        quest_json_path=quest_json, state_json_path=state_json)


def _write_legacy_v0_db(db_path: Path) -> None:
    """Overwrite index.db with a synthesized v0 db: old schema (no last_event_ts),
    a populated events table + idx_events_*, a bogus 'stale' row, user_version 0.
    Records/blobs on disk are untouched, so a rebuild reconstructs the real rows."""
    db_path.unlink(missing_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "CREATE TABLE quests (id TEXT PRIMARY KEY, title TEXT, base TEXT,"
            " created_at TEXT, status TEXT, step TEXT, events_hash TEXT,"
            " ingested_at TEXT)")
        conn.execute("CREATE TABLE deps (quest_id TEXT, depends_on_id TEXT)")
        conn.execute(
            "CREATE TABLE events (quest_id TEXT, seq INTEGER, ts TEXT,"
            " type TEXT, payload_json TEXT)")
        conn.execute(
            "CREATE INDEX idx_events_change_seq ON events(quest_id, seq)")
        conn.execute(
            "INSERT INTO quests (id, title, status) VALUES"
            " ('stale', 'Stale Bogus', 'completed')")
        conn.execute(
            "INSERT INTO events (quest_id, seq, ts) VALUES ('stale', 0, 'x')")
        conn.commit()
    finally:
        conn.close()
    # user_version defaults to 0 -> reads back as a stale legacy db.


def _write_stale_v2_db(db_path: Path) -> None:
    """A v2 db: quests/deps present but NO content column, user_version 2, a
    bogus 'stale' row. Records/blobs on disk carry the real events, so a rebuild
    backfills content for the real rows and drops 'stale'."""
    db_path.unlink(missing_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "CREATE TABLE quests (id TEXT PRIMARY KEY, title TEXT, base TEXT,"
            " created_at TEXT, status TEXT, step TEXT, events_hash TEXT,"
            " ingested_at TEXT, last_event_ts TEXT)")
        conn.execute("CREATE TABLE deps (quest_id TEXT, depends_on_id TEXT)")
        conn.execute("INSERT INTO quests (id, title) VALUES ('stale', 'Stale')")
        conn.execute("PRAGMA user_version = 2")
        conn.commit()
    finally:
        conn.close()


def _write_stale_v4_db(db_path: Path) -> None:
    """A v4 db: quests/deps present, a content column but NO events_content
    column, user_version 4, a bogus 'stale' row. Records/blobs on disk carry the
    real events, so a rebuild backfills events_content for the real rows and
    drops 'stale'."""
    db_path.unlink(missing_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "CREATE TABLE quests (id TEXT PRIMARY KEY, title TEXT, base TEXT,"
            " created_at TEXT, status TEXT, step TEXT, events_hash TEXT,"
            " ingested_at TEXT, last_event_ts TEXT, content TEXT)")
        conn.execute("CREATE TABLE deps (quest_id TEXT, depends_on_id TEXT)")
        conn.execute("INSERT INTO quests (id, title) VALUES ('stale', 'Stale')")
        conn.execute("PRAGMA user_version = 4")
        conn.commit()
    finally:
        conn.close()


def _seed_prose_change(control: Path, cid: str, body: str) -> None:
    """Archive one change whose events carry an assistant prose line."""
    events = control / f"{cid}.jsonl"
    events.write_text(json.dumps({
        "id": f"{cid}-e0", "timestamp": "2026-06-01T00:00:01+00:00",
        "type": "agent_message",
        "payload": {"provider": "anthropic", "raw": {
            "content": [{"type": "text", "text": body}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 1, "output_tokens": 1}}}}) + "\n")
    quest_json = control / f"{cid}-quest.json"
    quest_json.write_text(json.dumps({
        "id": cid, "title": "", "base_branch": "main",
        "created_at": "2026-06-01T00:00:00+00:00", "depends_on": []}))
    state_json = control / f"{cid}-state.json"
    state_json.write_text(json.dumps({
        "id": cid, "status": "merged", "step": "finalize"}))
    archive.archive_quest(
        control, cid, events_path=events,
        quest_json_path=quest_json, state_json_path=state_json)


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
                "SELECT id, last_event_ts FROM quests")}
        except sqlite3.OperationalError:
            # Legacy v0 schema lacks last_event_ts; return ids with None values.
            return {r[0]: None for r in conn.execute("SELECT id FROM quests")}
    finally:
        conn.close()


def test_ensure_current_fast_path_skips_rebuild(tmp_path: Path, monkeypatch) -> None:
    _seed_change(tmp_path, "alpha", "2026-06-01T00:00:01+00:00")
    assert _user_version(archive.index_path(tmp_path)) == 5  # fresh ingest stamped v5

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
    assert _user_version(db) == 5
    rows = _rows_by_id(db)
    assert "stale" not in rows                       # rebuilt from records, not the old db
    assert rows["alpha"] == "2026-06-01T00:00:01+00:00"  # last_event_ts denormalized


def test_completed_rows_migrates_before_query(tmp_path: Path) -> None:
    _seed_change(tmp_path, "alpha", "2026-06-01T00:00:01+00:00")
    db = archive.index_path(tmp_path)
    _write_legacy_v0_db(db)  # legacy db (user_version 0) lacks last_event_ts column

    rows = read.completed_rows(tmp_path)

    # Migration ran first: the new last_event_ts query would OperationalError on
    # the legacy schema; instead it returns the rebuilt v5 rows.
    ids = {r[0] for r in rows}
    assert ids == {"alpha"}            # rebuilt from records; 'stale' dropped
    assert _user_version(db) == 5


def test_search_archive_migrates_before_query(tmp_path: Path) -> None:
    _seed_change(tmp_path, "alpha", "2026-06-01T00:00:01+00:00")
    db = archive.index_path(tmp_path)
    _write_legacy_v0_db(db)  # only the bogus 'stale' row remains in the db

    hits = read.search_archive(tmp_path, "alpha")

    # Without the gate the legacy db has no 'alpha' row -> []. With it, the
    # rebuild restores 'alpha' (title "Alpha") and the search finds it.
    assert [h[0] for h in hits] == ["alpha"]
    assert _user_version(db) == 5


def test_archive_quest_on_legacy_v0_db_migrates_and_ingests(tmp_path: Path) -> None:
    _seed_change(tmp_path, "alpha", "2026-06-01T00:00:01+00:00")
    db = archive.index_path(tmp_path)
    _write_legacy_v0_db(db)  # finalize must not run the v2 INSERT against this

    # A finalize landing on a not-yet-migrated db: it must trigger the rebuild
    # then ingest, never raise OperationalError on the missing column.
    _seed_change(tmp_path, "gamma", "2026-06-02T00:00:02+00:00")

    assert "events" not in _table_names(db)
    assert _user_version(db) == 5
    rows = _rows_by_id(db)
    assert set(rows) == {"alpha", "gamma"}        # 'stale' dropped by the rebuild
    assert rows["alpha"] == "2026-06-01T00:00:01+00:00"
    assert rows["gamma"] == "2026-06-02T00:00:02+00:00"


def test_concurrent_migration_reads_and_ingest_lose_nothing(tmp_path: Path) -> None:
    control = tmp_path
    seeded = [f"c{i}" for i in range(5)]
    for i, cid in enumerate(seeded):
        _seed_change(control, cid, f"2026-06-01T00:00:0{i}+00:00")
    _write_legacy_v0_db(archive.index_path(control))  # force a real migration

    # Pre-stage one finalize's inputs; the writer thread runs archive_quest.
    w_events = control / "writer.jsonl"
    w_events.write_text(
        '{"id":"writer-e0","timestamp":"2026-06-02T00:00:00+00:00",'
        '"type":"quest_created","payload":{}}\n')
    w_change = control / "writer-quest.json"
    w_change.write_text(json.dumps({
        "id": "writer", "title": "Writer", "base_branch": "main",
        "created_at": "2026-06-02T00:00:00+00:00", "depends_on": []}))
    w_state = control / "writer-state.json"
    w_state.write_text(json.dumps({
        "id": "writer", "status": "merged", "step": "finalize"}))

    start = threading.Barrier(6)  # 5 readers + 1 writer start together
    missing_db: list[bool] = []
    missing_db_lock = threading.Lock()

    def reader() -> None:
        start.wait()
        for _ in range(8):
            if not archive.index_path(control).exists():
                with missing_db_lock:
                    missing_db.append(True)
            read.completed_rows(control)

    def writer() -> None:
        start.wait()
        archive.archive_quest(
            control, "writer", events_path=w_events,
            quest_json_path=w_change, state_json_path=w_state)

    with ThreadPoolExecutor(max_workers=6) as ex:
        futs = [ex.submit(reader) for _ in range(5)]
        futs.append(ex.submit(writer))
        for f in futs:
            f.result()  # re-raises any worker exception (e.g. a crash on legacy db)

    assert missing_db == []  # no reader ever saw a missing index across the swap
    final_ids = {r[0] for r in read.completed_rows(control)}
    assert final_ids == set(seeded) | {"writer"}  # migration lost no row, gained the finalize


def test_rebuild_backfills_content_column(tmp_path: Path) -> None:
    _seed_prose_change(tmp_path, "alpha", "the 300s deadline is enforced")
    db = archive.index_path(tmp_path)
    _write_stale_v2_db(db)                        # v2 db, no content column, 'stale' row
    assert _user_version(db) == 2

    archive.ensure_current(tmp_path)              # stale -> one-time rebuild

    assert _user_version(db) == 5
    conn = sqlite3.connect(db)
    try:
        rows = {r[0]: r[1] for r in conn.execute("SELECT id, content FROM quests")}
    finally:
        conn.close()
    assert "stale" not in rows                    # rebuilt from records, not the old db
    assert "300s" in rows["alpha"]                # content backfilled from the blob


def test_schema_bump_reindexes_and_drops_injected_prompt(tmp_path: Path) -> None:
    # Archive a change whose events carry an injected (engine_message/agent_prompt) prompt and a
    # genuine human reply, then simulate a pre-bump index: stamp user_version back
    # to 3 and poison the content column with the injected term, as the old
    # unfiltered ingest would have stored it.
    control = tmp_path
    cid = "arc"
    events = control / f"{cid}.jsonl"
    events.write_text(
        json.dumps({"id": f"{cid}-e0", "timestamp": "2026-06-01T00:00:01+00:00",
                    "type": "engine_message",
                    "payload": {"role": "user", "subtype": "agent_prompt",
                                "content": [{"type": "text",
                                             "text": "INJECTED cover-check prompt"}]}}) + "\n"
        + json.dumps({"id": f"{cid}-e1", "timestamp": "2026-06-01T00:00:02+00:00",
                      "type": "user_message",
                      "payload": {"role": "user", "subtype": "reply",
                                  "content": [{"type": "text",
                                               "text": "please use HUMANTERM"}]}}) + "\n")
    quest_json = control / f"{cid}-quest.json"
    quest_json.write_text(json.dumps({
        "id": cid, "title": "", "base_branch": "main",
        "created_at": "2026-06-01T00:00:00+00:00", "depends_on": []}))
    state_json = control / f"{cid}-state.json"
    state_json.write_text(json.dumps({
        "id": cid, "status": "merged", "step": "finalize"}))
    archive.archive_quest(
        control, cid, events_path=events,
        quest_json_path=quest_json, state_json_path=state_json)

    db = archive.index_path(control)
    conn = sqlite3.connect(db)
    try:
        conn.execute("UPDATE quests SET content = ? WHERE id = ?",
                     ("INJECTED cover-check prompt\nplease use HUMANTERM", cid))
        conn.execute("PRAGMA user_version = 3")   # pre-bump index
        conn.commit()
    finally:
        conn.close()

    archive.ensure_current(control)               # stale v3 -> reindex to v5

    assert _user_version(db) == 5
    conn = sqlite3.connect(db)
    try:
        content = conn.execute(
            "SELECT content FROM quests WHERE id = ?", (cid,)).fetchone()[0]
    finally:
        conn.close()
    assert "INJECTED" not in content              # injected prompt dropped by reindex
    assert "HUMANTERM" in content                 # human reply retained


def test_rebuild_backfills_events_content_column(tmp_path: Path) -> None:
    control = tmp_path
    cid = "evt"
    events = control / f"{cid}.jsonl"
    events.write_text(json.dumps({
        "id": f"{cid}-e0", "timestamp": "2026-06-01T00:00:01+00:00",
        "type": "tool_result",
        "payload": {"id": "t1", "content": "BACKFILL_TRACE"}}) + "\n")
    quest_json = control / f"{cid}-quest.json"
    quest_json.write_text(json.dumps({
        "id": cid, "title": "", "base_branch": "main",
        "created_at": "2026-06-01T00:00:00+00:00", "depends_on": []}))
    state_json = control / f"{cid}-state.json"
    state_json.write_text(json.dumps({
        "id": cid, "status": "merged", "step": "finalize"}))
    archive.archive_quest(
        control, cid, events_path=events,
        quest_json_path=quest_json, state_json_path=state_json)

    db = archive.index_path(control)
    _write_stale_v4_db(db)                        # genuine v4 db, no events_content column
    assert _user_version(db) == 4

    archive.ensure_current(control)               # stale v4 -> reindex to v5

    assert _user_version(db) == 5
    conn = sqlite3.connect(db)
    try:
        rows = {r[0]: r[1] for r in conn.execute(
            "SELECT id, events_content FROM quests")}
    finally:
        conn.close()
    assert "stale" not in rows                    # rebuilt from records, not the old db
    assert "BACKFILL_TRACE" in rows["evt"]        # events_content backfilled from the blob


def _stamp_floors_ahead(control: Path) -> None:
    """Make the archive on disk look like a newer imp wrote it: both floors one ahead."""
    archive.data_version_path(control).write_text(str(archive._DATA_VERSION + 1))
    conn = sqlite3.connect(archive.index_path(control))
    try:
        conn.execute(f"PRAGMA user_version = {index._SCHEMA_VERSION + 1}")
        conn.commit()
    finally:
        conn.close()


def test_archive_quest_stamps_both_floors_to_this_writers_versions(
    tmp_path: Path,
) -> None:
    _seed_change(tmp_path, "alpha", "2026-06-01T00:00:01+00:00")
    _stamp_floors_ahead(tmp_path)

    _seed_change(tmp_path, "beta", "2026-06-02T00:00:02+00:00")

    db = archive.index_path(tmp_path)
    assert _rows_by_id(db)["beta"] == "2026-06-02T00:00:02+00:00"
    assert (archive.data_version_path(tmp_path).read_text().strip()
            == str(archive._DATA_VERSION))
    assert _user_version(db) == index._SCHEMA_VERSION


def _seed_legacy_id_ns_change(control: Path, cid: str, last_ts: str) -> None:
    """Archive one change whose events blob still carries the pre-v7 "event" id_ns."""
    events = control / f"{cid}.jsonl"
    events.write_text(json.dumps({
        "id": f"{cid}-e0", "timestamp": last_ts, "type": "tool_result",
        "payload": {"id": "t1", "id_ns": "event", "content": "HEAL_TRACE"}}) + "\n")
    quest_json = control / f"{cid}-quest.json"
    quest_json.write_text(json.dumps({
        "id": cid, "title": cid.title(), "base_branch": "main",
        "created_at": "2026-06-01T00:00:00+00:00", "depends_on": []}))
    state_json = control / f"{cid}-state.json"
    state_json.write_text(json.dumps({
        "id": cid, "status": "merged", "step": "finalize"}))
    archive.archive_quest(
        control, cid, events_path=events,
        quest_json_path=quest_json, state_json_path=state_json)


def _tool_result_id_ns(blob: bytes) -> str | None:
    for line in blob.decode().splitlines():
        event = json.loads(line)
        if event.get("type") == "tool_result":
            return event["payload"].get("id_ns")
    return None


def test_an_older_writers_index_row_is_rebuilt_by_the_next_read(tmp_path: Path) -> None:
    """An older imp knows a lower index schema; its write lowers the floor, so the next
    current read rebuilds instead of trusting the row it left behind."""
    _seed_change(tmp_path, "alpha", "2026-06-01T00:00:01+00:00")
    db = archive.index_path(tmp_path)
    schema_now = index._SCHEMA_VERSION

    # A scoped MonkeyPatch, not the `monkeypatch` fixture: undoing that fixture
    # mid-test would also undo conftest's autouse IMP_HOME isolation, moving the
    # archive root out from under the assertions below.
    with pytest.MonkeyPatch.context() as older_imp:
        older_imp.setattr(index, "_SCHEMA_VERSION", schema_now - 1)
        _seed_prose_change(tmp_path, "beta", "the 300s deadline is enforced")

    assert _user_version(db) == schema_now - 1

    # events_content is the column a genuine v4 ingest would not have written
    # (see _write_stale_v4_db above): blank it, as that older writer leaves it.
    conn = sqlite3.connect(db)
    try:
        conn.execute("UPDATE quests SET events_content = NULL WHERE id = 'beta'")
        conn.commit()
    finally:
        conn.close()

    ids = {r[0] for r in read.completed_rows(tmp_path)}

    assert ids == {"alpha", "beta"}
    assert _user_version(db) == schema_now
    conn = sqlite3.connect(db)
    try:
        events_content = conn.execute(
            "SELECT events_content FROM quests WHERE id = 'beta'").fetchone()[0]
    finally:
        conn.close()
    assert "300s" in events_content


def test_an_older_writers_record_is_healed_by_the_next_read(tmp_path: Path) -> None:
    """Both floors lowered: the next current read re-migrates the blob and rebuilds."""
    _seed_change(tmp_path, "alpha", "2026-06-01T00:00:01+00:00")
    db = archive.index_path(tmp_path)
    data_now, schema_now = archive._DATA_VERSION, index._SCHEMA_VERSION

    with pytest.MonkeyPatch.context() as older_imp:
        older_imp.setattr(archive, "_DATA_VERSION", data_now - 1)
        older_imp.setattr(index, "_SCHEMA_VERSION", schema_now - 1)
        _seed_legacy_id_ns_change(tmp_path, "beta", "2026-06-02T00:00:02+00:00")

    assert (archive.data_version_path(tmp_path).read_text().strip()
            == str(data_now - 1))
    assert _user_version(db) == schema_now - 1

    ids = {r[0] for r in read.completed_rows(tmp_path)}

    rec = record.read_record(archive.records_root(tmp_path), "beta")
    assert rec is not None
    blob = LocalBlobStore(archive.blobs_root(tmp_path)).get(rec["events_hash"])
    assert blob is not None
    assert _tool_result_id_ns(blob) == "block"
    assert ids == {"alpha", "beta"}
    assert (archive.data_version_path(tmp_path).read_text().strip() == str(data_now))
    assert _user_version(db) == schema_now
    conn = sqlite3.connect(db)
    try:
        row = conn.execute(
            "SELECT last_event_ts, events_content FROM quests"
            " WHERE id = 'beta'").fetchone()
    finally:
        conn.close()
    assert row[0] == "2026-06-02T00:00:02+00:00"
    assert "HEAL_TRACE" in row[1]
