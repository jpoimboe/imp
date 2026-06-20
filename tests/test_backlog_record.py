from __future__ import annotations

from pathlib import Path

import pytest

from imp.engine import backlog, journal


def test_write_read_roundtrip(tmp_path: Path) -> None:
    path = backlog.write_record(
        tmp_path, change_id="aaa111", title="Add OAuth",
        description="wire up OAuth login", depends_on=["dep001"],
        created_by="step:finalize")
    assert path == tmp_path / ".imp" / "backlog" / "aaa111.json"
    rec = backlog.read_record(tmp_path, "aaa111")
    assert rec is not None
    assert rec["id"] == "aaa111"
    assert rec["title"] == "Add OAuth"
    assert rec["description"] == "wire up OAuth login"
    assert rec["depends_on"] == ["dep001"]
    assert rec["created_by"] == "step:finalize"
    assert rec["created_at"]  # ISO timestamp present


def test_read_absent_returns_none(tmp_path: Path) -> None:
    assert backlog.read_record(tmp_path, "nope00") is None


def test_list_records_and_ids(tmp_path: Path) -> None:
    backlog.write_record(tmp_path, change_id="aaa111", title="A",
                         description="a", depends_on=[], created_by="human")
    backlog.write_record(tmp_path, change_id="bbb222", title="B",
                         description="b", depends_on=[], created_by="human")
    assert sorted(backlog.list_ids(tmp_path)) == [
        "aaa111", "bbb222"]
    titles = sorted(r["title"] for r in backlog.list_records(tmp_path))
    assert titles == ["A", "B"]


def test_remove_record(tmp_path: Path) -> None:
    backlog.write_record(tmp_path, change_id="aaa111", title="A",
                         description="a", depends_on=[], created_by="human")
    backlog.remove_record(tmp_path, "aaa111")
    assert backlog.read_record(tmp_path, "aaa111") is None
    assert backlog.list_ids(tmp_path) == []


def test_remove_record_nonexistent_is_safe(tmp_path: Path) -> None:
    backlog.remove_record(tmp_path, "nope00")  # must not raise


def test_readiness_all_deps_merged(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        journal, "journal_ids",
        lambda cr: ["dep001", "dep002"])
    ready, unmerged = backlog.readiness(
        tmp_path, ["dep001", "dep002"])
    assert ready is True
    assert unmerged == []


def test_readiness_blocked_names_absent_deps(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(journal, "journal_ids", lambda cr: ["dep001"])
    ready, unmerged = backlog.readiness(
        tmp_path, ["dep001", "missing"])
    assert ready is False
    assert unmerged == ["missing"]


def test_readiness_no_deps_is_ready(tmp_path: Path) -> None:
    assert backlog.readiness(tmp_path, []) == (True, [])


def test_write_record_uses_description_key(tmp_path: Path) -> None:
    backlog.write_record(
        tmp_path, change_id="aaa111", title="Add OAuth",
        description="wire up OAuth login", depends_on=[], created_by="human")
    rec = backlog.read_record(tmp_path, "aaa111")
    assert rec is not None
    assert rec["description"] == "wire up OAuth login"
    assert "brief" not in rec


def test_seed_opening_title_only_when_description_empty() -> None:
    rec = {"title": "Add OAuth", "description": ""}
    assert backlog.seed_opening(rec) == "Add OAuth"


def test_seed_opening_missing_description_key() -> None:
    rec = {"title": "Add OAuth"}
    assert backlog.seed_opening(rec) == "Add OAuth"


def test_seed_opening_joins_title_and_description() -> None:
    rec = {"title": "Add OAuth", "description": "wire up OAuth login"}
    assert backlog.seed_opening(rec) == "Add OAuth\n\nwire up OAuth login"


def test_write_record_rejects_blank_title(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        backlog.write_record(
            tmp_path, change_id="aaa111", title="   ",
            description="anything", depends_on=[], created_by="human")


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
    archive.archive_change(
        tmp_path, "dep1", events_path=events,
        change_json_path=cj, state_json_path=sj)

    assert backlog.readiness(tmp_path, ["dep1"]) == (True, [])
    assert backlog.readiness(tmp_path, ["dep1", "ghost"]) == (False, ["ghost"])
