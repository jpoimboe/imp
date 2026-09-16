from __future__ import annotations

import json
from pathlib import Path

from imp.engine.archive.read import (
    completed_rows,
    merge_commit_of,
    read_quest_json,
    read_state_json,
    status_of,
)


def _events_bytes(cid: str) -> bytes:
    return (
        f'{{"id":"{cid}-e0","parent_id":null,"agent_id":null,'
        '"timestamp":"2026-06-01T00:00:01+00:00","type":"quest_created",'
        f'"payload":{{"id":"{cid}"}}}}\n'
        f'{{"id":"{cid}-e1","parent_id":"{cid}-e0","agent_id":"plan-writer",'
        '"timestamp":"2026-06-01T00:00:09+00:00","type":"step_started",'
        '"payload":{"step":"plan-writer"}}\n'
    ).encode()


def _archive(control: Path, cid: str, status: str) -> bytes:
    from imp.engine import archive

    events = control / f"{cid}.jsonl"
    events.write_bytes(_events_bytes(cid))
    quest_json = control / f"{cid}-quest.json"
    quest_json.write_text(json.dumps({
        "id": cid, "title": cid.title(), "base_branch": "main",
        "created_at": "2026-06-01T00:00:00+00:00", "depends_on": ["dep1"],
        "materialized": True}))
    state_json = control / f"{cid}-state.json"
    state_json.write_text(json.dumps({
        "id": cid, "status": status, "step": None, "merge_commit": "c0ffee"}))
    archive.archive_quest(
        control, cid, events_path=events,
        quest_json_path=quest_json, state_json_path=state_json)
    return events.read_bytes()


def test_read_state_and_quest_json_reconstruct_from_record(tmp_path: Path) -> None:
    _git_init(tmp_path)
    _archive(tmp_path, "alpha", "merged")
    st = json.loads(read_state_json(tmp_path, "alpha"))
    assert st["status"] == "merged"
    assert st["merge_commit"] == "c0ffee"
    cj = json.loads(read_quest_json(tmp_path, "alpha"))
    assert cj["title"] == "Alpha"
    assert cj["base_branch"] == "main"
    assert cj["created_at"] == "2026-06-01T00:00:00+00:00"
    assert cj["materialized"] is True


def test_completed_rows_single_query_carries_last_activity(tmp_path: Path) -> None:
    _git_init(tmp_path)
    _archive(tmp_path, "alpha", "merged")
    _archive(tmp_path, "zeta", "abandoned")
    rows = {r[0]: r for r in completed_rows(tmp_path)}
    # Row shape: (id, status, step, last_activity, title, created_at).
    assert rows["alpha"][1] == "merged"
    assert rows["zeta"][1] == "abandoned"
    assert rows["alpha"][2] == "-"                       # step None -> "-"
    assert rows["alpha"][3] == "2026-06-01T00:00:09+00:00"  # MAX(events.ts)
    assert rows["alpha"][4] == "Alpha"
    assert rows["alpha"][5] == "2026-06-01T00:00:00+00:00"


def _git_init(root: Path) -> None:
    import subprocess
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=root, check=True)


def test_status_of_returns_status_from_record(tmp_path: Path) -> None:
    _git_init(tmp_path)
    _archive(tmp_path, "alpha", "merged")
    assert status_of(tmp_path, "alpha") == "merged"


def test_status_of_returns_none_when_not_archived(tmp_path: Path) -> None:
    _git_init(tmp_path)
    assert status_of(tmp_path, "ghost") is None


def test_read_quest_json_prefers_stored_meta(tmp_path: Path) -> None:
    from imp.engine import archive
    from imp.engine.archive import read as archive_read

    events = tmp_path / "events.jsonl"
    events.write_text("")
    quest_json = tmp_path / "quest.json"
    quest_json.write_text(json.dumps({
        "id": "conv", "title": "Conv", "base_branch": "main",
        "created_at": "2026-06-01T00:00:00+00:00", "depends_on": [],
        "project_root": str(tmp_path), "materialized": False,
        "orig_base_head": "deadbeef", "layout_version": 2}))
    state_json = tmp_path / "state.json"
    state_json.write_text(json.dumps({"id": "conv", "status": "shelved", "step": "threshold"}))

    archive.archive_quest(
        tmp_path, "conv", events_path=events,
        quest_json_path=quest_json, state_json_path=state_json, resumable=True)

    raw = archive_read.read_quest_json(tmp_path, "conv")
    assert raw is not None
    meta = json.loads(raw)
    assert meta["materialized"] is False           # faithful: not hardcoded True
    assert meta["orig_base_head"] == "deadbeef"    # preserved for the head_changed check
    assert meta["layout_version"] == 2


