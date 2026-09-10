from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

from imp.engine import archive, durable
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


def test_data_version_bumped_to_eleven() -> None:
    assert archive._DATA_VERSION == 11


def test_write_data_version_durably_replaces_exact_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = archive.data_version_path(tmp_path)
    calls: list[tuple[str, Path]] = []
    real_fsync_file = durable._fsync_file
    real_replace = durable.os.replace
    real_fsync_directory = durable.fsync_directory

    def fsync_file(stream, path: Path) -> None:
        calls.append(("file", path.parent))
        real_fsync_file(stream, path)

    def replace(src, target) -> None:
        calls.append(("replace", Path(target)))
        real_replace(src, target)

    def fsync_directory(path: Path) -> None:
        calls.append(("directory", path))
        real_fsync_directory(path)

    monkeypatch.setattr(durable, "_fsync_file", fsync_file)
    monkeypatch.setattr(durable.os, "replace", replace)
    monkeypatch.setattr(durable, "fsync_directory", fsync_directory)

    archive._write_data_version(tmp_path, 12)

    assert (calls, marker.read_bytes()) == (
        [
            ("file", marker.parent),
            ("replace", marker),
            ("directory", marker.parent),
        ],
        b"12",
    )


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
    assert archive.data_version_path(tmp_path).read_text().strip() == str(
        archive._DATA_VERSION)


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
    assert archive.data_version_path(tmp_path).read_text().strip() == str(
        archive._DATA_VERSION)


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


_HEAD_HISTORY = {
    "plan-writer": [
        {"lap": 1, "outcome": "wrote plan-1", "head": "aaaa1111bbbb2222"},
        {"lap": 2, "outcome": "revised plan-1", "head": "cccc3333dddd4444"},
    ],
    "plan-reviewer": [{"lap": 1, "outcome": "sent back", "head": "eeee5555ffff6666"}],
}


def _seed_head_bearing_record(control: Path, cid: str) -> None:
    """An archived record whose final_state.outcome_history entries still carry the
    pre-removal `head` sha — the shape 12 of this install's 29 records hold."""
    store = LocalBlobStore(archive.blobs_root(control))
    events_hash = store.put(_jsonl(_OLD))
    rec = record.build_record(
        {"id": cid, "title": "", "base_branch": "main",
         "created_at": "2026-06-01T00:00:00+00:00", "depends_on": []},
        {"id": cid, "status": "merged", "step": "finalize",
         "vars": {"plan_index": 2},
         "outcome_history": json.loads(json.dumps(_HEAD_HISTORY))},
        events_hash)
    record.write_record(archive.records_root(control), rec)
    archive.rebuild_index(control)


def test_migrate_strips_head_from_outcome_history(tmp_path: Path) -> None:
    _seed_head_bearing_record(tmp_path, "oh01")

    archive.ensure_current(tmp_path)

    fs = _final_state_of(tmp_path, "oh01")
    assert fs["outcome_history"] == {
        "plan-writer": [
            {"lap": 1, "outcome": "wrote plan-1"},
            {"lap": 2, "outcome": "revised plan-1"},
        ],
        "plan-reviewer": [{"lap": 1, "outcome": "sent back"}],
    }
    assert fs["status"] == "merged"                  # nothing else disturbed
    assert fs["vars"] == {"plan_index": 2}
    assert archive.data_version_path(tmp_path).read_text().strip() == str(
        archive._DATA_VERSION)


def test_stripping_outcome_history_head_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_head_bearing_record(tmp_path, "oh02")
    archive._migrate_archive_blobs(tmp_path)

    # write_record is sort_keys-stable, so equal bytes would not prove the record
    # was left alone — count the writes instead.
    writes: list[str] = []
    real_write = record.write_record

    def _spy(records_dir: Path, rec: dict) -> Path:
        writes.append(rec["id"])
        return real_write(records_dir, rec)

    monkeypatch.setattr(record, "write_record", _spy)

    archive._migrate_archive_blobs(tmp_path)         # forced re-run, marker bypassed

    assert writes == []                              # nothing left to change
    fs = _final_state_of(tmp_path, "oh02")
    assert all("head" not in e
               for laps in fs["outcome_history"].values() for e in laps)
    assert [e["lap"] for e in fs["outcome_history"]["plan-writer"]] == [1, 2]


