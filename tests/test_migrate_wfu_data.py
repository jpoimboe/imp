import importlib.util
import json
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "migrate_wfu_data",
    Path(__file__).resolve().parents[1] / "scripts" / "migrate_wfu_data.py",
)
mig = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mig)


def _call(cid, ts="t1"):
    return {"id": cid, "agent_id": "a", "timestamp": ts, "type": "tool_call",
            "payload": {"name": "wait_for_user", "id": cid}}


def _result(cid, reply, ts="t2", error=False):
    return {"id": "r" + cid, "agent_id": "a", "timestamp": ts, "type": "tool_result",
            "payload": {"id": cid, "error": error,
                        "content": [{"type": "text", "text": reply}]}}


def test_resolved_gate_maps_to_awaiting_then_user_message():
    events = [_call("X"), _result("X", "yes")]
    out, n = mig.migrate_event_lines(events)
    assert n == 1
    assert out[0]["type"] == "awaiting_user_input"
    assert out[0]["payload"] == {"prompt": ""}
    assert out[0]["agent_id"] == "a" and out[0]["timestamp"] == "t1"
    assert out[1]["type"] == "user_message"
    assert out[1]["payload"] == {"role": "user", "content": [{"type": "text", "text": "yes"}]}


def test_dangling_gate_maps_to_awaiting_only():
    events = [_call("X")]
    out, n = mig.migrate_event_lines(events)
    assert n == 1
    assert [e["type"] for e in out] == ["awaiting_user_input"]


def test_non_wfu_events_untouched():
    other = {"type": "step_started", "payload": {"x": 1}}
    out, n = mig.migrate_event_lines([other])
    assert n == 0 and out == [other]


def test_find_unsafe_flags_error_and_interleaved_but_not_dangling():
    interleaving = {"type": "tool_call", "payload": {"name": "bash", "id": "B"}}
    events = [_call("X"), interleaving, _result("X", "hi"),   # interleaved -> unsafe
              _call("Y"), _result("Y", "bad", error=True),    # error -> unsafe
              _call("Z")]                                      # dangling -> safe
    assert sorted(mig.find_unsafe_events(events)) == ["X", "Y"]


def test_migrate_change_events_file_dry_run_then_apply(tmp_path):
    p = tmp_path / "events.jsonl"
    p.write_text(json.dumps(_call("X")) + "\n" + json.dumps(_result("X", "ok")) + "\n")
    before = p.read_text()
    assert mig.migrate_change_events_file(p, apply=False) == 1
    assert p.read_text() == before  # dry-run leaves the file untouched
    assert mig.migrate_change_events_file(p, apply=True) == 1
    lines = [json.loads(line) for line in p.read_text().splitlines() if line.strip()]
    assert [line["type"] for line in lines] == ["awaiting_user_input", "user_message"]
    assert mig.migrate_change_events_file(p, apply=True) == 0  # idempotent


def _assistant(*blocks):
    return {"role": "assistant", "content": list(blocks)}


def _tool_use(tid, name="wait_for_user"):
    return {"type": "tool_use", "id": tid, "name": name, "input": {}}


def _user_result(tid, reply):
    return {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": tid,
         "content": [{"type": "text", "text": reply}]}]}


def test_transcript_dangling_drops_tool_use_keeps_text():
    msgs = [_assistant({"type": "text", "text": "what now?"}, _tool_use("X"))]
    out, n = mig.migrate_transcript_lines(msgs)
    assert n == 1
    assert out == [_assistant({"type": "text", "text": "what now?"})]


def test_transcript_empty_assistant_turn_is_dropped():
    msgs = [_assistant({"type": "text", "text": "ask"}), _assistant(_tool_use("X"))]
    out, n = mig.migrate_transcript_lines(msgs)
    assert n == 1
    assert out == [_assistant({"type": "text", "text": "ask"})]


def test_transcript_resolved_rewrites_result_to_user_text():
    msgs = [_assistant({"type": "text", "text": "q"}, _tool_use("X")),
            _user_result("X", "the reply")]
    out, n = mig.migrate_transcript_lines(msgs)
    assert n == 1
    assert out[0] == _assistant({"type": "text", "text": "q"})
    assert out[1] == {"role": "user", "content": [{"type": "text", "text": "the reply"}]}


def test_transcript_without_wfu_untouched():
    msgs = [_assistant({"type": "text", "text": "hi"}, _tool_use("X", name="bash"))]
    out, n = mig.migrate_transcript_lines(msgs)
    assert n == 0 and out == msgs