def test_read_quest_json_falls_back_for_legacy_record(tmp_path: Path) -> None:
    from imp.engine import archive
    from imp.engine.archive import record, records_root

    legacy_rec = {
        "id": "old", "title": "Old", "base": "main",
        "created_at": "2026-06-01T00:00:00+00:00", "depends_on": [],
        "final_state": {"id": "old", "status": "merged"},
        "events_hash": "abc",
    }
    record.write_record(records_root(tmp_path), legacy_rec)
    archive._write_data_version(tmp_path, archive._DATA_VERSION)
    raw = read_quest_json(tmp_path, "old")
    assert raw is not None
    meta = json.loads(raw)
    assert meta["id"] == "old"
    assert meta["title"] == "Old"
    assert meta["base_branch"] == "main"
    assert meta["materialized"] is True
    assert "quest_meta" not in meta


def test_read_step_blobs_returns_relpath_to_bytes(tmp_path: Path) -> None:
    from imp.engine import archive
    from imp.engine.archive.read import read_step_blobs

    events = tmp_path / "e.jsonl"
    events.write_bytes(b"")
    quest_json = tmp_path / "c.json"
    quest_json.write_text(json.dumps(
        {"id": "conv", "title": "C", "base_branch": "main"}))
    state_json = tmp_path / "st.json"
    state_json.write_text(json.dumps({"id": "conv", "status": "shelved"}))
    steps = tmp_path / "steps"
    (steps / "p" / "s").mkdir(parents=True)
    (steps / "p" / "s" / "transcript.jsonl").write_bytes(b"LINE\n")

    archive.archive_quest(
        tmp_path, "conv", events_path=events,
        quest_json_path=quest_json, state_json_path=state_json,
        steps_dir=steps, resumable=True)

    assert read_step_blobs(tmp_path, "conv") == {"p/s/transcript.jsonl": b"LINE\n"}


def test_read_step_blobs_empty_when_no_record(tmp_path: Path) -> None:
    from imp.engine.archive.read import read_step_blobs

    assert read_step_blobs(tmp_path, "ghost") == {}


def test_read_step_blobs_skips_missing_blob(tmp_path: Path) -> None:
    from imp.engine import archive
    from imp.engine.archive import record, records_root
    from imp.engine.archive.read import read_step_blobs

    events = tmp_path / "e.jsonl"
    events.write_bytes(b"")
    quest_json = tmp_path / "c.json"
    quest_json.write_text(json.dumps(
        {"id": "conv", "title": "C", "base_branch": "main"}))
    state_json = tmp_path / "st.json"
    state_json.write_text(json.dumps({"id": "conv", "status": "shelved"}))
    steps = tmp_path / "steps"
    (steps / "p" / "s").mkdir(parents=True)
    (steps / "p" / "s" / "transcript.jsonl").write_bytes(b"LINE\n")

    archive.archive_quest(
        tmp_path, "conv", events_path=events,
        quest_json_path=quest_json, state_json_path=state_json,
        steps_dir=steps, resumable=True)

    rec = record.read_record(records_root(tmp_path), "conv")
    rec["step_blobs"] = {"p/s/transcript.jsonl": "0" * 64}
    record.write_record(records_root(tmp_path), rec)

    assert read_step_blobs(tmp_path, "conv") == {}


def test_read_step_blobs_empty_when_not_resumable(tmp_path: Path) -> None:
    from imp.engine import archive
    from imp.engine.archive.read import read_step_blobs

    events = tmp_path / "e.jsonl"
    events.write_bytes(b"")
    quest_json = tmp_path / "c.json"
    quest_json.write_text(json.dumps(
        {"id": "conv", "title": "C", "base_branch": "main"}))
    state_json = tmp_path / "st.json"
    state_json.write_text(json.dumps({"id": "conv", "status": "shelved"}))
    steps = tmp_path / "steps"
    (steps / "p" / "s").mkdir(parents=True)
    (steps / "p" / "s" / "transcript.jsonl").write_bytes(b"LINE\n")

    archive.archive_quest(
        tmp_path, "conv", events_path=events,
        quest_json_path=quest_json, state_json_path=state_json,
        steps_dir=steps, resumable=False)

    assert read_step_blobs(tmp_path, "conv") == {}


def test_merge_commit_of_reads_recorded_sha(tmp_path: Path) -> None:
    _git_init(tmp_path)
    _archive(tmp_path, "alpha", "merged")
    assert merge_commit_of(tmp_path, "alpha") == "c0ffee"


def test_merge_commit_of_none_when_absent(tmp_path: Path) -> None:
    from imp.engine import archive

    _git_init(tmp_path)
    events = tmp_path / "beta.jsonl"
    events.write_bytes(_events_bytes("beta"))
    quest_json = tmp_path / "beta-quest.json"
    quest_json.write_text(json.dumps({
        "id": "beta", "title": "Beta", "base_branch": "main",
        "created_at": "2026-06-01T00:00:00+00:00", "depends_on": []}))
    state_json = tmp_path / "beta-state.json"
    state_json.write_text(json.dumps({"id": "beta", "status": "merged", "step": None}))
    archive.archive_quest(
        tmp_path, "beta", events_path=events,
        quest_json_path=quest_json, state_json_path=state_json)
    assert merge_commit_of(tmp_path, "beta") is None


def test_merge_commit_of_none_for_unknown_quest(tmp_path: Path) -> None:
    _git_init(tmp_path)
    assert merge_commit_of(tmp_path, "nope") is None
