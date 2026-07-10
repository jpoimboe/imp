from __future__ import annotations

import json
from pathlib import Path

from imp.engine import archive
from imp.engine.archive.maintenance import reap_archived_homes


def _home(control: Path, cid: str, status: str) -> Path:
    home = control / ".imp" / "changes" / cid
    (home / "state").mkdir(parents=True)
    (home / "change.json").write_text(json.dumps({
        "id": cid, "title": cid, "base_branch": "main",
        "created_at": "2026-06-01T00:00:00+00:00"}))
    (home / "state" / "state.json").write_text(json.dumps({
        "id": cid, "status": status, "step": None}))
    (home / "events.jsonl").write_text(
        '{"id":"e0","parent_id":null,"agent_id":null,'
        '"timestamp":"2026-06-01T00:00:01+00:00","type":"change_created",'
        '"payload":{"id":"' + cid + '"}}\n')
    return home


def _archive(control: Path, home: Path, cid: str) -> None:
    archive.archive_quest(
        control, cid, events_path=home / "events.jsonl",
        quest_json_path=home / "change.json",
        state_json_path=home / "state" / "state.json")


def test_reaps_only_archived_terminal_homes(tmp_path: Path) -> None:
    done = _home(tmp_path, "done", "completed")
    _archive(tmp_path, done, "done")               # terminal + durable in archive
    running = _home(tmp_path, "run", "new")         # in-flight, never archived
    branch1 = _home(tmp_path, "b1", "completed")    # terminal but NOT archived

    count = reap_archived_homes(tmp_path)

    assert count == 1
    assert not done.exists()                         # archived-terminal home reaped
    assert running.exists()                          # in-flight home kept
    assert branch1.exists()                          # un-archived completion kept


def test_corrupt_state_json_is_skipped(tmp_path: Path) -> None:
    bad = _home(tmp_path, "bad", "completed")
    (bad / "state" / "state.json").write_text("{ not json")

    assert reap_archived_homes(tmp_path) == 0     # corrupt state.json skipped, not raised
    assert bad.exists()


def test_reap_is_idempotent(tmp_path: Path) -> None:
    done = _home(tmp_path, "done", "completed")
    _archive(tmp_path, done, "done")

    assert reap_archived_homes(tmp_path) == 1
    assert reap_archived_homes(tmp_path) == 0        # nothing left to reap
    assert not done.exists()
