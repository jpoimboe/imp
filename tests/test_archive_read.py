from __future__ import annotations

import json
from pathlib import Path

from imp.engine.archive.read import (
    completed_rows,
    last_event_iso,
    pick_newest_plan,
    read_change_json,
    read_state_json,
    status_of,
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


def test_pick_newest_plan_picks_highest_n() -> None:
    assert pick_newest_plan(
        "alpha", ["alpha-plan-1.md", "alpha-plan-10.md", "alpha-plan-2.md"]
    ) == "alpha-plan-10.md"
    assert pick_newest_plan("alpha", ["unrelated.md"]) is None


def _git_init(root: Path) -> None:
    import subprocess
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=root, check=True)


def test_last_event_iso_returns_last_event_timestamp(tmp_path: Path) -> None:
    _git_init(tmp_path)
    _archive(tmp_path, "alpha", "completed")
    assert last_event_iso(tmp_path, "alpha") == "2026-06-01T00:00:09+00:00"


def test_last_event_iso_returns_none_when_not_archived(tmp_path: Path) -> None:
    _git_init(tmp_path)
    assert last_event_iso(tmp_path, "ghost") is None


def test_last_event_iso_returns_none_for_empty_events(tmp_path: Path) -> None:
    from imp.engine import archive

    _git_init(tmp_path)
    events_path = tmp_path / "alpha.jsonl"
    events_path.write_bytes(b"")
    change_json_path = tmp_path / "alpha-change.json"
    change_json_path.write_text(json.dumps({
        "id": "alpha", "title": "Alpha", "base_branch": "main",
        "created_at": "2026-06-01T00:00:00+00:00", "depends_on": []}))
    state_json_path = tmp_path / "alpha-state.json"
    state_json_path.write_text(json.dumps({
        "id": "alpha", "status": "completed", "step": None, "merge_commit": "c0ffee"}))
    archive.archive_change(
        tmp_path, "alpha",
        events_path=events_path,
        change_json_path=change_json_path,
        state_json_path=state_json_path,
    )
    assert last_event_iso(tmp_path, "alpha") is None


def test_status_of_returns_status_from_record(tmp_path: Path) -> None:
    _git_init(tmp_path)
    _archive(tmp_path, "alpha", "completed")
    assert status_of(tmp_path, "alpha") == "completed"


def test_status_of_returns_none_when_not_archived(tmp_path: Path) -> None:
    _git_init(tmp_path)
    assert status_of(tmp_path, "ghost") is None
