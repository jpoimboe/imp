from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

from imp.engine import archive
from imp.engine.archive import index


class _FakeStderr:
    """Records writes; reports isatty() as configured. Stands in for sys.stderr."""

    def __init__(self, tty: bool) -> None:
        self._tty = tty
        self.chunks: list[str] = []

    def write(self, s: str) -> int:
        self.chunks.append(s)
        return len(s)

    def flush(self) -> None:
        pass

    def isatty(self) -> bool:
        return self._tty

    @property
    def text(self) -> str:
        return "".join(self.chunks)


def _archive_one(control: Path, cid: str, dep: str, etype: str) -> None:
    events = control / f"{cid}-events.jsonl"
    events.write_text(
        f'{{"id":"{cid}-e0","parent_id":null,"agent_id":null,'
        f'"timestamp":"2026-06-01T00:00:01+00:00","type":"{etype}",'
        f'"payload":{{}}}}\n')
    quest_json = control / f"{cid}-quest.json"
    quest_json.write_text(json.dumps({
        "id": cid, "title": cid.title(), "base_branch": "main",
        "created_at": "2026-06-01T00:00:00+00:00", "depends_on": [dep]}))
    state_json = control / f"{cid}-state.json"
    state_json.write_text(json.dumps({
        "id": cid, "status": "completed", "step": "finalize"}))
    archive.archive_quest(
        control, cid, events_path=events,
        quest_json_path=quest_json, state_json_path=state_json)


def _snapshot(conn) -> dict:
    return {
        "quests": sorted(conn.execute(
            "SELECT id, title, base, created_at, status, step, events_hash,"
            " last_event_ts FROM quests").fetchall()),
        "deps": sorted(conn.execute(
            "SELECT quest_id, depends_on_id FROM deps").fetchall()),
    }


def test_rebuild_reproduces_index_contents(tmp_path: Path) -> None:
    _archive_one(tmp_path, "alpha", "beta", "change_created")
    _archive_one(tmp_path, "gamma", "alpha", "change_merged")

    conn = index.connect(archive.index_path(tmp_path))
    try:
        before = _snapshot(conn)
    finally:
        conn.close()

    # Drop the derived index entirely, then rebuild from records + blobs.
    archive.index_path(tmp_path).unlink()
    archive.rebuild_index(tmp_path)

    conn = index.connect(archive.index_path(tmp_path))
    try:
        after = _snapshot(conn)
    finally:
        conn.close()

    assert after == before
    assert after["quests"]  # non-empty: the rebuild actually loaded rows
    assert all(row[7] for row in after["quests"])  # last_event_ts populated


def test_rebuild_keeps_original_rows_queryable_during_ingest(
    tmp_path: Path, monkeypatch,
) -> None:
    _archive_one(tmp_path, "alpha", "beta", "change_created")
    _archive_one(tmp_path, "gamma", "alpha", "change_merged")

    observed: dict[str, set[str]] = {}
    real_ingest = index.ingest

    def spy(conn, rec, events):
        # First time the rebuild ingests a row, query the LIVE index path with a
        # separate connection. The atomic rebuild builds into a temp db and only
        # os.replaces it at the very end, so the original committed rows must
        # still be fully visible on disk here.
        if "ids" not in observed:
            reader = sqlite3.connect(archive.index_path(tmp_path))
            try:
                observed["ids"] = {
                    r[0] for r in reader.execute("SELECT id FROM quests")}
            finally:
                reader.close()
        return real_ingest(conn, rec, events)

    monkeypatch.setattr(index, "ingest", spy)
    archive.rebuild_index(tmp_path)

    # Mid-rebuild, the live index still held BOTH original rows (not an empty,
    # just-recreated db).
    assert observed["ids"] == {"alpha", "gamma"}
    # No temp db is left behind in the archive dir after the swap.
    assert not list(archive.archive_root(tmp_path).glob("*.db.tmp"))
    # Final content reproduced from records + blobs.
    conn = index.connect(archive.index_path(tmp_path))
    try:
        ids = {r[0] for r in conn.execute("SELECT id FROM quests")}
    finally:
        conn.close()
    assert ids == {"alpha", "gamma"}


def test_rebuild_emits_dim_inplace_progress_on_tty(tmp_path: Path, monkeypatch) -> None:
    _archive_one(tmp_path, "alpha", "beta", "change_created")
    _archive_one(tmp_path, "gamma", "alpha", "change_merged")

    fake = _FakeStderr(tty=True)
    monkeypatch.setattr(sys, "stderr", fake)
    archive.rebuild_index(tmp_path)

    out = fake.text
    assert "Reindexing archive…" in out       # ellipsis-glyph progress text
    assert "\x1b[2m" in out and "\x1b[0m" in out    # dim SGR wrap
    assert "\r\x1b[K" in out                         # in-place rewrite idiom
    assert "2/2" in out                              # counts up to record total
    assert out.endswith("\r\x1b[K")                  # trailing erase after the loop


def test_rebuild_on_non_tty_emits_nothing_and_indexes(tmp_path: Path, monkeypatch) -> None:
    _archive_one(tmp_path, "alpha", "beta", "change_created")
    _archive_one(tmp_path, "gamma", "alpha", "change_merged")

    fake = _FakeStderr(tty=False)
    monkeypatch.setattr(sys, "stderr", fake)
    archive.index_path(tmp_path).unlink()
    archive.rebuild_index(tmp_path)

    assert fake.text == ""  # zero bytes on a non-TTY stream

    conn = index.connect(archive.index_path(tmp_path))
    try:
        ids = {r[0] for r in conn.execute("SELECT id FROM quests")}
        ts = {r[0] for r in conn.execute("SELECT last_event_ts FROM quests")}
    finally:
        conn.close()
    assert ids == {"alpha", "gamma"}   # index still correct/queryable
    assert all(ts)                     # last_event_ts populated


def test_rebuild_erases_progress_line_on_exception(tmp_path: Path, monkeypatch) -> None:
    _archive_one(tmp_path, "alpha", "beta", "change_created")
    _archive_one(tmp_path, "gamma", "alpha", "change_merged")

    fake = _FakeStderr(tty=True)
    monkeypatch.setattr(sys, "stderr", fake)

    def boom(conn, rec, events):
        raise KeyboardInterrupt

    monkeypatch.setattr(index, "ingest", boom)
    with pytest.raises(KeyboardInterrupt):
        archive.rebuild_index(tmp_path)

    # the transient line was erased by the finally before the exception propagated
    assert fake.text.endswith("\r\x1b[K")