# 9, spelled out: the marker every existing install carries today. Without the bump
# it equals _DATA_VERSION, ensure_current fast-paths, and every such archive skips
# the strip entirely.
_HEAD_ERA_DATA_VERSION = 9


def test_an_archive_at_the_head_era_version_still_gets_stripped(
    tmp_path: Path,
) -> None:
    _seed_head_bearing_record(tmp_path, "oh03")
    archive.data_version_path(tmp_path).write_text(str(_HEAD_ERA_DATA_VERSION))

    archive.ensure_current(tmp_path)

    fs = _final_state_of(tmp_path, "oh03")
    assert fs["outcome_history"]["plan-reviewer"] == [{"lap": 1, "outcome": "sent back"}]
    assert all("head" not in e
               for laps in fs["outcome_history"].values() for e in laps)
    assert archive.data_version_path(tmp_path).read_text().strip() == str(
        archive._DATA_VERSION)


_EVENT_KEYED = [
    {"id": "t0", "parent_id": None, "agent_id": None,
     "timestamp": "2026-06-02T00:00:00+00:00",
     "type": "quest_created", "payload": {"id": "x", "title": ""}},
    {"id": "t1", "parent_id": None, "agent_id": "A",
     "timestamp": "2026-06-02T03:00:00+00:00",
     "type": "user_message",
     "payload": {"role": "user", "subtype": "intent",
                 "content": [{"type": "text", "text": "hi"}]}},
    {"id": "t2", "parent_id": None, "agent_id": "A",
     "timestamp": "2026-06-02T03:00:01+00:00",
     "type": "tool_call",
     "payload": {"id": "item_1", "name": "sub_agent",
                 "input": {"subagent_name": "explorer", "task_description": "x"}}},
    {"id": "t3", "parent_id": "t2", "agent_id": "A",
     "timestamp": "2026-06-02T03:00:09+00:00",
     "type": "tool_result",
     "payload": {"id": "item_1", "id_ns": "event", "content": "done", "error": False}},
]


def test_migrate_restamps_event_keyed_task_folds_and_repoints_the_hash(
    tmp_path: Path,
) -> None:
    old_hash = _seed_old_archived_quest(tmp_path, "ns001", _EVENT_KEYED)

    archive.ensure_current(tmp_path)

    rec = record.read_record(archive.records_root(tmp_path), "ns001")
    assert rec is not None
    assert rec["events_hash"] != old_hash          # blob rewritten, record repointed
    migrated = _events_of(tmp_path, "ns001")
    assert [e["payload"].get("id_ns") for e in migrated] == [
        None, None, "block", "block"]
    assert archive.data_version_path(tmp_path).read_text().strip() == str(
        archive._DATA_VERSION)


def test_blob_with_nothing_to_migrate_keeps_its_events_hash(tmp_path: Path) -> None:
    # A triad blob with no id_ns anywhere: both transforms are no-ops, so the record
    # must still point at the original blob.
    old_hash = _seed_old_archived_quest(tmp_path, "ns002", _TRIAD)

    archive.ensure_current(tmp_path)

    rec = record.read_record(archive.records_root(tmp_path), "ns002")
    assert rec is not None
    assert rec["events_hash"] == old_hash


# A triad-shaped blob (migrate_events is a no-op on it) whose query_result rows
# predate the session_id field: two cumulative rows, a step_started that ends the
# series, then a third row opening a new one.
_PRE_SESSION_ID = [
    {"id": "q1", "parent_id": None, "agent_id": "step:build/coder",
     "timestamp": "2026-06-02T03:00:00+00:00", "type": "query_result",
     "payload": {"total_cost_usd": 3.00, "usage": {}, "model_usage": {}}},
    {"id": "q2", "parent_id": None, "agent_id": "step:build/coder",
     "timestamp": "2026-06-02T03:01:00+00:00", "type": "query_result",
     "payload": {"total_cost_usd": 12.50, "usage": {}, "model_usage": {}}},
    {"id": "s1", "parent_id": None, "agent_id": None,
     "timestamp": "2026-06-02T03:02:00+00:00", "type": "step_started",
     "payload": {"phase": "build", "step": "coder", "provider": "claude_code",
                 "model": "claude-sonnet-4-6"}},
    {"id": "q3", "parent_id": None, "agent_id": "step:build/coder",
     "timestamp": "2026-06-02T03:03:00+00:00", "type": "query_result",
     "payload": {"total_cost_usd": 6.40, "usage": {}, "model_usage": {}}},
]


