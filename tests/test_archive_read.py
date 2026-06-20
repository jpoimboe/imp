from __future__ import annotations

import json
from pathlib import Path

from imp.engine import worktree
from imp.engine.archive.read import (
    archived_ids,
    completed_rows,
    is_archived,
    newest_plan_name,
    pick_newest_plan,
    read_artifact,
    read_change_json,
    read_events,
    read_state_json,
)


def _events_bytes(cid: str) -> bytes:
    return (
        f'{{"id":"{cid}-e0","parent_id":null,"agent_id":null,'
        '"timestamp":"2026-06-01T00:00:01+00:00","type":"change_created",'
        f'"payload":{{"id":"{cid}"}}}}\n'
        f'{{"id":"{cid}-e1","parent_id":"{cid}-e0","agent_id":"plan-writer",'
        '"timestamp":"2026-06-01T00:00:09+00:00","type":"step_started",'
        '"payload":{"step":"plan-writer"}}\n'
    ).encode()


def _archive(control: Path, cid: str, status: str) -> bytes:
    from imp.engine import archive

    events = control / f"{cid}.jsonl"
    events.write_bytes(_events_bytes(cid))
    change_json = control / f"{cid}-change.json"
    change_json.write_text(json.dumps({
        "id": cid, "title": cid.title(), "base_branch": "main",
        "created_at": "2026-06-01T00:00:00+00:00", "depends_on": ["dep1"]}))
    state_json = control / f"{cid}-state.json"
    state_json.write_text(json.dumps({
        "id": cid, "status": status, "step": None, "merge_commit": "c0ffee"}))
    archive.archive_change(
        control, cid, events_path=events,
        change_json_path=change_json, state_json_path=state_json)
    return events.read_bytes()


def test_archived_ids_unions_index_and_legacy_journal(tmp_path: Path) -> None:
    _git_init(tmp_path)
    _archive(tmp_path, "alpha", "completed")
    # A legacy journaled Change present only in refs/imp/journal.
    legacy = tmp_path / "legacy-events.jsonl"
    legacy.write_bytes(_events_bytes("beta"))
    worktree.journal_commit(tmp_path, "beta", {"events.jsonl": legacy})

    assert archived_ids(tmp_path) == ["alpha", "beta"]
    assert is_archived(tmp_path, "alpha") is True   # from the index
    assert is_archived(tmp_path, "beta") is True    # from the legacy journal
    assert is_archived(tmp_path, "missing") is False


def test_read_events_returns_blob_bytes_then_journal_fallback(tmp_path: Path) -> None:
    _git_init(tmp_path)
    raw = _archive(tmp_path, "alpha", "completed")
    assert read_events(tmp_path, "alpha") == raw   # byte-for-byte from the blob

    legacy = tmp_path / "legacy.jsonl"
    legacy.write_bytes(_events_bytes("beta"))
    worktree.journal_commit(tmp_path, "beta", {"events.jsonl": legacy})
    assert read_events(tmp_path, "beta") == legacy.read_bytes()
    assert read_events(tmp_path, "missing") is None


def test_read_state_and_change_json_reconstruct_from_record(tmp_path: Path) -> None:
    _git_init(tmp_path)
    _archive(tmp_path, "alpha", "completed")
    st = json.loads(read_state_json(tmp_path, "alpha"))
    assert st["status"] == "completed"
    assert st["merge_commit"] == "c0ffee"
    cj = json.loads(read_change_json(tmp_path, "alpha"))
    assert cj["title"] == "Alpha"
    assert cj["base_branch"] == "main"
    assert cj["created_at"] == "2026-06-01T00:00:00+00:00"
    assert cj["materialized"] is True


def test_completed_rows_single_query_carries_last_activity(tmp_path: Path) -> None:
    _git_init(tmp_path)
    _archive(tmp_path, "alpha", "completed")
    _archive(tmp_path, "zeta", "abandoned")
    rows = {r[0]: r for r in completed_rows(tmp_path)}
    # Row shape: (id, status, step, last_activity, title, created_at).
    assert rows["alpha"][1] == "completed"
    assert rows["zeta"][1] == "abandoned"
    assert rows["alpha"][2] == "-"                       # step None -> "-"
    assert rows["alpha"][3] == "2026-06-01T00:00:09+00:00"  # MAX(events.ts)
    assert rows["alpha"][4] == "Alpha"
    assert rows["alpha"][5] == "2026-06-01T00:00:00+00:00"


def test_read_artifact_prefers_wip_ref_then_legacy_journal(tmp_path: Path) -> None:
    _git_init(tmp_path)
    spec = tmp_path / "spec.md"
    spec.write_text("# WIP SPEC\n")
    worktree.artifact_commit(tmp_path, "alpha", {"spec/alpha.md": spec},
                             message="artifact: spec")
    assert read_artifact(tmp_path, "alpha", "spec/alpha.md") == b"# WIP SPEC\n"

    legacy_spec = tmp_path / "legacy-spec.md"
    legacy_spec.write_text("# LEGACY SPEC\n")
    worktree.journal_commit(tmp_path, "beta", {"spec/beta.md": legacy_spec})
    assert read_artifact(tmp_path, "beta", "spec/beta.md") == b"# LEGACY SPEC\n"
    assert read_artifact(tmp_path, "alpha", "nope.md") is None


def test_newest_plan_name_from_wip_then_legacy(tmp_path: Path) -> None:
    _git_init(tmp_path)
    p1 = tmp_path / "p1.md"
    p1.write_text("one")
    p2 = tmp_path / "p2.md"
    p2.write_text("two")
    worktree.artifact_commit(
        tmp_path, "alpha",
        {"plans/alpha-plan-1.md": p1, "plans/alpha-plan-2.md": p2},
        message="artifact: plans")
    assert newest_plan_name(tmp_path, "alpha") == "alpha-plan-2.md"

    legacy = tmp_path / "lp.md"
    legacy.write_text("x")
    worktree.journal_commit(tmp_path, "beta", {"plans/beta-plan-3.md": legacy})
    assert newest_plan_name(tmp_path, "beta") == "beta-plan-3.md"


def test_pick_newest_plan_picks_highest_n() -> None:
    assert pick_newest_plan(
        "alpha", ["alpha-plan-1.md", "alpha-plan-10.md", "alpha-plan-2.md"]
    ) == "alpha-plan-10.md"
    assert pick_newest_plan("alpha", ["unrelated.md"]) is None


def test_read_state_and_change_json_fall_back_to_journal(tmp_path: Path) -> None:
    _git_init(tmp_path)
    state = tmp_path / "state.json"
    state.write_text('{"id": "beta", "status": "completed"}')
    change = tmp_path / "change.json"
    change.write_text('{"id": "beta", "title": "Beta", "base_branch": "main"}')
    worktree.journal_commit(tmp_path, "beta", {
        "state.json": state, "change.json": change})

    st = read_state_json(tmp_path, "beta")
    assert st is not None and b'"completed"' in st
    cj = read_change_json(tmp_path, "beta")
    assert cj is not None and b'"Beta"' in cj

    assert read_state_json(tmp_path, "missing") is None
    assert read_change_json(tmp_path, "missing") is None


def _git_init(root: Path) -> None:
    import subprocess
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=root, check=True)
