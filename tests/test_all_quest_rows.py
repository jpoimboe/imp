from __future__ import annotations

import json
from pathlib import Path

import imp.cli.main as m
from imp.engine import archive


def test_all_quest_rows_includes_no_merge_completed_live_home(tmp_path: Path) -> None:
    # AC 5a: a quest with a live home and persisted completed status (no merge
    # step, no archive record) appears in _all_quest_rows.  _status_rows drops
    # it (completed not in _INFLIGHT) and _completed_rows misses it (reads only
    # the archive, which has no record for this id).
    cid = "2026-06-10-nomc01"
    home = tmp_path / ".imp" / "quests" / cid
    home.mkdir(parents=True)
    (home / "quest.json").write_text(json.dumps({
        "id": cid, "title": "No-Merge Completed", "base_branch": "main",
        "created_at": "2026-06-10T00:00:00+00:00", "depends_on": [],
        "materialized": True}))
    (home / "state").mkdir()
    (home / "state" / "state.json").write_text(json.dumps(
        {"id": cid, "status": "completed", "step": None}))
    (home / "events.jsonl").write_text("")

    ids = {r[0] for r in m._all_quest_rows(tmp_path)}
    assert cid in ids


def test_all_quest_rows_includes_shelved_paused_archive(tmp_path: Path) -> None:
    # AC 5b: a quest archived with final status paused (shelved, no live home)
    # appears in _all_quest_rows.  _completed_rows drops it (paused in
    # _INFLIGHT), _status_rows and _backlog_rows never see archived ids without
    # live homes.
    cid = "2026-06-11-shvd01"
    events = tmp_path / f"{cid}.jsonl"
    events.write_bytes(
        f'{{"id":"{cid}-e0","parent_id":null,"agent_id":null,'
        f'"timestamp":"2026-06-11T00:00:00+00:00","type":"step_started",'
        f'"payload":{{"id":"{cid}"}}}}\n'.encode())
    quest_json = tmp_path / f"{cid}-quest.json"
    quest_json.write_text(json.dumps({
        "id": cid, "title": "Shelved Paused", "base_branch": "main",
        "created_at": "2026-06-11T00:00:00+00:00", "depends_on": [],
        "materialized": False}))
    state_json = tmp_path / f"{cid}-state.json"
    state_json.write_text(json.dumps({"id": cid, "status": "paused", "step": None}))
    archive.archive_quest(
        tmp_path, cid, events_path=events,
        quest_json_path=quest_json, state_json_path=state_json)

    ids = {r[0] for r in m._all_quest_rows(tmp_path)}
    assert cid in ids