def _session_costs(control: Path, cid: str) -> dict[str, float]:
    """Cumulative rows collapse to the last one per session — what Part 4 sums."""
    last: dict[str, float] = {}
    for e in _events_of(control, cid):
        if e["type"] == "query_result":
            last[e["payload"]["session_id"]] = e["payload"]["total_cost_usd"]
    return last


def test_migrate_backfills_query_result_session_ids_in_the_blob(
    tmp_path: Path,
) -> None:
    old_hash = _seed_old_archived_quest(tmp_path, "qr001", _PRE_SESSION_ID)

    archive.ensure_current(tmp_path)

    rec = record.read_record(archive.records_root(tmp_path), "qr001")
    assert rec is not None
    assert rec["events_hash"] != old_hash        # blob rewritten, record repointed
    events = _events_of(tmp_path, "qr001")
    ids = [e["payload"]["session_id"]
           for e in events if e["type"] == "query_result"]
    assert ids[0] == ids[1]                      # one cumulative series
    assert ids[1] != ids[2]                      # the step_started closed it
    # 12.50 + 6.40 — the last row of each session, not the naive 21.90
    assert round(sum(_session_costs(tmp_path, "qr001").values()), 2) == 18.90
    # session_id is the ONLY key added, on the archive side as on the live one:
    # spec §4 leaves auth mode unrecoverable and deliberately unbackfilled.
    assert set(events[0]["payload"]) == {
        "session_id", "total_cost_usd", "usage", "model_usage"}
    assert events[2]["payload"] == {**_PRE_SESSION_ID[2]["payload"], "round": 1}
    assert archive.data_version_path(tmp_path).read_text().strip() == str(
        archive._DATA_VERSION)


def test_a_forced_second_pass_leaves_the_backfilled_blob_alone(
    tmp_path: Path,
) -> None:
    _seed_old_archived_quest(tmp_path, "qr002", _PRE_SESSION_ID)
    archive.ensure_current(tmp_path)
    first = record.read_record(archive.records_root(tmp_path), "qr002")
    assert first is not None

    archive._migrate_archive_blobs(tmp_path)     # forced re-run, marker bypassed

    again = record.read_record(archive.records_root(tmp_path), "qr002")
    assert again is not None
    assert again["events_hash"] == first["events_hash"]   # no second rewrite
    assert len(set(_session_costs(tmp_path, "qr002"))) == 2


# 10, spelled out: the marker every existing install already carries. Deriving it as
# _DATA_VERSION - 1 would sit below the constant whatever the constant is, so the
# test would pass with the bump reverted — the literal is what pins it.
_PREV_DATA_VERSION = 10


def _started(round_: int | None = None, eid: str = "ss1") -> dict:
    payload: dict[str, object] = {
        "step": "plan-reviewer", "kind": "agent", "phase": "planning",
        "label": "reviewing plan"}
    if round_ is not None:
        payload["round"] = round_
    return {"id": eid, "parent_id": None, "agent_id": None,
            "timestamp": "2026-06-02T04:00:00+00:00",
            "type": "step_started", "payload": payload}


def test_an_archive_at_the_previous_data_version_still_gets_rounds_stamped(
    tmp_path: Path,
) -> None:
    # The steady state of every existing install: the marker is ALREADY stamped at
    # the old version. Without the bump it equals _DATA_VERSION, ensure_current
    # fast-paths, and every such archive silently skips the pass.
    _seed_old_archived_quest(
        tmp_path, "rnd01", [*_TRIAD, _started(eid="a1"), _started(eid="a2")])
    archive.data_version_path(tmp_path).write_text(str(_PREV_DATA_VERSION))

    archive.ensure_current(tmp_path)

    assert [e["payload"]["round"] for e in _events_of(tmp_path, "rnd01")
            if e["type"] == "step_started"] == [1, 1]
    assert archive.data_version_path(tmp_path).read_text().strip() == str(
        archive._DATA_VERSION)


