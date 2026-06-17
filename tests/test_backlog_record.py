from __future__ import annotations

from pathlib import Path

from imp.engine import backlog, journal


def test_write_read_roundtrip(tmp_path: Path) -> None:
    path = backlog.write_record(
        tmp_path, change_id="aaa111", title="Add OAuth",
        brief="wire up OAuth login", depends_on=["dep001"],
        created_by="step:finalize")
    assert path == tmp_path / ".imp" / "backlog" / "aaa111.json"
    rec = backlog.read_record(tmp_path, "aaa111")
    assert rec is not None
    assert rec["id"] == "aaa111"
    assert rec["title"] == "Add OAuth"
    assert rec["brief"] == "wire up OAuth login"
    assert rec["depends_on"] == ["dep001"]
    assert rec["created_by"] == "step:finalize"
    assert rec["created_at"]  # ISO timestamp present


def test_read_absent_returns_none(tmp_path: Path) -> None:
    assert backlog.read_record(tmp_path, "nope00") is None


def test_list_records_and_ids(tmp_path: Path) -> None:
    backlog.write_record(tmp_path, change_id="aaa111", title="A",
                         brief="a", depends_on=[], created_by="human")
    backlog.write_record(tmp_path, change_id="bbb222", title="B",
                         brief="b", depends_on=[], created_by="human")
    assert sorted(backlog.list_ids(tmp_path)) == [
        "aaa111", "bbb222"]
    titles = sorted(r["title"] for r in backlog.list_records(tmp_path))
    assert titles == ["A", "B"]


def test_remove_record(tmp_path: Path) -> None:
    backlog.write_record(tmp_path, change_id="aaa111", title="A",
                         brief="a", depends_on=[], created_by="human")
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
