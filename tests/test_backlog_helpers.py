from __future__ import annotations

from pathlib import Path

from imp.engine import backlog


def test_seed_opening_title_only_when_description_empty() -> None:
    rec = {"title": "Add OAuth", "description": ""}
    assert backlog.seed_opening(rec) == "Add OAuth"


def test_seed_opening_missing_description_key() -> None:
    rec = {"title": "Add OAuth"}
    assert backlog.seed_opening(rec) == "Add OAuth"


def test_seed_opening_joins_title_and_description() -> None:
    rec = {"title": "Add OAuth", "description": "wire up OAuth login"}
    assert backlog.seed_opening(rec) == "Add OAuth\n\nwire up OAuth login"


def test_readiness_no_deps_is_ready(tmp_path: Path) -> None:
    assert backlog.readiness(tmp_path, []) == (True, [])


def test_readiness_resolves_from_archive_index(tmp_path: Path) -> None:
    import json as _json
    import subprocess

    from imp.engine import archive

    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)

    events = tmp_path / "e.jsonl"
    events.write_text(
        '{"id":"d-e0","parent_id":null,"agent_id":null,'
        '"timestamp":"2026-06-01T00:00:01+00:00","type":"change_created",'
        '"payload":{}}\n')
    cj = tmp_path / "c.json"
    cj.write_text(_json.dumps({
        "id": "dep1", "title": "Dep", "base_branch": "main",
        "created_at": "2026-06-01T00:00:00+00:00", "depends_on": []}))
    sj = tmp_path / "s.json"
    sj.write_text(_json.dumps({"id": "dep1", "status": "completed", "step": None}))
    archive.archive_quest(
        tmp_path, "dep1", events_path=events,
        quest_json_path=cj, state_json_path=sj)

    assert backlog.readiness(tmp_path, ["dep1"]) == (True, [])
    assert backlog.readiness(tmp_path, ["dep1", "ghost"]) == (False, ["ghost"])