def test_archiving_a_post_change_quest_keeps_its_real_rounds(tmp_path: Path) -> None:
    # archive_quest writes the record BEFORE calling ensure_current, so on an install
    # whose marker is stale the chain re-runs over the just-written blob. An
    # unconditional stamp would flatten these to 1.
    _seed_old_archived_quest(tmp_path, "rnd02", _TRIAD)
    archive.data_version_path(tmp_path).write_text(str(_PREV_DATA_VERSION))
    events = tmp_path / "live.jsonl"
    events.write_text("".join(json.dumps(e) + "\n" for e in [
        _started(1, "b1"), _started(2, "b2"), _started(3, "b3")]))
    quest_json = tmp_path / "live-quest.json"
    quest_json.write_text(json.dumps({
        "id": "rnd03", "title": "Live", "base_branch": "main",
        "created_at": "2026-06-02T00:00:00+00:00", "depends_on": []}))
    state_json = tmp_path / "live-state.json"
    state_json.write_text(json.dumps({
        "id": "rnd03", "status": "completed", "step": "finalize"}))

    archive.archive_quest(
        tmp_path, "rnd03", events_path=events,
        quest_json_path=quest_json, state_json_path=state_json)

    assert [e["payload"]["round"] for e in _events_of(tmp_path, "rnd03")
            if e["type"] == "step_started"] == [1, 2, 3]


def test_ensure_current_backfills_a_null_provider_pin(tmp_path: Path) -> None:
    store = LocalBlobStore(archive.blobs_root(tmp_path))
    events_hash = store.put(_jsonl(_OLD))
    rec = record.build_record(
        {"id": "pv01", "title": "", "base_branch": "main",
         "created_at": "2026-06-01T00:00:00+00:00", "depends_on": [],
         "workflow": "default"},
        {"id": "pv01", "status": "merged", "step": "finalize"},
        events_hash)
    record.write_record(archive.records_root(tmp_path), rec)
    archive.rebuild_index(tmp_path)
    assert not archive.data_version_path(tmp_path).exists()   # pre-migration

    archive.ensure_current(tmp_path)

    migrated = record.read_record(archive.records_root(tmp_path), "pv01")
    assert migrated is not None
    # Present and null, so a rehydrated home has the same shape as a live one and
    # readers need no fallback.
    assert "provider" in migrated["quest_meta"]
    assert migrated["quest_meta"]["provider"] is None
    assert archive.data_version_path(tmp_path).read_text().strip() == str(
        archive._DATA_VERSION)


def test_an_archive_at_the_previous_data_version_gets_the_pin_backfilled(
    tmp_path: Path,
) -> None:
    # The steady state of every existing install: the marker is ALREADY stamped at
    # the old version. Without the bump it equals _DATA_VERSION, ensure_current
    # fast-paths, and every such archive silently skips the pass.
    store = LocalBlobStore(archive.blobs_root(tmp_path))
    events_hash = store.put(_jsonl(_OLD))
    rec = record.build_record(
        {"id": "pv03", "title": "", "base_branch": "main",
         "created_at": "2026-06-01T00:00:00+00:00", "depends_on": [],
         "workflow": "default"},
        {"id": "pv03", "status": "merged", "step": "finalize"},
        events_hash)
    record.write_record(archive.records_root(tmp_path), rec)
    archive.rebuild_index(tmp_path)
    archive.data_version_path(tmp_path).write_text(str(_PREV_DATA_VERSION))

    archive.ensure_current(tmp_path)

    migrated = record.read_record(archive.records_root(tmp_path), "pv03")
    assert migrated is not None
    assert migrated["quest_meta"]["provider"] is None
    assert archive.data_version_path(tmp_path).read_text().strip() == str(
        archive._DATA_VERSION)


def test_an_archived_pin_survives_the_hop(tmp_path: Path) -> None:
    store = LocalBlobStore(archive.blobs_root(tmp_path))
    events_hash = store.put(_jsonl(_OLD))
    rec = record.build_record(
        {"id": "pv02", "title": "", "base_branch": "main",
         "created_at": "2026-06-01T00:00:00+00:00", "depends_on": [],
         "workflow": "default", "provider": "codex"},
        {"id": "pv02", "status": "merged", "step": "finalize"},
        events_hash)
    record.write_record(archive.records_root(tmp_path), rec)
    archive.rebuild_index(tmp_path)

    archive.ensure_current(tmp_path)

    migrated = record.read_record(archive.records_root(tmp_path), "pv02")
    assert migrated is not None
    assert migrated["quest_meta"]["provider"] == "codex"
