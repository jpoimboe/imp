from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

from imp.engine import archive
from imp.engine.archive import record
from imp.engine.archive.blobstore import LocalBlobStore


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


_INTENT = "Fix the login bug"
_FRAMEWORK = "You are the coder agent, follow the plan exactly"
_MODEL = "Investigating the login flow now"
_REPLY = "please retry the deploy step"

# A pre-triad (old-schema) event list: opening_intent + a step_context carrying a
# system_prompt + the human=False first-after-step_context user_message (the
# framework prompt) + an llm_response model turn + a human=True reply.
_OLD = [
    {"id": "i0", "parent_id": None, "agent_id": None, "timestamp": "T0",
     "type": "opening_intent", "payload": {"text": _INTENT}},
    {"id": "c1", "parent_id": None, "agent_id": "A", "timestamp": "T1",
     "type": "step_context",
     "payload": {"provider": "p", "model": "m", "system_prompt": "sys",
                 "tools_schema": [], "params": {}, "max_tool_calls": 1}},
    {"id": "p1", "parent_id": "c1", "agent_id": "A", "timestamp": "T2",
     "type": "user_message",
     "payload": {"role": "user", "content": [{"type": "text", "text": _FRAMEWORK}],
                 "human": False}},
    {"id": "r1", "parent_id": "p1", "agent_id": "A", "timestamp": "T3",
     "type": "llm_response",
     "payload": {"provider": "anthropic", "model": "m",
                 "raw": {"content": [{"type": "text", "text": _MODEL}]},
                 "usage": {"input_tokens": 1}, "stop_reason": "end_turn"}},
    {"id": "a1", "parent_id": "r1", "agent_id": "A", "timestamp": "T4",
     "type": "awaiting_user_input", "payload": {}},
    {"id": "u2", "parent_id": "a1", "agent_id": "A", "timestamp": "T5",
     "type": "user_message",
     "payload": {"role": "user", "content": [{"type": "text", "text": _REPLY}],
                 "human": True}},
]


def _jsonl(events: list[dict]) -> bytes:
    return "".join(json.dumps(e) + "\n" for e in events).encode("utf-8")


def _seed_old_archived_quest(control: Path, cid: str, events: list[dict]) -> str:
    """Construct a pre-migration archived quest directly (not via archive_quest,
    which would stamp the marker): an old-format blob, a record pointing at it, a
    rebuilt index — and NO data-version marker (the pre-migration state)."""
    store = LocalBlobStore(archive.blobs_root(control))
    events_hash = store.put(_jsonl(events))
    rec = record.build_record(
        {"id": cid, "title": "", "base_branch": "main",
         "created_at": "2026-06-01T00:00:00+00:00", "depends_on": []},
        {"id": cid, "status": "completed", "step": "finalize"},
        events_hash)
    record.write_record(archive.records_root(control), rec)
    archive.rebuild_index(control)
    return events_hash


def _events_of(control: Path, cid: str) -> list[dict]:
    rec = record.read_record(archive.records_root(control), cid)
    assert rec is not None
    blob = LocalBlobStore(archive.blobs_root(control)).get(rec["events_hash"])
    assert blob is not None
    return [json.loads(ln) for ln in blob.decode().splitlines() if ln.strip()]


def test_ensure_current_migrates_old_blob_and_bumps_marker(tmp_path: Path) -> None:
    old_hash = _seed_old_archived_quest(tmp_path, "old001", _OLD)
    assert not archive.data_version_path(tmp_path).exists()  # pre-migration

    archive.ensure_current(tmp_path)

    rec = record.read_record(archive.records_root(tmp_path), "old001")
    assert rec is not None
    assert rec["events_hash"] != old_hash        # blob rewritten, record repointed
    migrated = _events_of(tmp_path, "old001")
    assert [e["type"] for e in migrated] == [
        "user_message", "step_context", "engine_message", "engine_message",
        "agent_message", "awaiting_user_input", "user_message"]
    assert migrated[0]["payload"]["subtype"] == "intent"
    assert migrated[3]["payload"]["subtype"] == "agent_prompt"
    assert migrated[6]["payload"]["subtype"] == "reply"
    assert "human" not in migrated[6]["payload"]
    assert archive.data_version_path(tmp_path).read_text().strip() == str(
        archive._DATA_VERSION)


