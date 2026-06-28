from __future__ import annotations

import json
from pathlib import Path

import imp.cli.main as m
from imp.engine import archive
from imp.engine.change import Change
from tests.conftest import _seed_control


def test_scan_change_homes_parses_each_home_once(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    Change.create(tmp_path, title="A", change_id="alpha", base_branch="main")
    Change.create(tmp_path, title="B", change_id="beta", base_branch="main")
    ch_a = Change.load(tmp_path, "alpha")
    ch_a.events_path.write_text('{"timestamp":"2026-06-05T12:00:00+00:00"}\n')
    # Change.create seeds a timestamped change_created event; truncate beta's
    # events.jsonl to exercise the empty-file -> "" branch of _scan_change_homes.
    Change.load(tmp_path, "beta").events_path.write_text("")

    scan = {h.change_id: h for h in m._scan_change_homes(tmp_path)}
    assert set(scan) == {"alpha", "beta"}
    assert scan["alpha"].meta["title"] == "A"
    assert scan["alpha"].state_json is not None
    assert scan["alpha"].last_iso == "2026-06-05T12:00:00+00:00"
    assert scan["beta"].last_iso == ""          # empty events.jsonl -> ""


def test_scan_change_homes_missing_state_json(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    Change.create(tmp_path, title="No State", change_id="noState", base_branch="main")
    state_file = tmp_path / ".imp" / "changes" / "noState" / "state" / "state.json"
    state_file.unlink()

    scan = {h.change_id: h for h in m._scan_change_homes(tmp_path)}
    assert "noState" in scan
    assert scan["noState"].state_json is None


def test_scan_change_homes_drops_malformed_change_json(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    Change.create(tmp_path, title="Bad JSON", change_id="badJson", base_branch="main")
    change_file = tmp_path / ".imp" / "changes" / "badJson" / "change.json"
    change_file.write_bytes(b"bad json")

    ids = {h.change_id for h in m._scan_change_homes(tmp_path)}
    assert "badJson" not in ids


def test_status_rows_uses_passed_scan(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    Change.create(tmp_path, title="A", change_id="alpha", base_branch="main")
    Change.create(tmp_path, title="B", change_id="beta", base_branch="main")

    scan = m._scan_change_homes(tmp_path)
    # Threaded scan and internal scan produce identical rows.
    assert m._status_rows(tmp_path, scan) == m._status_rows(tmp_path)
    assert {r[0] for r in m._status_rows(tmp_path)} == {"alpha", "beta"}
    # The passed scan is actually honored, not ignored: a one-home scan yields one row.
    alpha_only = [h for h in scan if h.change_id == "alpha"]
    assert {r[0] for r in m._status_rows(tmp_path, alpha_only)} == {"alpha"}


def test_backlog_rows_uses_passed_scan(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    Change.create_draft(tmp_path, title="Brief", change_id="bk1",
                        base_branch="main", origin="idea", seed_opening="Brief")
    Change.create_draft(tmp_path, title="Brief2", change_id="bk2",
                        base_branch="main", origin="idea", seed_opening="Brief2")

    scan = m._scan_change_homes(tmp_path)
    assert m._backlog_rows(tmp_path, scan) == m._backlog_rows(tmp_path)
    assert {r[0] for r in m._backlog_rows(tmp_path)} == {"bk1", "bk2"}
    bk1_only = [h for h in scan if h.change_id == "bk1"]
    assert {r[0] for r in m._backlog_rows(tmp_path, bk1_only)} == {"bk1"}


def _archive_completed(control: Path, cid: str) -> None:
    events = control / f"{cid}.jsonl"
    events.write_bytes(
        f'{{"id":"{cid}-e0","parent_id":null,"agent_id":null,'
        '"timestamp":"2026-06-01T00:00:09+00:00","type":"step_started",'
        f'"payload":{{"id":"{cid}"}}}}\n'.encode())
    change_json = control / f"{cid}-change.json"
    change_json.write_text(json.dumps({
        "id": cid, "title": cid.title(), "base_branch": "main",
        "created_at": "2026-06-01T00:00:00+00:00", "depends_on": [],
        "materialized": True}))
    state_json = control / f"{cid}-state.json"
    state_json.write_text(json.dumps({
        "id": cid, "status": "completed", "step": None, "merge_commit": "c0ffee"}))
    archive.archive_change(
        control, cid, events_path=events,
        change_json_path=change_json, state_json_path=state_json)


def test_completed_rows_honors_live_ids(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    _archive_completed(tmp_path, "gamma")
    assert {r[0] for r in m._completed_rows(tmp_path)} == {"gamma"}
    assert m._completed_rows(tmp_path, live_ids={"gamma"}) == []


def test_history_rows_threads_live_ids(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    _archive_completed(tmp_path, "gamma")
    assert {r[0] for r in m._history_rows(tmp_path, show_all=False)} == {"gamma"}
    assert m._history_rows(tmp_path, show_all=False, live_ids={"gamma"}) == []