def test_migrate_transcripts_under_walks_and_rewrites(tmp_path):
    steps = tmp_path / "steps" / "plan" / "plan-writer"
    steps.mkdir(parents=True)
    t = steps / "transcript.jsonl"
    t.write_text(json.dumps(_assistant({"type": "text", "text": "q"}, _tool_use("X"))) + "\n")
    assert mig.migrate_transcripts_under(tmp_path / "steps", apply=False) == 1
    assert "wait_for_user" in t.read_text()  # dry-run untouched
    assert mig.migrate_transcripts_under(tmp_path / "steps", apply=True) == 1
    assert "wait_for_user" not in t.read_text()
    assert mig.migrate_transcripts_under(tmp_path / "steps", apply=True) == 0


def test_migrate_transcripts_under_missing_dir_is_zero(tmp_path):
    assert mig.migrate_transcripts_under(tmp_path / "nope", apply=True) == 0


def test_migrate_archive_repoints_blob_and_reindexes(tmp_path):
    from imp.engine import archive
    from imp.engine.archive import index, record
    from imp.engine.archive.blobstore import LocalBlobStore

    control = tmp_path
    store = LocalBlobStore(archive.blobs_root(control))
    events = [_call("X"), _result("X", "ok")]
    old_hash = store.put("".join(json.dumps(e) + "\n" for e in events).encode())
    rec = record.build_record(
        {"id": "abc123", "title": "t", "base_branch": "master",
         "created_at": "2026-01-01", "depends_on": []},
        {"status": "completed", "step": "done"}, old_hash)
    record.write_record(archive.records_root(control), rec)
    conn = index.connect(archive.index_path(control))
    index.ingest(conn, rec, events)
    conn.commit()
    conn.close()

    assert mig.migrate_archive(control, apply=True) == 1

    new_rec = record.read_record(archive.records_root(control), "abc123")
    assert new_rec["events_hash"] != old_hash
    new_blob = store.get(new_rec["events_hash"]).decode()
    assert "wait_for_user" not in new_blob
    assert '"awaiting_user_input"' in new_blob
    conn = index.connect(archive.index_path(control))
    n = conn.execute(
        "SELECT COUNT(*) FROM events WHERE type='tool_call'"
        " AND payload_json LIKE '%wait_for_user%'").fetchone()[0]
    conn.close()
    assert n == 0  # index regenerated from migrated blob
    assert mig.migrate_archive(control, apply=True) == 0  # idempotent


def test_migrate_aborts_on_unsafe(tmp_path):
    cdir = tmp_path / ".imp" / "changes" / "ch1"
    cdir.mkdir(parents=True)
    interleave = {"type": "tool_call", "payload": {"name": "bash", "id": "B"}}
    (cdir / "events.jsonl").write_text(
        "".join(json.dumps(e) + "\n" for e in [_call("X"), interleave, _result("X", "hi")]))
    rep = mig.migrate(tmp_path, apply=False)
    assert rep["unsafe"] == [("ch1", "X")]
    import pytest
    with pytest.raises(SystemExit):
        mig.migrate(tmp_path, apply=True)


def test_migrate_full_run_clears_live_events_and_transcripts(tmp_path):
    cdir = tmp_path / ".imp" / "changes" / "ch2"
    (cdir / "steps" / "s").mkdir(parents=True)
    (cdir / "events.jsonl").write_text(
        "".join(json.dumps(e) + "\n" for e in [_call("X"), _result("X", "ok")]))
    (cdir / "steps" / "s" / "transcript.jsonl").write_text(
        json.dumps(_assistant({"type": "text", "text": "q"}, _tool_use("X"))) + "\n")
    rep = mig.migrate(tmp_path, apply=True)
    assert rep["live_events"] == 1 and rep["live_transcripts"] == 1
    assert "wait_for_user" not in (cdir / "events.jsonl").read_text()
    assert "wait_for_user" not in (cdir / "steps" / "s" / "transcript.jsonl").read_text()


def test_migrate_skips_live_change(tmp_path, monkeypatch):
    cdir = tmp_path / ".imp" / "changes" / "live1"
    cdir.mkdir(parents=True)
    before = "".join(json.dumps(e) + "\n" for e in [_call("X"), _result("X", "ok")])
    (cdir / "events.jsonl").write_text(before)
    # Force the live-detection to report this change as running.
    monkeypatch.setattr(mig, "_live_change_ids", lambda root: {"live1"})
    rep = mig.migrate(tmp_path, apply=True)
    assert rep["skipped_live"] == ["live1"]
    assert rep["live_events"] == 0
    assert (cdir / "events.jsonl").read_text() == before  # untouched, no race