def test_second_ensure_current_is_a_no_op(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_old_archived_quest(tmp_path, "old002", _OLD)
    archive.ensure_current(tmp_path)
    first_hash = record.read_record(
        archive.records_root(tmp_path), "old002")["events_hash"]

    calls: list[int] = []
    monkeypatch.setattr(archive, "rebuild_index", lambda _root: calls.append(1))
    archive.ensure_current(tmp_path)

    assert calls == []                           # marker current -> fast path
    second_hash = record.read_record(
        archive.records_root(tmp_path), "old002")["events_hash"]
    assert second_hash == first_hash             # blob not rewritten a second time


def test_index_content_reflects_migrated_events(tmp_path: Path) -> None:
    _seed_old_archived_quest(tmp_path, "old003", _OLD)
    archive.ensure_current(tmp_path)

    conn = sqlite3.connect(archive.index_path(tmp_path))
    try:
        content = conn.execute(
            "SELECT content FROM quests WHERE id = ?", ("old003",)).fetchone()[0]
    finally:
        conn.close()
    assert _INTENT in content        # opening intent -> user_message, now rendered
    assert _MODEL in content         # llm_response -> agent_message, now rendered
    assert _FRAMEWORK not in content  # framework prompt -> engine_message, excluded


def test_archive_quest_stamps_marker_current(tmp_path: Path) -> None:
    # A quest archived through the normal path is born-current: the marker is
    # stamped so a later ensure_current fast-paths (no redundant blob pass).
    events = tmp_path / "born.jsonl"
    events.write_text(json.dumps({
        "id": "b-e0", "parent_id": None, "agent_id": None,
        "timestamp": "2026-06-01T00:00:01+00:00",
        "type": "user_message",
        "payload": {"role": "user", "subtype": "intent",
                    "content": [{"type": "text", "text": "hi"}]}}) + "\n")
    quest_json = tmp_path / "born-quest.json"
    quest_json.write_text(json.dumps({
        "id": "born", "title": "Born", "base_branch": "main",
        "created_at": "2026-06-01T00:00:00+00:00", "depends_on": []}))
    state_json = tmp_path / "born-state.json"
    state_json.write_text(json.dumps({
        "id": "born", "status": "completed", "step": "finalize"}))

    archive.archive_quest(
        tmp_path, "born", events_path=events,
        quest_json_path=quest_json, state_json_path=state_json)

    assert archive.data_version_path(tmp_path).read_text().strip() == str(
        archive._DATA_VERSION)
    assert archive._read_data_version(tmp_path) >= archive._DATA_VERSION


def test_migrate_emits_dim_inplace_progress_on_tty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_old_archived_quest(tmp_path, "old004", _OLD)
    _seed_old_archived_quest(tmp_path, "old005", _OLD)

    # Isolate the migration pass: no-op the reindex so only the migration
    # progress line is captured.
    monkeypatch.setattr(archive, "rebuild_index", lambda _root: None)
    fake = _FakeStderr(tty=True)
    monkeypatch.setattr(sys, "stderr", fake)

    archive._migrate_archive_blobs(tmp_path)

    out = fake.text
    assert "Migrating archive... 2/2" in out    # labeled counter, counts to record total
    assert "Migrating archive... 1/2" in out    # counter emitted per record, not just the last
    assert "\x1b[2m" in out and "\x1b[0m" in out  # dim SGR wrap
    assert "\r\x1b[K" in out                       # in-place rewrite idiom
    assert "Reindexing" not in out                 # migration pass only (reindex no-op'd)
    # final frame committed to scrollback, newline-terminated, no trailing erase
    assert out.endswith("\x1b[2mMigrating archive... 2/2\x1b[0m\n")


def test_migrate_on_non_tty_emits_nothing_and_migrates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    old_hash = _seed_old_archived_quest(tmp_path, "old006", _OLD)

    fake = _FakeStderr(tty=False)
    monkeypatch.setattr(sys, "stderr", fake)

    archive._migrate_archive_blobs(tmp_path)

    assert fake.text == ""  # zero bytes on a non-TTY stream

    rec = record.read_record(archive.records_root(tmp_path), "old006")
    assert rec is not None
    assert rec["events_hash"] != old_hash  # blob still rewritten / record repointed


# A triad-schema (already-migrated) event list: migrate_events is a no-op on it,
# so the migrated==events guard would `continue` — the started_at backfill must
# still run for it.
_TRIAD = [
    {"id": "t0", "parent_id": None, "agent_id": None, "timestamp": "2026-06-02T00:00:00+00:00",
     "type": "quest_created", "payload": {"id": "x", "title": ""}},
    {"id": "t1", "parent_id": None, "agent_id": "A", "timestamp": "2026-06-02T03:00:00+00:00",
     "type": "user_message",
     "payload": {"role": "user", "subtype": "intent",
                 "content": [{"type": "text", "text": "hi"}]}},
]


def _final_state_of(control: Path, cid: str) -> dict:
    rec = record.read_record(archive.records_root(control), cid)
    assert rec is not None
    return rec["final_state"]


def test_migrate_backfills_started_at_into_final_state(tmp_path: Path) -> None:
    _seed_old_archived_quest(tmp_path, "sa001", _OLD)

    archive.ensure_current(tmp_path)

    fs = _final_state_of(tmp_path, "sa001")
    # First user_message in the triad-migrated _OLD blob is the opening intent (T0).
    assert fs["started_at"] == "T0"
    assert archive.data_version_path(tmp_path).read_text().strip() == str(
        archive._DATA_VERSION)


def test_backfill_runs_for_already_triad_record_past_the_guards(tmp_path: Path) -> None:
    # An already-triad blob: migrate_events is a no-op (migrated == events), so the
    # existing guard would skip it. The started_at backfill must still land.
    _seed_old_archived_quest(tmp_path, "sa002", _TRIAD)

    archive._migrate_archive_blobs(tmp_path)

    assert _final_state_of(tmp_path, "sa002")["started_at"] == "2026-06-02T03:00:00+00:00"


def test_data_version_bumped_to_six() -> None:
    assert archive._DATA_VERSION == 6


def test_ensure_current_rewrites_imp_workflow_to_default(tmp_path: Path) -> None:
    store = LocalBlobStore(archive.blobs_root(tmp_path))
    events_hash = store.put(_jsonl(_OLD))
    rec = record.build_record(
        {"id": "wf01", "title": "", "base_branch": "main",
         "created_at": "2026-06-01T00:00:00+00:00", "depends_on": [],
         "workflow": "imp"},
        {"id": "wf01", "status": "merged", "step": "finalize"},
        events_hash)
    record.write_record(archive.records_root(tmp_path), rec)
    archive.rebuild_index(tmp_path)
    assert not archive.data_version_path(tmp_path).exists()   # pre-migration

    archive.ensure_current(tmp_path)

    migrated = record.read_record(archive.records_root(tmp_path), "wf01")
    assert migrated is not None
    assert migrated["quest_meta"]["workflow"] == "default"
    assert archive.data_version_path(tmp_path).read_text().strip() == "6"


def test_migrate_remaps_resume_target_prompt_to_prompt_paused(tmp_path: Path) -> None:
    store = LocalBlobStore(archive.blobs_root(tmp_path))
    events_hash = store.put(_jsonl(_OLD))
    rec = record.build_record(
        {"id": "rt1", "title": "", "base_branch": "main",
         "created_at": "2026-06-01T00:00:00+00:00", "depends_on": []},
        {"id": "rt1", "status": "completed", "step": "finalize",
         "resume_target": "prompt"},
        events_hash)
    record.write_record(archive.records_root(tmp_path), rec)
    archive.rebuild_index(tmp_path)

    archive._migrate_archive_blobs(tmp_path)

    assert _final_state_of(tmp_path, "rt1")["resume_target"] == "prompt-paused"
    assert archive.data_version_path(tmp_path).read_text().strip() == str(
        archive._DATA_VERSION)


def test_migrate_leaves_agent_resume_target_unchanged(tmp_path: Path) -> None:
    store = LocalBlobStore(archive.blobs_root(tmp_path))
    events_hash = store.put(_jsonl(_OLD))
    rec = record.build_record(
        {"id": "rt2", "title": "", "base_branch": "main",
         "created_at": "2026-06-01T00:00:00+00:00", "depends_on": []},
        {"id": "rt2", "status": "completed", "step": "finalize",
         "resume_target": "agent"},
        events_hash)
    record.write_record(archive.records_root(tmp_path), rec)
    archive.rebuild_index(tmp_path)

    archive._migrate_archive_blobs(tmp_path)

    assert _final_state_of(tmp_path, "rt2")["resume_target"] == "agent"


def test_ensure_current_leaves_non_imp_workflow_untouched(tmp_path: Path) -> None:
    store = LocalBlobStore(archive.blobs_root(tmp_path))
    events_hash = store.put(_jsonl(_OLD))
    rec = record.build_record(
        {"id": "wf02", "title": "", "base_branch": "main",
         "created_at": "2026-06-01T00:00:00+00:00", "depends_on": [],
         "workflow": "minimal"},
        {"id": "wf02", "status": "merged", "step": "finalize"},
        events_hash)
    record.write_record(archive.records_root(tmp_path), rec)
    archive.rebuild_index(tmp_path)

    archive.ensure_current(tmp_path)

    migrated = record.read_record(archive.records_root(tmp_path), "wf02")
    assert migrated is not None
    assert migrated["quest_meta"]["workflow"] == "minimal"


def test_ensure_current_leaves_null_workflow_untouched(tmp_path: Path) -> None:
    store = LocalBlobStore(archive.blobs_root(tmp_path))
    events_hash = store.put(_jsonl(_OLD))
    rec = record.build_record(
        {"id": "wf03", "title": "", "base_branch": "main",
         "created_at": "2026-06-01T00:00:00+00:00", "depends_on": [],
         "workflow": None},
        {"id": "wf03", "status": "merged", "step": "finalize"},
        events_hash)
    record.write_record(archive.records_root(tmp_path), rec)
    archive.rebuild_index(tmp_path)

    archive.ensure_current(tmp_path)

    migrated = record.read_record(archive.records_root(tmp_path), "wf03")
    assert migrated is not None
    assert migrated["quest_meta"]["workflow"] is None


def test_migrate_strips_project_facts_from_final_state(tmp_path: Path) -> None:
    from imp.engine.archive import index

    store = LocalBlobStore(archive.blobs_root(tmp_path))
    events_hash = store.put(_jsonl(_OLD))
    rec = record.build_record(
        {"id": "pf01", "title": "", "base_branch": "main",
         "created_at": "2026-06-01T00:00:00+00:00", "depends_on": []},
        {"id": "pf01", "status": "merged", "step": "finalize",
         "project_facts": {"stack": "python"}},
        events_hash)
    record.write_record(archive.records_root(tmp_path), rec)
    archive.rebuild_index(tmp_path)

    archive.ensure_current(tmp_path)

    fs = _final_state_of(tmp_path, "pf01")
    assert "project_facts" not in fs
    assert fs["status"] == "merged"                     # nothing else disturbed
    assert archive._read_user_version(tmp_path) >= index._SCHEMA_VERSION
    assert archive.data_version_path(tmp_path).read_text().strip() == "6"


def test_stripping_project_facts_is_idempotent(tmp_path: Path) -> None:
    store = LocalBlobStore(archive.blobs_root(tmp_path))
    events_hash = store.put(_jsonl(_OLD))
    rec = record.build_record(
        {"id": "pf02", "title": "", "base_branch": "main",
         "created_at": "2026-06-01T00:00:00+00:00", "depends_on": []},
        {"id": "pf02", "status": "merged", "step": "finalize",
         "project_facts": {"stack": "python"}},
        events_hash)
    record.write_record(archive.records_root(tmp_path), rec)
    archive.rebuild_index(tmp_path)

    archive._migrate_archive_blobs(tmp_path)
    archive._migrate_archive_blobs(tmp_path)            # re-run must be a no-op

    fs = _final_state_of(tmp_path, "pf02")
    assert "project_facts" not in fs
    assert fs["status"] == "merged"
